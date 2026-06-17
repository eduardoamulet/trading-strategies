"""Parquet cache layer on top of PolygonAdapter.

Thread-safe: el backtest de rango corre varias iteraciones EN PARALELO (hilos) y
todas comparten un mismo Downloader. Estrategia de locking:

  - Un lock POR ARCHIVO (path), creado on-demand bajo un guard global. Dos hilos que
    tocan archivos DISTINTOS corren en paralelo; el acceso al MISMO archivo (lectura
    cacheada o escritura en cache-miss) se serializa. Eso evita el "cache-miss write
    race" (dos hilos pegando a la API y escribiendo el mismo parquet a la vez) y que
    un hilo lea un parquet que otro está escribiendo a medias.
  - Escritura ATÓMICA (a un .tmp + os.replace) para que un lector nunca vea un
    archivo a medio escribir, ni siquiera desde otro proceso.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

import pandas as pd

from adapter_polygon import PolygonAdapter


class Downloader:
    # Resolución de barras → (sufijo de cache, multiplier, timespan de Polygon).
    RES = {"1min": ("", 1, "minute"), "30s": ("_30s", 30, "second"), "15s": ("_15s", 15, "second")}

    def __init__(self, adapter: PolygonAdapter, data_dir: Path):
        self.adapter = adapter
        self.data_dir = Path(data_dir)
        # Resolución por defecto de las barras (underlying + opción). La setea la app antes
        # de cada backtest (toggle 1 min / 30 s / 15 s); los hilos del batch solo la LEEN.
        self.resolution = "1min"
        (self.data_dir / "underlying").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "chain").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "options").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "quotes").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "quotes_minute").mkdir(parents=True, exist_ok=True)
        # Lock por path (creados on-demand bajo el guard). Serializa SOLO el acceso al
        # mismo archivo; archivos distintos no se bloquean entre sí.
        self._path_locks: dict[str, threading.Lock] = {}
        self._path_locks_guard = threading.Lock()

    def _lock_for(self, path) -> threading.Lock:
        key = str(path)
        with self._path_locks_guard:
            lk = self._path_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._path_locks[key] = lk
            return lk

    @staticmethod
    def _write_parquet(df: pd.DataFrame, path: Path) -> None:
        """Escritura atómica: escribe a un .tmp y renombra (os.replace es atómico en
        el mismo filesystem) → un lector nunca ve un parquet a medio escribir."""
        tmp = Path(str(path) + ".tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)

    def underlying(self, ticker: str, date: str, force: bool = False,
                   resolution: Optional[str] = None) -> pd.DataFrame:
        _sfx, _mult, _span = self.RES.get(resolution or self.resolution, ("", 1, "minute"))
        path = self.data_dir / "underlying" / f"{ticker}_{date}{_sfx}.parquet"
        with self._lock_for(path):
            if path.exists() and not force:
                return pd.read_parquet(path)
            df = self.adapter.underlying_minute_bars(ticker, date, _mult, _span)
            if not df.empty:
                self._write_parquet(df, path)
            return df

    def chain(self, ticker: str, expiry: str, force: bool = False) -> pd.DataFrame:
        path = self.data_dir / "chain" / f"{ticker}_{expiry}.parquet"
        with self._lock_for(path):
            if path.exists() and not force:
                return pd.read_parquet(path)
            df = self.adapter.options_chain(ticker, expiry)
            if not df.empty:
                self._write_parquet(df, path)
            return df

    def option(self, occ_symbol: str, date: str, force: bool = False,
               resolution: Optional[str] = None) -> pd.DataFrame:
        _sfx, _mult, _span = self.RES.get(resolution or self.resolution, ("", 1, "minute"))
        safe = occ_symbol.replace(":", "_")
        path = self.data_dir / "options" / f"{safe}_{date}{_sfx}.parquet"
        with self._lock_for(path):
            if path.exists() and not force:
                return pd.read_parquet(path)
            df = self.adapter.option_minute_bars(occ_symbol, date, _mult, _span)
            if not df.empty:
                self._write_parquet(df, path)
            return df

    def option_quote(self, occ_symbol: str, date: str, entry_ts, force: bool = False) -> dict:
        """NBBO (bid/ask/spread) vigente al `entry_ts`, cacheado por
        (occ, date, HHMM). Idempotente: re-runs con el mismo minuto de entrada
        no re-pegan a Polygon. Devuelve dict {bid, ask, bid_size, ask_size, spread}."""
        ts = pd.Timestamp(entry_ts)
        hhmm = ts.strftime("%H%M")
        safe = occ_symbol.replace(":", "_")
        path = self.data_dir / "quotes" / f"{safe}_{date}_{hhmm}.parquet"
        with self._lock_for(path):
            if path.exists() and not force:
                df = pd.read_parquet(path)
                if not df.empty:
                    r = df.iloc[0]
                    return {
                        "bid": None if pd.isna(r["bid"]) else float(r["bid"]),
                        "ask": None if pd.isna(r["ask"]) else float(r["ask"]),
                        "bid_size": None if pd.isna(r["bid_size"]) else float(r["bid_size"]),
                        "ask_size": None if pd.isna(r["ask_size"]) else float(r["ask_size"]),
                        "spread": None if pd.isna(r["spread"]) else float(r["spread"]),
                    }
            q = self.adapter.option_quote_at(occ_symbol, ts)
            # Persistir aunque sea vacío (None) para no re-intentar contratos sin quote.
            self._write_parquet(pd.DataFrame([q]), path)
            return q

    def option_quote_series(self, occ_symbol: str, date: str, force: bool = False) -> pd.DataFrame:
        """Línea de bid/ask por MINUTO de la sesión RTH (09:30–16:00) de `occ`/`date`,
        cacheada en quotes_minute/ (1 parquet por occ+date, reusado por TODAS las
        iteraciones de ese contrato/día). Trae los NBBO crudos de la sesión y los
        resamplea al ÚLTIMO bid/ask de cada minuto → alineado al CIERRE del minuto, igual
        que el bar (arregla el desfasaje de Fase 1, que tomaba el bid al inicio del bar).
        Para los fills NBBO por barra (Fase 2). Devuelve DataFrame[timestamp, bid, ask]
        (vacío si el contrato no tuvo quotes). Idempotente vía cache."""
        safe = occ_symbol.replace(":", "_")
        path = self.data_dir / "quotes_minute" / f"{safe}_{date}.parquet"
        with self._lock_for(path):
            if path.exists() and not force:
                return pd.read_parquet(path)
            start = pd.Timestamp(f"{date} 09:30", tz="America/New_York")
            end = pd.Timestamp(f"{date} 16:00", tz="America/New_York")
            raw = self.adapter.option_quotes_window(occ_symbol, start, end)
            if raw is None or raw.empty:
                out = pd.DataFrame(columns=["timestamp", "bid", "ask"])
            else:
                raw = raw.copy()
                raw["timestamp"] = raw["timestamp"].dt.floor("min")
                out = (raw.groupby("timestamp", as_index=False)
                          .agg(bid=("bid", "last"), ask=("ask", "last")))
            # Persistir aunque sea vacío para no re-pegar a la API en contratos sin quotes.
            self._write_parquet(out, path)
            return out

    def nearest_expiry(self, ticker: str, on_or_after: str) -> Optional[str]:
        """Return the smallest expiration >= on_or_after, or None.

        Fallback: el filtro `expiration_date.gte` de Polygon a veces NO incluye la
        expiración del MISMO día (el 0DTE de hoy), aunque los contratos existan. Si
        la lista viene vacía pero la consulta EXACTA de la cadena para esa fecha trae
        contratos, ese día sí tiene 0DTE → devolvemos esa fecha."""
        exps = self.adapter.list_expirations(ticker, on_or_after)
        if exps:
            return exps[0]
        if not self.adapter.options_chain(ticker, on_or_after).empty:
            return on_or_after
        return None

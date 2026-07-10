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
from collections import OrderedDict
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
        # Cache LRU en MEMORIA de parquets ya parseados. En el batch los MISMOS archivos (mismo
        # ticker/fecha) se re-leen N veces (1 por config) → read_parquet era ~80% del run_one. El
        # cache elimina esas relecturas. Acotado (LRU) para no crecer sin límite.
        self._pq_cache: "OrderedDict[str, pd.DataFrame]" = OrderedDict()
        self._pq_cache_lock = threading.Lock()
        self._pq_cache_max = 6000
        # Memo de vencimiento más cercano por (ticker, fecha). Con lock: en el backtest de señales
        # varios hilos comparten el Downloader y llaman nearest_expiry → sin lock, el `hasattr`+dict
        # era la ÚNICA costura sin proteger (race de lectura/escritura + fetches duplicados a la API).
        self._ne_cache: dict = {}
        self._ne_cache_lock = threading.Lock()

    def _lock_for(self, path) -> threading.Lock:
        key = str(path)
        with self._path_locks_guard:
            lk = self._path_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._path_locks[key] = lk
            return lk

    def _read_pq(self, path) -> pd.DataFrame:
        """Lee un parquet con cache LRU en memoria. Devuelve el frame CACHEADO (sin copiar); los
        call-sites que lo DEVUELVEN al exterior hacen .copy() para no mutar el original del cache."""
        key = str(path)
        with self._pq_cache_lock:
            df = self._pq_cache.get(key)
            if df is not None:
                self._pq_cache.move_to_end(key)
                return df
        df = pd.read_parquet(path)
        with self._pq_cache_lock:
            self._pq_cache[key] = df
            self._pq_cache.move_to_end(key)
            while len(self._pq_cache) > self._pq_cache_max:
                self._pq_cache.popitem(last=False)
        return df

    def _write_parquet(self, df: pd.DataFrame, path: Path) -> None:
        """Escritura atómica: escribe a un .tmp y renombra (os.replace es atómico en el mismo
        filesystem) → un lector nunca ve un parquet a medio escribir. Invalida el cache en memoria
        de ese path (un force/refresh no debe servir datos viejos)."""
        tmp = Path(str(path) + ".tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        with self._pq_cache_lock:
            self._pq_cache.pop(str(path), None)

    def _cache_confiable(self, path, date: str) -> bool:
        """Un parquet de un día es CONFIABLE solo si se escribió DESPUÉS del cierre de esa
        sesión (16:05 ET). Un fetch intradía/pre-market cachea un día PARCIAL (barras a
        medias) o VACÍO, y ese archivo envenena todo recompute futuro — la deriva del verify
        del 2026-07-08/09 fue exactamente esto (quotes 0/0 y barras parciales cacheadas en
        caliente). mtime < cierre → stale → se refetchea y sobreescribe (auto-sana el
        histórico envenenado sin borrar nada a mano; los históricos legítimos, descargados
        siempre después de su cierre, pasan intactos)."""
        try:
            fin = pd.Timestamp(f"{date} 16:05", tz="America/New_York")
            mt = (pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")
                  .tz_convert("America/New_York"))
            return mt >= fin
        except Exception:  # noqa: BLE001 — ante la duda, el cache vale (comportamiento previo)
            return True

    def underlying(self, ticker: str, date: str, force: bool = False,
                   resolution: Optional[str] = None) -> pd.DataFrame:
        _sfx, _mult, _span = self.RES.get(resolution or self.resolution, ("", 1, "minute"))
        path = self.data_dir / "underlying" / f"{ticker}_{date}{_sfx}.parquet"
        with self._lock_for(path):
            if path.exists() and not force and self._cache_confiable(path, date):
                return self._read_pq(path).copy()
            if getattr(self, "offline", False):
                return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            df = self.adapter.underlying_minute_bars(ticker, date, _mult, _span)
            if not df.empty:
                self._write_parquet(df, path)
            return df

    def chain(self, ticker: str, expiry: str, force: bool = False) -> pd.DataFrame:
        path = self.data_dir / "chain" / f"{ticker}_{expiry}.parquet"
        with self._lock_for(path):
            if path.exists() and not force and self._cache_confiable(path, expiry):
                return self._read_pq(path).copy()
            if getattr(self, "offline", False):
                return pd.DataFrame()
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
            if path.exists() and not force and self._cache_confiable(path, date):
                return self._read_pq(path).copy()
            if getattr(self, "offline", False):
                return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
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
            if path.exists() and not force and self._cache_confiable(path, date):
                df = self._read_pq(path)
                if not df.empty:
                    r = df.iloc[0]
                    q = {
                        "bid": None if pd.isna(r["bid"]) else float(r["bid"]),
                        "ask": None if pd.isna(r["ask"]) else float(r["ask"]),
                        "bid_size": None if pd.isna(r["bid_size"]) else float(r["bid_size"]),
                        "ask_size": None if pd.isna(r["ask_size"]) else float(r["ask_size"]),
                        "spread": None if pd.isna(r["spread"]) else float(r["spread"]),
                    }
                    # Un 0/0 cacheado = fallo TRANSITORIO de la API en su momento, no un NBBO
                    # real (envenenó QQQ 2026-07-08: 480 escenarios con «Sin contrato» falsos).
                    # Se trata como cache-miss → refetch y sobreescritura.
                    if q["bid"] or q["ask"]:
                        return q
            # Fallback OFFLINE (opt-in): servir el NBBO de entrada desde quotes_minute (que ya
            # tenemos cacheado 4 años) en vez de pegar a Polygon. Default OFF → ni el live ni el
            # flujo normal cambian; solo el walk-forward histórico activa entry_from_timeline.
            if getattr(self, "entry_from_timeline", False):
                try:
                    ser = self.option_quote_series(occ_symbol, date)
                except Exception:
                    ser = None
                q = self._quote_from_series(ser, ts)
                # En offline (batch histórico) NO escribimos a quotes/: serían miles de archivitos
                # (lentísimo en OneDrive) y el quote ya sale de quotes_minute cacheado.
                if not getattr(self, "offline", False):
                    self._write_parquet(pd.DataFrame([q]), path)
                return q
            q = self.adapter.option_quote_at(occ_symbol, ts)
            # Persistir SOLO si hay NBBO usable: un 0/0-None (fallo transitorio de la API o
            # pedido prematuro) cacheado «para siempre» fue el veneno del 2026-07-08. Sin
            # quote usable → NO se persiste y la próxima corrida reintenta.
            if (q or {}).get("bid") or (q or {}).get("ask"):
                self._write_parquet(pd.DataFrame([q]), path)
            return q

    @staticmethod
    def _quote_from_series(ser, ts) -> dict:
        """NBBO de entrada tomado de la línea por-minuto (quotes_minute): el quote vigente al
        minuto de `ts` (o el último minuto previo). Sin sizes. Para el modo offline/histórico."""
        empty = {"bid": None, "ask": None, "bid_size": None, "ask_size": None, "spread": None}
        if ser is None or getattr(ser, "empty", True):
            return empty
        minute = pd.Timestamp(ts).floor("min")
        exact = ser[ser["timestamp"] == minute]
        row = exact if not exact.empty else ser[ser["timestamp"] <= minute].tail(1)
        if row.empty:
            return empty
        r = row.iloc[0]
        bid = None if pd.isna(r["bid"]) else float(r["bid"])
        ask = None if pd.isna(r["ask"]) else float(r["ask"])
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        return {"bid": bid, "ask": ask, "bid_size": None, "ask_size": None, "spread": spread}

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
            if path.exists() and not force and self._cache_confiable(path, date):
                return self._read_pq(path).copy()
            if getattr(self, "offline", False):
                return pd.DataFrame(columns=["timestamp", "bid", "ask"])
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
            # Persistir vacío SOLO si la sesión de ese día YA CERRÓ (contrato genuinamente
            # sin quotes → no re-pegar a la API). Un vacío pedido pre-market/intradía es
            # PREMATURO y cachearlo envenena el histórico → se devuelve sin persistir.
            if out.empty:
                fin = pd.Timestamp(f"{date} 16:05", tz="America/New_York")
                if pd.Timestamp.now(tz="America/New_York") < fin:
                    return out
            self._write_parquet(out, path)
            return out

    def nearest_expiry(self, ticker: str, on_or_after: str) -> Optional[str]:
        """Return the smallest expiration >= on_or_after, or None.

        UNA sola llamada a la API (vencimientos ordenados asc, limit 1) + memo por (ticker,
        fecha). Antes paginaba TODOS los contratos (decenas de páginas → ~25s para SPY/QQQ).

        Fallback: el filtro `expiration_date.gte` de Polygon a veces NO incluye la
        expiración del MISMO día (el 0DTE de hoy), aunque los contratos existan."""
        _k = (ticker, on_or_after)
        with self._ne_cache_lock:              # lectura cacheada bajo lock (rápida)
            if _k in self._ne_cache:
                return self._ne_cache[_k]
        # Cache-miss: se pega a la API FUERA del lock (no serializa a los demás hilos; a lo sumo dos
        # hilos hacen el mismo fetch una vez → mismo resultado, inofensivo).
        try:
            _r = self.adapter.first_expiration(ticker, on_or_after)
        except Exception:
            _r = None
        if _r is None and not self.adapter.options_chain(ticker, on_or_after).empty:
            _r = on_or_after   # 0DTE existe pero el filtro gte no lo listó
        with self._ne_cache_lock:              # escritura bajo lock
            self._ne_cache[_k] = _r
        return _r

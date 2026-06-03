"""Parquet cache layer on top of PolygonAdapter."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from adapter_polygon import PolygonAdapter


class Downloader:
    def __init__(self, adapter: PolygonAdapter, data_dir: Path):
        self.adapter = adapter
        self.data_dir = Path(data_dir)
        (self.data_dir / "underlying").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "chain").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "options").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "quotes").mkdir(parents=True, exist_ok=True)

    def underlying(self, ticker: str, date: str, force: bool = False) -> pd.DataFrame:
        path = self.data_dir / "underlying" / f"{ticker}_{date}.parquet"
        if path.exists() and not force:
            return pd.read_parquet(path)
        df = self.adapter.underlying_minute_bars(ticker, date)
        if not df.empty:
            df.to_parquet(path, index=False)
        return df

    def chain(self, ticker: str, expiry: str, force: bool = False) -> pd.DataFrame:
        path = self.data_dir / "chain" / f"{ticker}_{expiry}.parquet"
        if path.exists() and not force:
            return pd.read_parquet(path)
        df = self.adapter.options_chain(ticker, expiry)
        if not df.empty:
            df.to_parquet(path, index=False)
        return df

    def option(self, occ_symbol: str, date: str, force: bool = False) -> pd.DataFrame:
        safe = occ_symbol.replace(":", "_")
        path = self.data_dir / "options" / f"{safe}_{date}.parquet"
        if path.exists() and not force:
            return pd.read_parquet(path)
        df = self.adapter.option_minute_bars(occ_symbol, date)
        if not df.empty:
            df.to_parquet(path, index=False)
        return df

    def option_quote(self, occ_symbol: str, date: str, entry_ts, force: bool = False) -> dict:
        """NBBO (bid/ask/spread) vigente al `entry_ts`, cacheado por
        (occ, date, HHMM). Idempotente: re-runs con el mismo minuto de entrada
        no re-pegan a Polygon. Devuelve dict {bid, ask, bid_size, ask_size, spread}."""
        ts = pd.Timestamp(entry_ts)
        hhmm = ts.strftime("%H%M")
        safe = occ_symbol.replace(":", "_")
        path = self.data_dir / "quotes" / f"{safe}_{date}_{hhmm}.parquet"
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
        pd.DataFrame([q]).to_parquet(path, index=False)
        return q

    def nearest_expiry(self, ticker: str, on_or_after: str) -> Optional[str]:
        """Return the smallest expiration >= on_or_after, or None."""
        exps = self.adapter.list_expirations(ticker, on_or_after)
        return exps[0] if exps else None

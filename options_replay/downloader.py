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

    def nearest_expiry(self, ticker: str, on_or_after: str) -> Optional[str]:
        """Return the smallest expiration >= on_or_after, or None."""
        exps = self.adapter.list_expirations(ticker, on_or_after)
        return exps[0] if exps else None

"""Polygon.io HTTP client for options + underlying minute bars."""
from __future__ import annotations

import time
from typing import Optional

import pandas as pd
import requests


class PolygonAdapter:
    BASE = "https://api.polygon.io"

    def __init__(self, api_key: str, rate_limit_per_min: int = 100, timeout: int = 30):
        if not api_key:
            raise ValueError("POLYGON_API_KEY is empty. Set it in ../config.py")
        self._key = api_key
        self._session = requests.Session()
        self._min_interval = 60.0 / max(rate_limit_per_min, 1)
        self._timeout = timeout
        self._last_call = 0.0

    def _get(self, path_or_url: str, params: Optional[dict] = None) -> dict:
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        params = dict(params or {})
        params["apiKey"] = self._key
        url = path_or_url if path_or_url.startswith("http") else self.BASE + path_or_url
        r = self._session.get(url, params=params, timeout=self._timeout)
        self._last_call = time.time()
        r.raise_for_status()
        return r.json()

    # ---------- underlying ----------

    def underlying_minute_bars(self, ticker: str, date: str) -> pd.DataFrame:
        """1-min OHLCV for the underlying on `date` (YYYY-MM-DD), in America/New_York tz."""
        path = f"/v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}"
        data = self._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        return self._bars_to_df(data.get("results", []))

    # ---------- chain ----------

    def options_chain(self, ticker: str, expiry: str) -> pd.DataFrame:
        """All contracts for `ticker` with given `expiry` (YYYY-MM-DD)."""
        path = "/v3/reference/options/contracts"
        params = {
            "underlying_ticker": ticker,
            "expiration_date": expiry,
            "limit": 1000,
            "expired": "true",
        }
        rows: list[dict] = []
        next_url: Optional[str] = None
        while True:
            data = self._get(next_url or path, params if next_url is None else None)
            rows.extend(data.get("results", []))
            next_url = data.get("next_url")
            if not next_url:
                break
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        keep = [c for c in ["ticker", "underlying_ticker", "contract_type",
                            "strike_price", "expiration_date", "shares_per_contract"] if c in df.columns]
        return df[keep]

    def list_expirations(self, ticker: str, on_or_after: str) -> list[str]:
        """Distinct expirations >= `on_or_after` (YYYY-MM-DD), ascending."""
        path = "/v3/reference/options/contracts"
        params = {
            "underlying_ticker": ticker,
            "expiration_date.gte": on_or_after,
            "limit": 1000,
            "expired": "true",
        }
        seen: set[str] = set()
        next_url: Optional[str] = None
        while True:
            data = self._get(next_url or path, params if next_url is None else None)
            for row in data.get("results", []):
                exp = row.get("expiration_date")
                if exp:
                    seen.add(exp)
            next_url = data.get("next_url")
            if not next_url:
                break
        return sorted(seen)

    # ---------- option bars ----------

    def option_minute_bars(self, occ_symbol: str, date: str) -> pd.DataFrame:
        path = f"/v2/aggs/ticker/{occ_symbol}/range/1/minute/{date}/{date}"
        data = self._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        return self._bars_to_df(data.get("results", []))

    # ---------- helpers ----------

    @staticmethod
    def build_occ(underlying: str, expiry: str, right: str, strike: float) -> str:
        """Build OPRA OCC symbol, e.g. O:SPY260521C00585000."""
        right = right.upper()
        if right not in ("C", "P"):
            raise ValueError("right must be 'C' or 'P'")
        yymmdd = expiry.replace("-", "")[2:]
        strike_int = int(round(float(strike) * 1000))
        return f"O:{underlying.upper()}{yymmdd}{right}{strike_int:08d}"

    @staticmethod
    def _bars_to_df(results: list[dict]) -> pd.DataFrame:
        if not results:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(results)
        df["timestamp"] = (
            pd.to_datetime(df["t"], unit="ms", utc=True)
            .dt.tz_convert("America/New_York")
        )
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        return df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

"""Polygon.io HTTP client for options + underlying minute bars."""
from __future__ import annotations

import time
from typing import Optional

import pandas as pd
import requests


INDEX_TICKERS = {"SPX", "VIX", "NDX", "RUT", "DJX", "XSP"}


def _aggs_ticker(ticker: str) -> str:
    """Polygon aggregates require the 'I:' prefix for indices (SPX, VIX, etc.)
    but the chain/contracts endpoint uses the bare ticker. Helper for the
    underlying-aggregates path only."""
    t = ticker.upper().strip()
    return f"I:{t}" if t in INDEX_TICKERS else t


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

    def _get(self, path_or_url: str, params: Optional[dict] = None, max_retries: int = 4) -> dict:
        params = dict(params or {})
        params["apiKey"] = self._key
        url = path_or_url if path_or_url.startswith("http") else self.BASE + path_or_url

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            elapsed = time.time() - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            try:
                r = self._session.get(url, params=params, timeout=self._timeout)
                self._last_call = time.time()
                if r.status_code >= 500:
                    last_exc = requests.HTTPError(f"server {r.status_code} on {url}")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    r.raise_for_status()
                r.raise_for_status()
                return r.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                self._last_call = time.time()
                last_exc = e
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
                    continue
                raise
        raise last_exc if last_exc is not None else RuntimeError("max_retries exhausted")

    # ---------- underlying ----------

    def underlying_minute_bars(self, ticker: str, date: str) -> pd.DataFrame:
        """1-min OHLCV for the underlying on `date` (YYYY-MM-DD), in America/New_York tz.
        Index tickers (SPX, VIX, ...) get prefixed with 'I:' as Polygon requires."""
        polygon_ticker = _aggs_ticker(ticker)
        path = f"/v2/aggs/ticker/{polygon_ticker}/range/1/minute/{date}/{date}"
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

    # ---------- option NBBO quote (bid/ask) ----------

    def option_quote_at(self, occ_symbol: str, entry_ts: pd.Timestamp) -> dict:
        """NBBO (bid/ask) vigente AL momento de entrada `entry_ts` (tz-aware).
        Trae el último quote con sip_timestamp <= entry_ts.
        Devuelve dict con bid, ask, bid_size, ask_size, spread (o None si no hay).
        Requiere plan Polygon con Options Quotes habilitado."""
        ns = int(pd.Timestamp(entry_ts).value)  # epoch ns (tz-aware → UTC ns)
        path = f"/v3/quotes/{occ_symbol}"
        data = self._get(path, {
            "timestamp.lte": ns,
            "order": "desc",
            "sort": "timestamp",
            "limit": 1,
        })
        results = data.get("results", [])
        if not results:
            return {"bid": None, "ask": None, "bid_size": None,
                    "ask_size": None, "spread": None}
        q = results[0]
        bid = q.get("bid_price")
        ask = q.get("ask_price")
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        return {
            "bid": bid,
            "ask": ask,
            "bid_size": q.get("bid_size"),
            "ask_size": q.get("ask_size"),
            "spread": spread,
        }

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
        # Los índices (NDX, RUT, etc.) no tienen volumen — Polygon devuelve la
        # respuesta sin la clave `v`. Para mantener un esquema uniforme con
        # ETFs/stocks, defaulteamos a 0.
        if "volume" not in df.columns:
            df["volume"] = 0
        return df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

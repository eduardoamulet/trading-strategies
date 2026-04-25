"""Data loader with Alpaca + local parquet cache."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import ALPACA_API_KEY, ALPACA_API_SECRET
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

CACHE = ROOT / "data_cache"
CACHE.mkdir(exist_ok=True)

_client: StockHistoricalDataClient | None = None

TF_MAP = {
    "1H":  TimeFrame(1, TimeFrameUnit.Hour),
    "15m": TimeFrame(15, TimeFrameUnit.Minute),
    "1D":  TimeFrame(1, TimeFrameUnit.Day),
}


def _get_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_API_SECRET)
    return _client


def load_bars(
    ticker: str,
    timeframe: str,
    start: datetime = datetime(2014, 1, 1),
    end: datetime = datetime(2026, 4, 22),
    feed: str = "iex",
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Load OHLCV bars for a ticker + timeframe.

    Returns a DataFrame indexed by UTC timestamp with columns:
    open, high, low, close, volume
    """
    cache_file = CACHE / f"{ticker}_{timeframe}.parquet"
    if cache_file.exists() and not force_refresh:
        df = pd.read_parquet(cache_file)
        return df

    if timeframe not in TF_MAP:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TF_MAP[timeframe],
        start=start,
        end=end,
        feed=feed,
    )
    bars = _get_client().get_stock_bars(req).df

    if bars.empty:
        raise RuntimeError(f"No data for {ticker} {timeframe}")

    # Drop the symbol level from MultiIndex
    df = bars.reset_index(level=0, drop=True)
    # Keep only OHLCV
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.index.name = "timestamp"
    df.to_parquet(cache_file)
    return df


def load_all(ticker: str, *, force_refresh: bool = False) -> dict[str, pd.DataFrame]:
    """Download 1H, 15m and 1D bars for a ticker (cached)."""
    return {
        "1H":  load_bars(ticker, "1H",  force_refresh=force_refresh),
        "15m": load_bars(ticker, "15m", force_refresh=force_refresh),
        "1D":  load_bars(ticker, "1D",  force_refresh=force_refresh),
    }


if __name__ == "__main__":
    # Quick smoke test
    ticker = sys.argv[1] if len(sys.argv) > 1 else "QQQ"
    data = load_all(ticker)
    for tf, df in data.items():
        print(f"{ticker} {tf}: {len(df)} bars  {df.index.min()} -> {df.index.max()}")

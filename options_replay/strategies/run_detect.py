"""Runner: carga 1-min cacheado (Polygon) -> 15m RTH -> detecta señales de una estrategia.

Uso:
    py strategies/run_detect.py --ticker SPY --start 2026-05-20 --end 2026-06-23
    py strategies/run_detect.py --ticker QQQ,SPY,IWM --start 2026-01-01 --end 2026-06-23 --out signals.csv
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from strategies import indicators as ind                      # noqa: E402
from strategies.trend_reversal_bb_15m import TrendReversalBB15m  # noqa: E402

DATA = HERE.parent / "data" / "underlying"

STRATEGIES = {"trend_reversal_bb_15m": TrendReversalBB15m}


def load_15m(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Carga todos los parquet 1-min de [start, end] y los pasa a 15m RTH continuo."""
    files = sorted(glob.glob(str(DATA / f"{ticker}_*.parquet")))
    keep = [f for f in files
            if start <= os.path.basename(f)[len(ticker) + 1:-8] <= end]
    if not keep:
        return pd.DataFrame()
    df1 = pd.concat([pd.read_parquet(f) for f in keep], ignore_index=True)
    return ind.to_15m_rth(df1)


def detect(ticker: str, start: str, end: str, strat) -> pd.DataFrame:
    b15 = load_15m(ticker, start, end)
    if b15.empty:
        return pd.DataFrame()
    return strat.detect_signals(b15, ticker)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True, help="CSV de tickers")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--strategy", default="trend_reversal_bb_15m", choices=list(STRATEGIES))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    strat = STRATEGIES[args.strategy]()
    tickers = [t.strip().upper() for t in args.ticker.split(",") if t.strip()]
    allsig = []
    for tk in tickers:
        sig = detect(tk, args.start, args.end, strat)
        print(f"{tk}: {len(sig)} señales")
        if not sig.empty:
            allsig.append(sig)
    res = pd.concat(allsig, ignore_index=True) if allsig else pd.DataFrame()
    if args.out and not res.empty:
        res.to_csv(args.out, index=False)
        print(f"-> {args.out} ({len(res)} filas)")
    elif not res.empty:
        print(res.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

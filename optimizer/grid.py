"""Grid search optimizer for the Trend Reversal strategy.

Structure of the outer/inner loops:
  Outer: params that affect signal computation (chandelier, macro SMA)
  Inner: params that only affect backtest (cooldown, reset distance)

This avoids recomputing expensive indicators for every combo.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from optimizer.data import load_all
from optimizer.strategy import StrategyParams, compute_signals, run_backtest

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "optimizer_results"
RESULTS_DIR.mkdir(exist_ok=True)


# Default grid — 5 x 3 x 3 x 3 x 3 = 405 combos per ticker
DEFAULT_GRID = {
    "trail_atr_mult":     [1.5, 2.0, 2.5, 3.0, 3.5],
    "trail_atr_len":      [14, 22, 30],
    "macro_sma_len":      [50, 100, 200],
    "cooldown_bars":      [5, 8, 12],
    "reset_distance_pct": [0.2, 0.3, 0.5],
}


def grid_combos(grid: dict) -> list[dict]:
    keys = list(grid.keys())
    vals = [grid[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*vals)]


def run_grid_for_ticker(
    ticker: str,
    grid: dict | None = None,
    *,
    base_params: dict | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """Run full grid on one ticker, return DataFrame of results."""
    grid = grid or DEFAULT_GRID
    base_params = base_params or {}
    combos = grid_combos(grid)

    data = load_all(ticker)
    df_1h, df_15m, df_1d = data["1H"], data["15m"], data["1D"]

    # Outer params (affect signals) vs inner params (backtest only)
    outer_keys = {"trail_atr_mult", "trail_atr_len", "macro_sma_len"}
    inner_keys = {"cooldown_bars", "reset_distance_pct"}

    # Group combos by outer-params so we reuse signals
    outer_values = itertools.product(
        grid["trail_atr_mult"], grid["trail_atr_len"], grid["macro_sma_len"]
    )
    rows: list[dict] = []

    total_outer = len(grid["trail_atr_mult"]) * len(grid["trail_atr_len"]) * len(grid["macro_sma_len"])
    iterator = tqdm(outer_values, total=total_outer, desc=f"{ticker}", disable=not verbose)

    for trail_mult, trail_len, macro_len in iterator:
        outer = {
            "trail_atr_mult": trail_mult,
            "trail_atr_len":  trail_len,
            "macro_sma_len":  macro_len,
        }
        p = StrategyParams(**{**base_params, **outer})
        signals = compute_signals(df_1h, df_15m, df_1d, p)

        for cd in grid["cooldown_bars"]:
            for rd in grid["reset_distance_pct"]:
                p2 = StrategyParams(**{
                    **base_params,
                    **outer,
                    "cooldown_bars": cd,
                    "reset_distance_pct": rd,
                })
                bt = run_backtest(signals, p2)
                m = bt.metrics()
                rows.append({
                    "ticker": ticker,
                    **outer,
                    "cooldown_bars": cd,
                    "reset_distance_pct": rd,
                    **m,
                })
    return pd.DataFrame(rows)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("tickers", nargs="+", help="tickers to optimize")
    parser.add_argument("--quick", action="store_true",
                        help="small grid for smoke test (27 combos)")
    parser.add_argument("--out", type=str, default=None,
                        help="output CSV path (default: optimizer_results/<ts>.csv)")
    args = parser.parse_args()

    grid = DEFAULT_GRID
    if args.quick:
        grid = {
            "trail_atr_mult":     [2.0, 2.5, 3.0],
            "trail_atr_len":      [22],
            "macro_sma_len":      [50],
            "cooldown_bars":      [5, 8, 12],
            "reset_distance_pct": [0.2, 0.3, 0.5],
        }

    all_rows: list[pd.DataFrame] = []
    t0 = time.time()
    for t in args.tickers:
        df = run_grid_for_ticker(t, grid)
        all_rows.append(df)
    combined = pd.concat(all_rows, ignore_index=True)
    elapsed = time.time() - t0
    print(f"\nFinished {len(combined)} backtests in {elapsed:.1f}s "
          f"({elapsed/len(combined)*1000:.1f} ms/backtest)")

    out_path = args.out or RESULTS_DIR / f"grid_{int(time.time())}.csv"
    combined.to_csv(out_path, index=False)
    print(f"Saved: {out_path}")

    # Top 5 per ticker by PF
    print("\n=== Top 5 combos per ticker (by Profit Factor) ===")
    for t, g in combined.groupby("ticker"):
        print(f"\n{t}:")
        top = g.sort_values("profit_factor", ascending=False).head(5)
        show = top[["trail_atr_mult", "trail_atr_len", "macro_sma_len",
                    "cooldown_bars", "reset_distance_pct",
                    "total_trades", "win_rate", "profit_factor", "net_profit",
                    "max_drawdown_pct"]]
        print(show.to_string(index=False))


if __name__ == "__main__":
    main()

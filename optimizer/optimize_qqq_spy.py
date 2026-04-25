"""Exhaustive grid search for QQQ and SPY with walk-forward validation.

Walk-forward setup (last 3 years, rolling from today):
  TRAIN:    bars from start to 2/3 of the window
  TEST:     bars from 2/3 of the window to today

For each grid combo we compute metrics on train AND test separately, then
keep configs that:
  - train PF > 1.1  AND  test PF > 1.0
  - train_trades >= 30  AND  test_trades >= 10
  - test net profit > 0

Final ranking: by test Sharpe (out-of-sample performance).

Grid (per ticker):
  trail_atr_mult        : 1.5 .. 4.0  (9 values)
  trail_atr_len         : 10, 14, 18, 22, 26
  macro_sma_len         : 30, 50, 100, 150, 200
  cooldown_bars         : 5, 8, 12, 15, 20
  cross_validity_window : 5, 8, 10, 15, 20
  → 9 * 5 * 5 * 5 * 5 = 5,625 combos / ticker
  → 11,250 total backtests on ~6,500 hourly bars
"""
from __future__ import annotations

import itertools
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

from optimizer.data import load_all
from optimizer.strategy import (
    StrategyParams,
    compute_signals,
    run_backtest,
    DEFAULT_LOOKBACK_YEARS,
)

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "optimizer_results"
RESULTS_DIR.mkdir(exist_ok=True)

GRID = {
    "trail_atr_mult":        [1.5, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 4.0],
    "trail_atr_len":         [10, 14, 18, 22, 26],
    "macro_sma_len":         [30, 50, 100, 150, 200],
    "cooldown_bars":         [5, 8, 12, 15, 20],
    "cross_validity_window": [5, 8, 10, 15, 20],
}

OUTER_KEYS = {"trail_atr_mult", "trail_atr_len", "macro_sma_len", "cross_validity_window"}
INNER_KEYS = {"cooldown_bars"}


def split_train_test(signals: pd.DataFrame, train_frac: float = 2/3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a signals DataFrame chronologically into train and test."""
    if len(signals) < 100:
        return signals, signals.iloc[0:0]
    cut = int(len(signals) * train_frac)
    return signals.iloc[:cut], signals.iloc[cut:]


def evaluate_combo(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_1d: pd.DataFrame,
    params: dict,
    train_frac: float = 2/3,
) -> dict:
    """Compute signals once, then run backtest on train and test slices."""
    p = StrategyParams(**params)
    signals = compute_signals(df_1h, df_15m, df_1d, p)
    train_sig, test_sig = split_train_test(signals, train_frac)

    train_bt = run_backtest(train_sig, p)
    test_bt  = run_backtest(test_sig,  p)

    tm = train_bt.metrics()
    te = test_bt.metrics()

    return {
        **params,
        "train_trades": tm["total_trades"],
        "train_pf":     tm["profit_factor"],
        "train_wr":     tm["win_rate"],
        "train_net":    tm["net_profit"],
        "train_sharpe": tm["sharpe"],
        "train_dd":     tm["max_drawdown_pct"],
        "test_trades":  te["total_trades"],
        "test_pf":      te["profit_factor"],
        "test_wr":      te["win_rate"],
        "test_net":     te["net_profit"],
        "test_sharpe":  te["sharpe"],
        "test_dd":      te["max_drawdown_pct"],
    }


def run_grid_for_ticker(ticker: str) -> pd.DataFrame:
    data = load_all(ticker)
    df_1h, df_15m, df_1d = data["1H"], data["15m"], data["1D"]

    # All combos
    keys = list(GRID.keys())
    vals = [GRID[k] for k in keys]
    combos = [dict(zip(keys, c)) for c in itertools.product(*vals)]

    results: list[dict] = []
    iterator = tqdm(combos, desc=f"{ticker}")
    for params in iterator:
        try:
            row = evaluate_combo(df_1h, df_15m, df_1d, params)
            row["ticker"] = ticker
            results.append(row)
        except Exception as e:
            # Skip configs that error (rare).
            results.append({**params, "ticker": ticker, "error": str(e)})

    return pd.DataFrame(results)


def filter_robust(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the robustness filter: must work in both train and test."""
    return df[
        (df["train_pf"] > 1.1) &
        (df["test_pf"]  > 1.0) &
        (df["train_trades"] >= 30) &
        (df["test_trades"]  >= 10) &
        (df["test_net"] > 0)
    ].copy()


def main():
    t0 = time.time()
    all_rows: list[pd.DataFrame] = []
    for ticker in ["QQQ", "SPY"]:
        df = run_grid_for_ticker(ticker)
        df["ticker"] = ticker
        all_rows.append(df)
    full = pd.concat(all_rows, ignore_index=True)
    elapsed = time.time() - t0

    out_csv = RESULTS_DIR / "qqq_spy_walkforward.csv"
    full.to_csv(out_csv, index=False)
    print(f"\nFinished {len(full)} backtests in {elapsed/60:.1f} min")
    print(f"Saved: {out_csv}")

    print("\n" + "=" * 80)
    print("ROBUST CONFIGS (passing the train+test filter), per ticker")
    print("=" * 80)
    cols = [
        "trail_atr_mult", "trail_atr_len", "macro_sma_len",
        "cooldown_bars", "cross_validity_window",
        "train_trades", "train_pf", "train_sharpe",
        "test_trades",  "test_pf",  "test_sharpe", "test_net",
    ]
    for ticker, g in full.groupby("ticker"):
        robust = filter_robust(g)
        print(f"\n--- {ticker}: {len(robust)} / {len(g)} pass robustness filter ---")
        if not robust.empty:
            top = robust.sort_values("test_sharpe", ascending=False).head(10)
            print(top[cols].to_string(index=False, float_format="%.3f"))


if __name__ == "__main__":
    main()

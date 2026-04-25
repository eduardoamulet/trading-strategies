"""Validate the Python port against known TradingView backtest results.

Known TradingView results (QQQ 1H, period 2014-2026, Chandelier 2.5x):
  Net Profit:  -$74.47
  PF:           0.921
  Win Rate:     30.52%
  W/L ratio:    2.10
  Trades:       308
  Max DD:       1.52%

Note: Alpaca data starts 2020-07 (only ~6 years vs 12y in TradingView).
We expect PF/WR/WL_ratio to be in the same ballpark;
total_trades and net_profit will scale with data length.
"""
from __future__ import annotations

import json

from optimizer.data import load_all
from optimizer.strategy import (
    StrategyParams,
    compute_signals,
    run_backtest,
)


def run_and_print(ticker: str, p: StrategyParams) -> None:
    data = load_all(ticker)
    signals = compute_signals(data["1H"], data["15m"], data["1D"], p)
    bt = run_backtest(signals, p)
    m = bt.metrics()

    print(f"\n=== {ticker}  Chandelier {p.trail_atr_mult}x  (Python port) ===")
    print(f"  Data range:    {signals.index.min()} -> {signals.index.max()}")
    print(f"  Total trades:  {m['total_trades']}")
    print(f"  Wins / Losses: {m['wins']} / {m['losses']}")
    print(f"  Win Rate:      {m['win_rate']:.2%}")
    print(f"  Avg Win:       ${m['avg_win']:.2f}")
    print(f"  Avg Loss:      ${m['avg_loss']:.2f}")
    print(f"  W/L ratio:     {m['win_loss_ratio']:.2f}")
    print(f"  Gross Profit:  ${m['gross_profit']:.2f}")
    print(f"  Gross Loss:    ${m['gross_loss']:.2f}")
    print(f"  Net Profit:    ${m['net_profit']:.2f}")
    print(f"  Commission:    ${m['commission_paid']:.2f}")
    print(f"  Profit Factor: {m['profit_factor']:.3f}")
    print(f"  Max DD:        {m['max_drawdown_pct']:.2%}")
    print(f"  Equity Final:  ${m['equity_final']:.2f}")


def main() -> None:
    # Match TradingView BACKTEST defaults that produced PF 0.921
    p = StrategyParams(
        bb_len=20,
        bb_mult=2.0,
        cooldown_bars=8,
        require_reset=True,
        reset_distance_pct=0.3,
        use_macro_trend=True,
        macro_sma_len=50,
        macro_slope_bars=5,
        use_trailing_stop=True,
        trail_atr_len=22,
        trail_atr_mult=2.5,
        enable_long=True,
        enable_short=False,
    )

    # Run QQQ (baseline) and TSLA for comparison
    for tk in ["QQQ", "TSLA"]:
        run_and_print(tk, p)

    print("\n--- TradingView baseline (for reference) ---")
    print("  QQQ 1H 2.5x (2014-2026): PF 0.921, WR 30.52%, W/L 2.10, Trades 308, Net -$74")
    print("  TSLA 4H 2.5x (2014-2026): PF 1.078 -- note: different TF, can't compare 1H")


if __name__ == "__main__":
    main()

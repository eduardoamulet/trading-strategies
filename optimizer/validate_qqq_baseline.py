"""Validate the Python port against the TradingView 1H QQQ baseline.

TradingView (4e064.xlsx, 1H, 3-year window):
  - chand 2.5 x ATR(10) | Macro D(200) | Cooldown 20 | CrossWindow 5 | LONG only
  - PF 1.064  Net +$36.11  133 trades  Max DD 1.59%  WR 39.85%

This script runs the Python port with the exact same config and reports
the gap. If the port is faithful, results should be within ~10% of TV.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from optimizer.data import load_all
from optimizer.strategy import (
    StrategyParams, compute_signals, run_backtest,
)


def fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def main() -> None:
    print("Loading QQQ data from cache...")
    data = load_all("QQQ")
    df_1h, df_15m, df_1d = data["1H"], data["15m"], data["1D"]
    print(f"  1H : {len(df_1h):>6} bars  {df_1h.index.min()} -> {df_1h.index.max()}")
    print(f"  15m: {len(df_15m):>6} bars  {df_15m.index.min()} -> {df_15m.index.max()}")
    print(f"  1D : {len(df_1d):>6} bars  {df_1d.index.min()} -> {df_1d.index.max()}")
    print()

    p = StrategyParams(
        # 1H BB
        bb_len=20,
        bb_mult=2.0,
        # 15m confirmation
        ltf_bb_len=20,
        ltf_slope_lookback=3,
        # Filters
        cooldown_bars=20,
        require_reset=True,
        cross_validity_window=5,
        # Macro filter (FORCED ON in BACKTEST)
        use_macro_trend=True,
        macro_sma_len=200,
        macro_slope_bars=5,
        # Trailing chandelier
        use_trailing_stop=True,
        trail_atr_len=10,
        trail_atr_mult=2.5,
        # Direction
        enable_long=True,
        enable_short=False,
    )
    print("Config:")
    print(f"  Chandelier  : {p.trail_atr_mult} x ATR({p.trail_atr_len})")
    print(f"  Macro SMA   : D({p.macro_sma_len})  slope_bars={p.macro_slope_bars}")
    print(f"  Cooldown    : {p.cooldown_bars} bars")
    print(f"  Cross window: {p.cross_validity_window} bars")
    print(f"  Direction   : LONG only")
    print()

    # Compute signals on last 3 years (matches the BACKTEST_LOOKBACK_YEARS=3 in Pine)
    print("Computing signals (3-year window)...")
    signals = compute_signals(df_1h, df_15m, df_1d, p, lookback_years=3)
    print(f"  Signal bars : {len(signals)}")
    print(f"  Range       : {signals.index.min()} -> {signals.index.max()}")
    raw_calls = int(signals["raw_call"].sum())
    print(f"  Raw CALLs   : {raw_calls}  (before cooldown/reset/macro)")
    print()

    print("Running backtest (initial $10,000, 10% size, 0.05% commission)...")
    result = run_backtest(
        signals, p,
        initial_capital=10_000.0,
        position_size_pct=0.10,
        commission_pct=0.0005,
    )
    m = result.metrics()

    # TradingView reference
    tv = {
        "trades": 133,
        "wins":   53,
        "losses": 80,
        "win_rate": 39.85,
        "net_profit": 36.11,
        "gross_profit": 603.17,
        "gross_loss":   567.07,
        "profit_factor": 1.064,
        "max_dd_pct": 1.59,   # close-to-close
        "avg_win": 11.38,
        "avg_loss": 7.09,
    }

    py = {
        "trades": m["total_trades"],
        "wins":   m["wins"],
        "losses": m["losses"],
        "win_rate": m["win_rate"] * 100,
        "net_profit": m["net_profit"],
        "gross_profit": m["gross_profit"],
        "gross_loss":   m["gross_loss"],
        "profit_factor": m["profit_factor"],
        "max_dd_pct": m["max_drawdown_pct"] * 100,
        "avg_win": m["avg_win"],
        "avg_loss": m["avg_loss"],
    }

    def diff_pct(py_v: float, tv_v: float) -> str:
        if tv_v == 0:
            return "n/a"
        d = (py_v - tv_v) / abs(tv_v) * 100
        return f"{d:+.1f}%"

    print()
    print("=" * 76)
    print(f"{'Metric':<22}{'Python':>15}{'TradingView':>15}{'Diff %':>12}")
    print("-" * 76)
    rows = [
        ("Total trades",      py["trades"],       tv["trades"],       "{:.0f}"),
        ("Wins",              py["wins"],         tv["wins"],         "{:.0f}"),
        ("Losses",            py["losses"],       tv["losses"],       "{:.0f}"),
        ("Win rate %",        py["win_rate"],     tv["win_rate"],     "{:.2f}"),
        ("Net Profit $",      py["net_profit"],   tv["net_profit"],   "{:.2f}"),
        ("Gross Profit $",    py["gross_profit"], tv["gross_profit"], "{:.2f}"),
        ("Gross Loss $",      py["gross_loss"],   tv["gross_loss"],   "{:.2f}"),
        ("Profit Factor",     py["profit_factor"],tv["profit_factor"],"{:.3f}"),
        ("Max DD %",          py["max_dd_pct"],   tv["max_dd_pct"],   "{:.2f}"),
        ("Avg win $",         py["avg_win"],      tv["avg_win"],      "{:.2f}"),
        ("Avg loss $",        py["avg_loss"],     tv["avg_loss"],     "{:.2f}"),
    ]
    for label, pv, tvv, fmt in rows:
        py_str = fmt.format(pv)
        tv_str = fmt.format(tvv)
        print(f"{label:<22}{py_str:>15}{tv_str:>15}{diff_pct(pv, tvv):>12}")
    print("=" * 76)
    print()

    # Verdict
    pf_diff = abs(py["profit_factor"] - tv["profit_factor"]) / tv["profit_factor"]
    trade_diff = abs(py["trades"] - tv["trades"]) / tv["trades"]
    print(f"PF gap         : {pf_diff*100:.1f}%   (target: <10%)")
    print(f"Trade-count gap: {trade_diff*100:.1f}%   (target: <15%)")
    if pf_diff < 0.10 and trade_diff < 0.15:
        print("\nVERDICT: Port is FAITHFUL.  Future grid-search predictions are trustworthy.")
    else:
        print("\nVERDICT: Port DEVIATES from TradingView.  Investigation needed.")
        print("Likely culprits to check (in order):")
        print("  - request.security alignment (lookahead, fill mode)")
        print("  - Chandelier stop semantics (Pine sets stop for NEXT bar, not current)")
        print("  - Cooldown semantics (>= vs >)")
        print("  - 15m confirmation slope direction (>=0 vs >0)")
        print("  - Initial position size: TV uses contracts (int), port uses fractional shares")
        print("  - Commission: TV charges on entry+exit. Port does too. Check value.")


if __name__ == "__main__":
    main()

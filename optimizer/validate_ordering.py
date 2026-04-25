"""Sensitivity test: QQQ 1H with chandelier 2.0, 2.5, 3.0, 4.0.

Known TradingView ordering (2014-2026):
  2.5x → PF 0.921  ← BEST
  3.0x → PF 0.870
  4.0x → PF 0.835

If Python reproduces this ordering (best at 2.5x, worst at 4.0x), the port is
behaving consistently with the Pine Script implementation.
"""
from __future__ import annotations

from optimizer.data import load_all
from optimizer.strategy import StrategyParams, compute_signals, run_backtest

TICKER = "QQQ"

# Cache data once
data = load_all(TICKER)

print(f"\nTicker: {TICKER}  |  Data: {data['1H'].index.min()} -> {data['1H'].index.max()}")
print(f"{'Chandelier':>11} {'Trades':>7} {'WinRate':>8} {'W/L':>6} {'PF':>7} {'MaxDD':>7} {'Net $':>10}")
print("-" * 65)

results = []
for mult in [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]:
    p = StrategyParams(trail_atr_mult=mult)
    signals = compute_signals(data["1H"], data["15m"], data["1D"], p)
    bt = run_backtest(signals, p)
    m = bt.metrics()
    print(f"{mult:>10.1f}x {m['total_trades']:>7d} "
          f"{m['win_rate']:>7.1%} "
          f"{m['win_loss_ratio']:>6.2f} "
          f"{m['profit_factor']:>7.3f} "
          f"{m['max_drawdown_pct']:>6.2%} "
          f"${m['net_profit']:>9.2f}")
    results.append((mult, m['profit_factor']))

best_mult, best_pf = max(results, key=lambda x: x[1])
print("-" * 65)
print(f"Best chandelier: {best_mult}x  (PF = {best_pf:.3f})")

print("\nTradingView baseline (2014-2026, longer period):")
print("  2.5x → PF 0.921 (best)")
print("  3.0x → PF 0.870")
print("  4.0x → PF 0.835")

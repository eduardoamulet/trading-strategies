"""Framework reutilizable de estrategias de trading (señales replicables Pine↔Python).

Cada estrategia implementa `base.Strategy.detect_signals(bars15m, ticker) -> DataFrame`,
y queda lista para el harness de validación cruzada (vs Pine export) y el backtesting
(options_replay/signals_backtest.run_one). Ver SPEC_*.md de cada estrategia.
"""

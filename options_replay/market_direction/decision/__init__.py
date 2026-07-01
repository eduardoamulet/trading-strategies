"""decision — del score a la decisión: `SignalGenerator` + `LevelCalculator` → `TradeSignal`.

Acá se aplican los umbrales (CALL>75 / PUT<25), el filtro de volatilidad ("gran movimiento"),
la confianza (via `Calibrator`) y los niveles entry/stop/target (R:R≥2, escalados por ATR).
"""
from .generator import SignalGenerator
from .levels import LevelCalculator

__all__ = ["SignalGenerator", "LevelCalculator"]

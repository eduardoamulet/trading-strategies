"""model — la costura de MODELO: score direccional 0–100 desde features + confirmación.

`DirectionModel` (interface) → `RuleBasedModel` (reglas, hoy). Mañana un modelo ML entra por la
misma interface sin tocar el engine.
"""
from .base import DirectionModel
from .rule_based import RuleBasedModel

__all__ = ["DirectionModel", "RuleBasedModel"]

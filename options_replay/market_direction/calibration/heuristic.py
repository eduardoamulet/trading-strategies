"""`HeuristicCalibrator` — confianza PROVISIONAL por promedio ponderado. NO está calibrada.

    confidence = w_f·fuerza + w_c·confirmación + w_k·condiciones      (recortado a [0,1])
      fuerza        = |score − 50| / 50          → distancia direccional del score
      confirmación  = Confirmation.factor        → 1.0 acuerda · 0.5 neutral · 0.0 contradice
      condiciones   = nº de razones / max_conditions

⚠️  El 0.80 que devuelve NO significa "acierta el 80% de las veces": es *calidad de setup*, no una
probabilidad. Es el bootstrap para tener algo funcionando ANTES del backtest. El entregable real de
confianza es `EmpiricalCalibrator` (tabla de hit-rate del backtest). Misma interface → swap directo.
"""
from __future__ import annotations

from typing import Optional

from ..domain import Confirmation, DirectionScore, FeatureSet


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


class HeuristicCalibrator:
    """Promedio ponderado fuerza/confirmación/condiciones. Provisional, no calibrado."""

    def __init__(self, w_strength: float = 0.40, w_confirm: float = 0.30,
                 w_conditions: float = 0.30, max_conditions: int = 8):
        self.w_strength = w_strength
        self.w_confirm = w_confirm
        self.w_conditions = w_conditions
        self.max_conditions = max_conditions

    def confidence(self, score: DirectionScore, features: Optional[FeatureSet] = None,
                   confirmation: Optional[Confirmation] = None) -> float:
        fuerza = abs(score.score - 50.0) / 50.0
        confirm = confirmation.factor if confirmation is not None else 0.5
        cond = min(1.0, len(score.reasons) / self.max_conditions) if score.reasons else 0.0
        return _clamp01(self.w_strength * fuerza + self.w_confirm * confirm + self.w_conditions * cond)

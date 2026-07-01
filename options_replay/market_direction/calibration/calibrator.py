"""`Calibrator` — la COSTURA de confianza: mapea un `DirectionScore` (+ contexto) a confianza 0–1.

Separar la *confianza* del *score* es deliberado: el score (0–100) mide la fuerza direccional del
setup; la confianza (0–1) debería medir la **probabilidad de acertar**. Hoy lo aproximamos con un
heurístico; mañana, con la tasa de acierto empírica del backtest; pasado, con la probabilidad
calibrada de un modelo ML. Las tres entran por ESTA misma interface, sin tocar el engine.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ..domain import Confirmation, DirectionScore, FeatureSet


@runtime_checkable
class Calibrator(Protocol):
    """Convierte un score direccional en una confianza 0–1."""

    def confidence(self, score: DirectionScore, features: FeatureSet,
                   confirmation: Optional[Confirmation] = None) -> float:
        ...

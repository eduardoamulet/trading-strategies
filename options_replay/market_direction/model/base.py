"""`DirectionModel` — la 2ª COSTURA swappable: convierte features (+ confirmación) en un score.

El engine depende de esta interface, no del `RuleBasedModel`. Mañana un `XGBoostModel`/`LightGBM`
implementa `predict(FeatureSet, Confirmation) → DirectionScore` y entra sin tocar nada más. El
contrato de entrada (`FeatureSet`) y salida (`DirectionScore`) es el mismo para reglas o ML.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ..domain import Confirmation, DirectionScore, FeatureSet


@runtime_checkable
class DirectionModel(Protocol):
    """Score direccional 0–100 (50 neutral) a partir de las features y la confirmación cross-asset."""

    def predict(self, features: FeatureSet,
                confirmation: Optional[Confirmation] = None) -> DirectionScore:
        ...

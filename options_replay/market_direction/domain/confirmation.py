"""`Confirmation` — resultado de la confirmación cross-asset (value object del dominio).

Lo PRODUCE la capa `confirmation/` (Módulo 4: SPY↔QQQ) y lo CONSUMEN el calibrador (para la
confianza) y el generador de señal. Es solo data (qué tan de acuerdo está el activo confirmador con
la dirección evaluada); la lógica de cómputo vive en la capa, no acá.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True, slots=True)
class Confirmation:
    """¿El activo confirmador (p.ej. QQQ al evaluar SPY) acompaña la dirección?

    agrees:   True acompaña · False contradice · None neutral / sin datos.
    strength: 0–1, qué tan fuerte es esa (des)confirmación.
    """
    agrees: Optional[bool] = None
    strength: float = 0.0
    confirmer: str = ""                       # ticker confirmador (ej. "QQQ")
    reasons: list[str] = field(default_factory=list)

    @property
    def factor(self) -> float:
        """Multiplicador para la confianza, en [0, 1]:
        acuerdo fuerte → ~1.0 · neutral → 0.5 · contradicción fuerte → ~0.0."""
        s = max(0.0, min(1.0, self.strength))
        if self.agrees is None:
            return 0.5
        return 0.5 + 0.5 * s if self.agrees else 0.5 - 0.5 * s

    @classmethod
    def neutral(cls, confirmer: str = "") -> "Confirmation":
        return cls(agrees=None, strength=0.0, confirmer=confirmer, reasons=["Sin confirmación"])

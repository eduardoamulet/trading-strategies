"""confirmation — confirmación cross-asset (SPY ancla + breadth QQQ/IWM), ponderada por correlación.

Produce el `Confirmation` del dominio que consumen el calibrador (confianza) y el generador de señal.
"""
from .cross_asset import CrossAssetConfirmation, read_direction

__all__ = ["CrossAssetConfirmation", "read_direction"]

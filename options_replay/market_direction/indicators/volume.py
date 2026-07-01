"""Indicadores de volumen — volumen promedio, volumen relativo, spike.

El promedio se toma sobre las barras PREVIAS (excluye la barra actual) para que el volumen relativo
mida "¿esta barra pesa más que lo normal reciente?". Funciones PURAS sobre `volumes` (velas ≤ t).
"""
from __future__ import annotations

import numpy as np

_NAN = float("nan")


def avg_volume(volumes, window: int = 20) -> float:
    """Promedio de las últimas `window` barras PREVIAS (sin la actual)."""
    v = np.asarray(volumes, float)
    if len(v) < 2:
        return _NAN
    prior = v[:-1][-window:]
    return float(prior.mean()) if len(prior) else _NAN


def relative_volume(volumes, window: int = 20) -> float:
    """volumen_actual / promedio_previo."""
    v = np.asarray(volumes, float)
    av = avg_volume(v, window)
    if not np.isfinite(av) or av == 0:
        return _NAN
    return float(v[-1] / av)


def volume_spike(volumes, window: int = 20, threshold: float = 1.5) -> bool:
    rv = relative_volume(volumes, window)
    return bool(np.isfinite(rv) and rv > threshold)

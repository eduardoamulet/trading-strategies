"""Indicadores de volatilidad — true range, ATR(14 Wilder), volatilidad realizada.

Funciones PURAS sobre arrays (velas ≤ t). Devuelven el valor en `t`. NaN si faltan datos.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_NAN = float("nan")


def _tr_array(highs, lows, closes) -> np.ndarray:
    """True Range por barra: max(h−l, |h−c_prev|, |l−c_prev|)."""
    h = np.asarray(highs, float); l = np.asarray(lows, float); c = np.asarray(closes, float)
    n = len(h)
    if n == 0:
        return np.array([])
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    if n > 1:
        pc = c[:-1]
        tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    return tr


def true_range(highs, lows, closes) -> float:
    tr = _tr_array(highs, lows, closes)
    return float(tr[-1]) if len(tr) else _NAN


def atr(highs, lows, closes, period: int = 14) -> float:
    """ATR de Wilder (EMA de α=1/period sobre el true range)."""
    tr = _tr_array(highs, lows, closes)
    if len(tr) < 2:
        return _NAN
    return float(pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])


def realized_vol(closes) -> float:
    """Desvío estándar de los retornos intradía (crudo, por barra)."""
    c = np.asarray(closes, float)
    if len(c) < 3:
        return _NAN
    r = pd.Series(c).pct_change().dropna()
    return float(r.std()) if len(r) else _NAN

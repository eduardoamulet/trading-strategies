"""Indicadores de estructura — opening range (breakout/breakdown/distancia) y swings (HH/HL/LH/LL).

Los niveles del OR los provee `Session.opening_range(n)`; acá se interpretan respecto al precio.
Funciones PURAS. NaN/False si faltan datos.
"""
from __future__ import annotations

import numpy as np

_NAN = float("nan")


def or_signals(close: float, or_high: float, or_low: float):
    """(breakout, breakdown, distancia_relativa_al_borde) respecto al opening range de referencia.

    distancia: >0 si rompió arriba (relativo a or_high), <0 si rompió abajo, 0 si está adentro.
    """
    if not (np.isfinite(close) and np.isfinite(or_high) and np.isfinite(or_low)):
        return False, False, _NAN
    if close > or_high:
        return True, False, float((close - or_high) / or_high) if or_high else _NAN
    if close < or_low:
        return False, True, float((close - or_low) / or_low) if or_low else _NAN
    return False, False, 0.0


def market_structure(highs, lows, lookback: int = 5):
    """(higher_high, higher_low, lower_high, lower_low) comparando la última vela con las `lookback`
    previas. HH/LL = ruptura del techo/piso reciente; HL/LH = respeto del piso/techo reciente."""
    h = np.asarray(highs, float); l = np.asarray(lows, float)
    if len(h) < lookback + 1:
        return False, False, False, False
    prev_h = h[-lookback - 1:-1]
    prev_l = l[-lookback - 1:-1]
    hh = bool(h[-1] > prev_h.max())
    ll = bool(l[-1] < prev_l.min())
    hl = bool(l[-1] > prev_l.min())     # el mínimo se sostiene sobre el piso reciente
    lh = bool(h[-1] < prev_h.max())     # el máximo queda por debajo del techo reciente
    return hh, hl, lh, ll

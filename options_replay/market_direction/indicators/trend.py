"""Indicadores de tendencia — EMA (9/20/50, pendientes, cruces) y VWAP (valor, pendiente, distancia).

Funciones PURAS sobre arrays (velas ≤ t). El VWAP es acumulativo desde la apertura (causal por
construcción). NaN si faltan datos.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_NAN = float("nan")


def ema_series(closes, span: int) -> pd.Series:
    return pd.Series(np.asarray(closes, float)).ewm(span=span, adjust=False).mean()


def ema(closes, span: int) -> float:
    c = np.asarray(closes, float)
    if len(c) == 0:
        return _NAN
    return float(ema_series(c, span).iloc[-1])


def ema_slope(closes, span: int, lookback: int = 3) -> float:
    """Δ de la EMA en las últimas `lookback` barras (pendiente reciente)."""
    s = ema_series(closes, span)
    if len(s) <= lookback:
        return _NAN
    return float(s.iloc[-1] - s.iloc[-1 - lookback])


def vwap_series(highs, lows, closes, volumes) -> np.ndarray:
    """VWAP acumulativo (precio típico ponderado por volumen desde la apertura)."""
    h = np.asarray(highs, float); l = np.asarray(lows, float); c = np.asarray(closes, float)
    v = np.asarray(volumes, float)
    tp = (h + l + c) / 3.0
    cv = np.cumsum(v)
    cpv = np.cumsum(tp * v)
    out = np.full(len(v), _NAN)
    nz = cv > 0
    out[nz] = cpv[nz] / cv[nz]
    return out


def vwap(highs, lows, closes, volumes) -> float:
    s = vwap_series(highs, lows, closes, volumes)
    return float(s[-1]) if len(s) else _NAN


def vwap_slope(highs, lows, closes, volumes, lookback: int = 5) -> float:
    s = vwap_series(highs, lows, closes, volumes)
    if len(s) <= lookback or not np.isfinite(s[-1]) or not np.isfinite(s[-1 - lookback]):
        return _NAN
    return float(s[-1] - s[-1 - lookback])

"""Indicadores de momentum — RSI(14), ROC, momentum, MACD (línea, señal, histograma, pendiente).

Funciones PURAS sobre `closes` (velas ≤ t). NaN si faltan datos.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_NAN = float("nan")


def momentum(closes, n: int = 10) -> float:
    """close[t] − close[t−n] (recorrido absoluto de las últimas n barras)."""
    c = np.asarray(closes, float)
    if len(c) <= n:
        return _NAN
    return float(c[-1] - c[-1 - n])


def roc(closes, n: int = 10) -> float:
    """Rate of change % en n barras."""
    c = np.asarray(closes, float)
    if len(c) <= n or c[-1 - n] == 0:
        return _NAN
    return float((c[-1] - c[-1 - n]) / c[-1 - n] * 100.0)


def rsi(closes, period: int = 14) -> float:
    """RSI de Wilder."""
    c = np.asarray(closes, float)
    if len(c) <= period:
        return _NAN
    d = pd.Series(c).diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    ad = dn.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    if ad == 0:
        return 100.0
    rs = au / ad
    return float(100.0 - 100.0 / (1.0 + rs))


def macd(closes, fast: int = 12, slow: int = 26, signal: int = 9):
    """(macd, señal, histograma, pendiente del histograma). NaN-tuple si faltan datos."""
    c = np.asarray(closes, float)
    if len(c) < slow:
        return _NAN, _NAN, _NAN, _NAN
    s = pd.Series(c)
    line = s.ewm(span=fast, adjust=False).mean() - s.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    slope = float(hist.iloc[-1] - hist.iloc[-2]) if len(hist) >= 2 else _NAN
    return float(line.iloc[-1]), float(sig.iloc[-1]), float(hist.iloc[-1]), slope

"""Indicadores compartidos, replicando la semántica de Pine Script v6.

Decisiones de PARIDAD con Pine (críticas para que las señales coincidan):
- `ta.sma`  -> media móvil simple.
- `ta.stdev`-> desviación estándar de POBLACIÓN (ddof=0), igual que Pine.
- `ta.atr`  -> RMA (Wilder) del True Range. Aproximado con ewm(alpha=1/len, adjust=False).
- `ta.pivothigh/low(L,R)` -> el bar i es pivot si su high/low es ESTRICTAMENTE el extremo
  de la ventana [i-L, i+R]; el pivot se "confirma" R barras después (como en Pine).
- Remuestreo a 15m de SOLO horario regular (RTH 09:30–16:00 ET), alineado a 09:30,
  igual que un chart de TradingView en horario regular.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RTH_START_MIN = 9 * 60 + 30   # 09:30
RTH_END_MIN   = 16 * 60       # 16:00 (exclusivo; el bar 15:45 cierra a 16:00)


# ─────────────────────────────────────────────────────────────────────────────
# Remuestreo 1-min -> 15-min RTH
# ─────────────────────────────────────────────────────────────────────────────
def to_15m_rth(df1: pd.DataFrame) -> pd.DataFrame:
    """1-min OHLCV (tz-aware ET) -> 15-min RTH alineado a 09:30. Devuelve columnas
    timestamp/open/high/low/close/volume + session_date + is_opening (bool)."""
    s = df1.copy()
    ts = pd.to_datetime(s["timestamp"] if "timestamp" in s.columns else s.index)
    s = s.set_index(pd.DatetimeIndex(ts)).sort_index()
    if s.index.tz is None:
        s.index = s.index.tz_localize("America/New_York")
    else:
        s.index = s.index.tz_convert("America/New_York")

    mins = s.index.hour * 60 + s.index.minute
    rth = (mins >= RTH_START_MIN) & (mins < RTH_END_MIN)
    s = s[rth]
    if s.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close",
                                     "volume", "session_date", "is_opening"])

    binid = ((s.index.hour * 60 + s.index.minute) - RTH_START_MIN) // 15   # 0..25
    day = s.index.normalize()
    grp = s.groupby([day, binid])
    out = pd.DataFrame({
        "open":   grp["open"].first(),
        "high":   grp["high"].max(),
        "low":    grp["low"].min(),
        "close":  grp["close"].last(),
        "volume": grp["volume"].sum(),
    })
    out.index.set_names(["session_date", "binid"], inplace=True)
    out = out.reset_index()
    out["timestamp"] = out["session_date"] + pd.to_timedelta(
        RTH_START_MIN + out["binid"] * 15, unit="m")
    out["is_opening"] = out["binid"] == 0
    out["session_date"] = out["session_date"].dt.date
    out = out.sort_values("timestamp").reset_index(drop=True)
    return out[["timestamp", "open", "high", "low", "close", "volume",
                "session_date", "is_opening"]]


# ─────────────────────────────────────────────────────────────────────────────
# Bollinger / ATR
# ─────────────────────────────────────────────────────────────────────────────
def bollinger(close: pd.Series, length: int = 20, mult: float = 2.0):
    basis = close.rolling(length).mean()
    sd = close.rolling(length).std(ddof=0)            # POBLACIÓN, como Pine
    dev = mult * sd
    return basis, basis + dev, basis - dev


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    pc = close.shift(1)
    tr = pd.concat([(high - low), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False).mean()   # RMA (Wilder)


# ─────────────────────────────────────────────────────────────────────────────
# Pivots (ta.pivothigh / ta.pivotlow)
# ─────────────────────────────────────────────────────────────────────────────
def pivot_high_bars(high: pd.Series, left: int = 3, right: int = 3) -> np.ndarray:
    """Bool array: True en el bar i si high[i] es ESTRICTAMENTE el máximo de [i-left, i+right]."""
    a = high.to_numpy(dtype=float)
    n = len(a)
    out = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        v = a[i]
        if v > a[i - left:i].max() and v > a[i + 1:i + right + 1].max():
            out[i] = True
    return out


def pivot_low_bars(low: pd.Series, left: int = 3, right: int = 3) -> np.ndarray:
    a = low.to_numpy(dtype=float)
    n = len(a)
    out = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        v = a[i]
        if v < a[i - left:i].min() and v < a[i + 1:i + right + 1].min():
            out[i] = True
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Trendline multi-punto (mismo hull que el Pine)
# ─────────────────────────────────────────────────────────────────────────────
def hull_line(pivots: list[tuple[int, float]], kind: str):
    """pivots = [(bar, price)] dentro de la ventana. kind='res' (CALL) usa ancla=MÁS ALTO y
    pendiente=máx razón (upper hull, debe ser <0); 'sup' (PUT) ancla=MÁS BAJO y pendiente=mín
    (lower hull, >0). Devuelve (anchorBar, anchorPrice, slope) o None.
    Replica EXACTO el .pine: recorre todos y filtra db>0 (evita el for inverso)."""
    n = len(pivots)
    if n < 2:
        return None
    if kind == "res":
        ai = max(range(n), key=lambda i: pivots[i][1])
    else:
        ai = min(range(n), key=lambda i: pivots[i][1])
    a_bar, a_price = pivots[ai]
    best = None
    for k in range(n):                       # 0..n-1 (siempre adelante)
        db = pivots[k][0] - a_bar
        if db > 0:                           # sólo pivots POSTERIORES al ancla
            r = (pivots[k][1] - a_price) / db
            if best is None:
                best = r
            elif kind == "res" and r > best:
                best = r
            elif kind == "sup" and r < best:
                best = r
    if best is None:
        return None
    if kind == "res" and best >= 0:
        return None
    if kind == "sup" and best <= 0:
        return None
    return a_bar, a_price, best


# ─────────────────────────────────────────────────────────────────────────────
# Rango del día anterior (prevHigh/prevLow/prevMidpoint)
# ─────────────────────────────────────────────────────────────────────────────
def prev_day_levels(bars15: pd.DataFrame, midpoint_pct: float = 0.5) -> pd.DataFrame:
    """Por cada session_date, calcula el high/low RTH del DÍA ANTERIOR y su midpoint."""
    daily = bars15.groupby("session_date").agg(dh=("high", "max"), dl=("low", "min"))
    daily["prevHigh"] = daily["dh"].shift(1)
    daily["prevLow"] = daily["dl"].shift(1)
    daily["prevMidpoint"] = daily["prevLow"] + (daily["prevHigh"] - daily["prevLow"]) * midpoint_pct
    return daily[["prevHigh", "prevLow", "prevMidpoint"]]

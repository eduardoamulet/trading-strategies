"""Markov 2.0 — evaluación de régimen de mercado (corrected).

Mismo método que el skill `/markov-2-hedge-fund-method`, empaquetado como módulo de
la app para el panel de Backtesting: estados (BULL/BEAR/SIDEWAYS por retorno de 20
barras) → matriz de transición OVERLAPPING (legacy) + STRIDE (honesta, ventanas no
solapadas) → señal = P(bull)−P(bear) de la fila del estado actual → veredicto de edge.

Evalúa SIEMPRE "as of" una fecha: la matriz se entrena solo con datos HASTA esa fecha
(sin mirar el futuro). Datos diarios vía el adapter de Polygon (key en config.py).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SIDEWAYS, BULL, BEAR = 0, 1, 2
STATE_NAMES = {SIDEWAYS: "SIDEWAYS", BULL: "BULL", BEAR: "BEAR", -1: "—"}
_HERE = Path(__file__).resolve().parent


# ─────────────────────────────────────────────────────── data
def fetch_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    """OHLCV diario [start, end] vía el adapter de Polygon. Cacheado en data/daily/."""
    import config
    from adapter_polygon import PolygonAdapter, _aggs_ticker
    cache = _HERE / "data" / "daily"
    cache.mkdir(parents=True, exist_ok=True)
    fp = cache / f"{ticker.upper()}_{start}_{end}.parquet"
    if fp.exists():
        return pd.read_parquet(fp)
    ad = PolygonAdapter(config.POLYGON_API_KEY)
    path = f"/v2/aggs/ticker/{_aggs_ticker(ticker)}/range/1/day/{start}/{end}"
    data = ad._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
    df = PolygonAdapter._bars_to_df(data.get("results", []))
    if not df.empty:
        df.to_parquet(fp)
    return df


# ─────────────────────────────────────────────────────── core
def label_states(close: pd.Series, window: int = 20,
                 up: float = 0.05, down: float = -0.05) -> pd.Series:
    """Retorno acumulado de `window` barras → 0=SIDEWAYS, 1=BULL, 2=BEAR (−1 = sin lookback)."""
    ret = close.pct_change(window)
    st = pd.Series(np.where(ret >= up, BULL, np.where(ret <= down, BEAR, SIDEWAYS)),
                   index=close.index, dtype=int)
    st[ret.isna()] = -1
    return st


def transition_counts(states: pd.Series, stride: int, n: int = 3) -> np.ndarray:
    """Cuenta s[t] → s[t+stride]. stride=1 → overlapping (días consecutivos, autocorrelado);
    stride=window → cada transición liga dos ventanas NO solapadas (honesta)."""
    s = np.asarray(states.values)
    C = np.zeros((n, n), float)
    for t in range(len(s) - stride):
        a, b = s[t], s[t + stride]
        if a >= 0 and b >= 0:
            C[int(a), int(b)] += 1.0
    return C


def to_matrix(C: np.ndarray) -> np.ndarray:
    row = C.sum(axis=1, keepdims=True)
    return np.divide(C, row, out=np.full_like(C, np.nan), where=row > 0)


def stationary(P: np.ndarray) -> np.ndarray:
    M = np.nan_to_num(P)
    vals, vecs = np.linalg.eig(M.T)
    i = int(np.argmin(np.abs(vals - 1.0)))
    v = np.abs(np.real(vecs[:, i]))
    return v / v.sum() if v.sum() else v


def signal_from(P: np.ndarray, state: int) -> float:
    """Señal = P(bull) − P(bear) desde la fila del estado actual. NaN si nunca se vio."""
    if state < 0 or np.isnan(P[state]).any():
        return float("nan")
    return float(P[state, BULL] - P[state, BEAR])


# ─────────────────────────────────────────────────────── evaluación as-of
def evaluate(ticker: str, asof_date, window: int = 20,
             lookback_years: int = 11, edge_thr: float = 0.10) -> dict:
    """Evalúa el régimen de `ticker` AL `asof_date` (sin datos del futuro).
    Devuelve ambas matrices, el estado actual, su retorno de `window` barras, la señal
    y un veredicto de edge. `edge_thr` = |señal| mínima para declarar dirección."""
    asof = pd.Timestamp(asof_date).date()
    start = (pd.Timestamp(asof) - pd.DateOffset(years=lookback_years)).date().isoformat()
    df = fetch_daily(ticker, start, asof.isoformat())
    if df.empty:
        return {"ok": False, "error": f"sin datos diarios de {ticker} hasta {asof}"}
    df = df[df["timestamp"].dt.date <= asof].reset_index(drop=True)
    if len(df) <= window + 5:
        return {"ok": False, "error": f"datos insuficientes ({len(df)} barras) para ventana {window}"}

    close = df["close"]
    states = label_states(close, window)
    P_over = to_matrix(transition_counts(states, 1))
    P_stride = to_matrix(transition_counts(states, window))
    cur = int(states.iloc[-1])
    ret_w = float(close.iloc[-1] / close.iloc[-1 - window] - 1.0) if len(close) > window else float("nan")
    sig = signal_from(P_stride, cur)
    pi = stationary(P_stride)

    if cur < 0 or sig != sig:
        verdict = ("—", "datos insuficientes")
    elif abs(sig) < edge_thr:
        verdict = ("NO EDGE", "stay flat")
    elif sig > 0:
        verdict = ("BULLISH", "lean long")
    else:
        verdict = ("BEARISH", "lean short")

    return {"ok": True, "ticker": ticker.upper(), "asof": asof.isoformat(),
            "last_date": str(df["timestamp"].iloc[-1].date()), "n_bars": len(df),
            "window": window, "P_over": P_over, "P_stride": P_stride,
            "state": cur, "state_name": STATE_NAMES[cur], "ret_window": ret_w,
            "signal": sig, "stationary": pi, "verdict": verdict, "edge_thr": edge_thr}

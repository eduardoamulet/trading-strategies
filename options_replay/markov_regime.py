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
    # Tendencia 0–100 (0=BEAR · 50=SIDEWAYS · 100=BULL) desde el retorno de `window`
    # barras: ±5% (bordes de régimen) → 25/75, ±10% → 0/100, plano → 50.
    tendencia = float(np.clip(50.0 + ret_w * 500.0, 0.0, 100.0)) if ret_w == ret_w else 50.0

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
            "signal": sig, "stationary": pi, "verdict": verdict, "edge_thr": edge_thr,
            "tendencia": tendencia}


# ─────────────────────────────────────────────── HMM (Hidden Markov) — auditoría
def fit_gaussian_hmm(x: np.ndarray, n_states: int = 3, n_iter: int = 120,
                     tol: float = 1e-4, seed: int = 0) -> dict:
    """HMM gaussiano univariado vía Baum-Welch (forward-backward ESCALADO). Sin
    dependencias externas (solo numpy) — aprende los regímenes SIN etiquetas a mano.
    Devuelve medias/varianzas/A/π, los posteriors γ y el camino de Viterbi."""
    x = np.asarray(x, float)
    T, K = len(x), n_states
    mu = np.quantile(x, np.linspace(0.15, 0.85, K)).astype(float)
    var = np.full(K, max(float(np.var(x)), 1e-8))
    A = np.full((K, K), 1.0 / K)
    pi = np.full(K, 1.0 / K)
    prev, B = -np.inf, np.empty((T, K))
    for _ in range(n_iter):
        for i in range(K):
            B[:, i] = np.exp(-0.5 * (x - mu[i]) ** 2 / var[i]) / np.sqrt(2 * np.pi * var[i])
        np.clip(B, 1e-300, None, out=B)
        alpha = np.empty((T, K)); c = np.empty(T)
        alpha[0] = pi * B[0]; c[0] = alpha[0].sum(); alpha[0] /= c[0]
        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ A) * B[t]
            c[t] = alpha[t].sum(); alpha[t] /= c[t]
        ll = float(np.log(c).sum())
        beta = np.empty((T, K)); beta[-1] = 1.0
        for t in range(T - 2, -1, -1):
            beta[t] = (A @ (B[t + 1] * beta[t + 1])) / c[t + 1]
        gamma = alpha * beta
        gamma /= gamma.sum(axis=1, keepdims=True)
        xis = np.zeros((K, K))
        for t in range(T - 1):
            xis += (alpha[t][:, None] * A * (B[t + 1] * beta[t + 1])[None, :]) / c[t + 1]
        pi = gamma[0]
        A = xis / xis.sum(axis=1, keepdims=True)
        for i in range(K):
            w = gamma[:, i]; sw = w.sum() + 1e-12
            mu[i] = float((w * x).sum() / sw)
            var[i] = max(float((w * (x - mu[i]) ** 2).sum() / sw), 1e-8)
        if abs(ll - prev) < tol:
            break
        prev = ll
    lB = np.log(np.clip(B, 1e-300, None)); lA = np.log(np.clip(A, 1e-300, None))
    d = np.log(np.clip(pi, 1e-300, None)) + lB[0]
    psi = np.zeros((T, K), int)
    for t in range(1, T):
        m = d[:, None] + lA
        psi[t] = m.argmax(axis=0); d = m.max(axis=0) + lB[t]
    path = np.zeros(T, int); path[-1] = int(d.argmax())
    for t in range(T - 2, -1, -1):
        path[t] = psi[t + 1, path[t + 1]]
    return {"means": mu, "vars": var, "A": A, "pi": pi, "gamma": gamma, "path": path}


def hmm_audit(close: pd.Series, window: int = 20, seed: int = 0) -> dict:
    """Audita las etiquetas por UMBRAL con un HMM de 3 estados sobre el retorno de
    `window` barras (sin etiquetas a mano). Mapea estado→régimen por la media de emisión
    (menor=BEAR, mayor=BULL) y reporta % de acuerdo + matriz de confusión vs el umbral,
    más la tendencia HMM (del posterior del último día). Acuerdo alto = luz verde."""
    r = close.pct_change(window).dropna()
    if len(r) < 100:
        return {"ok": False, "error": "datos insuficientes para el HMM"}
    H = fit_gaussian_hmm(r.values, n_states=3, seed=seed)
    order = [int(k) for k in np.argsort(H["means"])]       # menor media→BEAR, mayor→BULL
    s2r = {order[0]: BEAR, order[1]: SIDEWAYS, order[2]: BULL}
    hmm_reg = np.array([s2r[int(s)] for s in H["path"]])
    th = label_states(close, window).loc[r.index].values
    valid = th >= 0
    agree = float((hmm_reg[valid] == th[valid]).mean()) if valid.any() else float("nan")
    disp = [BEAR, SIDEWAYS, BULL]
    conf = np.zeros((3, 3), int)
    for a, b in zip(th[valid], hmm_reg[valid]):
        conf[disp.index(int(a)), disp.index(int(b))] += 1
    g = H["gamma"][-1]
    p_bear, p_side, p_bull = float(g[order[0]]), float(g[order[1]]), float(g[order[2]])
    return {"ok": True, "agreement": agree, "confusion": conf,
            "means_pct": [round(float(H["means"][k]) * 100, 1) for k in order],
            "hmm_tendencia": float(100.0 * p_bull + 50.0 * p_side),
            "hmm_state": STATE_NAMES[s2r[int(H["path"][-1])]],
            "post": {"BEAR": p_bear, "SIDEWAYS": p_side, "BULL": p_bull}}


def hmm_audit_asof(ticker: str, asof_date, window: int = 20,
                   lookback_years: int = 11, seed: int = 0) -> dict:
    """Conveniencia para el panel: trae diario HASTA `asof_date` (sin futuro) y audita."""
    asof = pd.Timestamp(asof_date).date()
    start = (pd.Timestamp(asof) - pd.DateOffset(years=lookback_years)).date().isoformat()
    df = fetch_daily(ticker, start, asof.isoformat())
    if df.empty:
        return {"ok": False, "error": f"sin datos diarios de {ticker}"}
    df = df[df["timestamp"].dt.date <= asof].reset_index(drop=True)
    return hmm_audit(df["close"], window=window, seed=seed)

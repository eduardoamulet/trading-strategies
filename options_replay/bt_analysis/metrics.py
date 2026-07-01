"""Métricas por escenario + Robustness Score (0–100).

A nivel «scenario» (agregado) los datos disponibles son: Inversión, Ganancia, ROI mín/máx/prom (%/$),
#ROI>0, #ROI<=0, #err. Con eso se computan win-rate, esperanza (ROI prom), drawdown proxy (peor
corrida), error-rate y un Profit-Factor PROXY (marcado: sin las sumas por-corrida no es el PF exacto;
con datos «detailed» se calcula el real). El Robustness Score pondera esos factores en 0–100.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _clip01(x):
    return np.clip(x, 0.0, 1.0)


def add_scenario_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Añade columnas de métricas por escenario. No muta el input."""
    out = df.copy()
    npos = out["n_pos"].fillna(0.0)
    nneg = out["n_neg"].fillna(0.0)
    nerr = out["n_err"].fillna(0.0)
    n = npos + nneg
    out["n_trades"] = n
    out["win_rate"] = np.where(n > 0, npos / n * 100.0, np.nan)
    out["roi"] = out["roi_avg_pct"]              # ROI promedio por corrida = esperanza matemática
    out["expectancy"] = out["roi_avg_pct"]
    out["dd"] = out["roi_min_pct"]               # peor corrida (proxy de drawdown, negativo)
    out["best"] = out["roi_max_pct"]
    out["net"] = out["ganancia"]
    out["error_rate"] = np.where((n + nerr) > 0, nerr / (n + nerr) * 100.0, np.nan)
    # Profit Factor PROXY (crudo): magnitud win-side vs loss-side ponderada por win-rate.
    wr = out["win_rate"].fillna(0.0) / 100.0
    win_side = wr * out["roi_max_pct"].abs().fillna(0.0)
    loss_side = (1.0 - wr) * out["roi_min_pct"].abs().fillna(0.0)
    out["pf_proxy"] = win_side / loss_side.replace(0.0, np.nan)
    return out


# ── Robustness Score 0–100 ──────────────────────────────────────────────────────────────────────
# Sub-scores normalizados a [0,1] y ponderados. A nivel «scenario» no hay consistencia temporal ni
# cross-asset por-fila (el día/ticker están agregados) → esas dimensiones se cubren con el file DOW
# si está (ver dow.py), y acá se marca coverage="scenario".
_WEIGHTS = {"roi": 0.28, "wr": 0.22, "dd": 0.22, "pf": 0.14, "n": 0.14}

TIERS = [(90, "Excelente"), (75, "Muy Bueno"), (60, "Aceptable"), (40, "Débil"), (0, "Descartar")]


def _subscores(row) -> dict:
    roi = float(row.get("roi") or 0.0)
    wr = float(row.get("win_rate") or 0.0)
    dd = float(row.get("dd") or 0.0)
    pf = row.get("pf_proxy")
    pf = float(pf) if pf is not None and not pd.isna(pf) else 0.0
    n = float(row.get("n_trades") or 0.0)
    return {
        "roi": float(_clip01(0.5 + roi / 30.0)),        # 0%→.5 · +15%→1 · −15%→0
        "wr": float(_clip01((wr - 40.0) / 45.0)),        # 40%→0 · 85%→1
        "dd": float(_clip01(1.0 + dd / 60.0)),           # 0→1 · −60%→0 (peor corrida)
        "pf": float(_clip01((pf - 0.8) / 1.7)),          # 0.8→0 · 2.5→1
        "n": float(_clip01(n / 150.0)),                  # nº de corridas → confianza
    }


def tier_for(score: float) -> str:
    for thr, name in TIERS:
        if score >= thr:
            return name
    return "Descartar"


def add_robustness(df: pd.DataFrame) -> pd.DataFrame:
    """Añade `robustness` (0–100), `tier` y las columnas de sub-scores (auditable)."""
    out = df.copy()
    subs = out.apply(_subscores, axis=1, result_type="expand")
    subs.columns = [f"rs_{c}" for c in subs.columns]
    score = sum(_WEIGHTS[k] * subs[f"rs_{k}"] for k in _WEIGHTS) * 100.0
    out = pd.concat([out, subs], axis=1)
    out["robustness"] = score.round(1)
    out["tier"] = out["robustness"].apply(tier_for)
    return out

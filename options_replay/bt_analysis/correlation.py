"""Correlación CONDICIÓN-de-escenario ↔ outcome (Pearson / Spearman / Kendall).

Responde «¿qué condiciones de salida mueven el ROI / win-rate / drawdown?». Codifica las condiciones
categóricas (Sí/No → 1/0, texto → códigos) y correlaciona contra cada outcome. Descarta las
condiciones sin variación (constantes en los 480). Ordena por |Spearman| (monótona, robusta a outliers).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_PARAM_COLS = ["refuerzo", "refuerzo_umbral", "refuerzo_n", "sin_lookahead", "alcance",
               "ticker_roi_on", "ticker_roi", "ticker_stop_on", "ticker_stop", "filtro_confirmacion",
               "cerrar_confirmacion_debil", "cuerpo_min", "col_roi_on", "col_roi", "col_stop_on",
               "col_stop"]
_TARGETS = [("roi", "ROI promedio"), ("win_rate", "Win Rate"),
            ("net", "Ganancia neta"), ("dd", "Drawdown")]


def encode(s: pd.Series) -> pd.Series:
    """Serie → numérica (Sí/No→1/0; texto→códigos; numérica se deja)."""
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce")
    low = s.astype(str).str.strip().str.lower()
    m = {"sí": 1, "si": 1, "no": 0, "true": 1, "false": 0}
    if low.dropna().isin(m).all():
        return low.map(m)
    codes = low.astype("category").cat.codes
    return codes.where(codes >= 0, np.nan)


def correlations(joined: pd.DataFrame) -> pd.DataFrame:
    """DataFrame tidy: param · target · pearson · spearman · kendall · n. Ordenado por |spearman|."""
    rows = []
    for p in [c for c in _PARAM_COLS if c in joined.columns]:
        x = encode(joined[p])
        if x.nunique(dropna=True) < 2:          # condición constante → no correlaciona
            continue
        for tkey, tlabel in _TARGETS:
            if tkey not in joined.columns:
                continue
            y = pd.to_numeric(joined[tkey], errors="coerce")
            d = pd.DataFrame({"x": x, "y": y}).dropna()
            if len(d) < 5 or d["x"].nunique() < 2 or d["y"].nunique() < 2:
                continue
            rows.append({
                "param": p, "target": tlabel,
                "pearson": round(float(d["x"].corr(d["y"], method="pearson")), 3),
                "spearman": round(float(d["x"].corr(d["y"], method="spearman")), 3),
                "kendall": round(float(d["x"].corr(d["y"], method="kendall")), 3),
                "n": int(len(d)),
            })
    res = pd.DataFrame(rows, columns=["param", "target", "pearson", "spearman", "kendall", "n"])
    if not res.empty:
        res = (res.assign(_a=res["spearman"].abs())
               .sort_values("_a", ascending=False).drop(columns="_a").reset_index(drop=True))
    return res

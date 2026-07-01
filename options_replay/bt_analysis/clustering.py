"""Clustering de escenarios por su perfil de OUTCOME (KMeans) → familias de comportamiento.

k se elige por silhouette (2..6). Devuelve etiquetas + resumen por cluster con un perfil descriptivo.
"""
from __future__ import annotations

import pandas as pd

_FEATS = ["roi", "win_rate", "dd", "pf_proxy", "net", "n_trades"]


def _profile(r) -> str:
    roi, wr = float(r.get("roi", 0) or 0), float(r.get("win_rate", 0) or 0)
    if roi > 3 and wr > 60:
        return "Robusto (ROI+ · WR alto)"
    if roi > 0:
        return "Positivo moderado"
    if roi < -3:
        return "Perdedor"
    return "Neutro / mixto"


def cluster_scenarios(df: pd.DataFrame, k: int | None = None) -> dict:
    """Agrupa por perfil de outcome. {k, labels, ids, summary(df), assigned(df)} o {error}."""
    feats = [c for c in _FEATS if c in df.columns]
    if len(df) < 6 or len(feats) < 3:
        return {"error": "muy pocos escenarios/columnas para clustering"}
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    X = df[feats].apply(pd.to_numeric, errors="coerce")
    X = X.fillna(X.median(numeric_only=True))
    Xs = StandardScaler().fit_transform(X)
    if k is None:
        best_k, best_s = 3, -1.0
        for kk in range(2, min(7, len(df))):
            lab = KMeans(n_clusters=kk, n_init=10, random_state=42).fit_predict(Xs)
            if len(set(lab)) < 2:
                continue
            s = silhouette_score(Xs, lab)
            if s > best_s:
                best_k, best_s = kk, s
        k = best_k
    labels = KMeans(n_clusters=k, n_init=10, random_state=42).fit_predict(Xs)
    d = df.copy()
    d["cluster"] = labels
    summary = d.groupby("cluster")[feats].mean().round(2)
    summary["n"] = d.groupby("cluster").size()
    summary["perfil"] = summary.apply(_profile, axis=1)
    return {
        "k": int(k), "labels": labels.tolist(), "ids": d["id"].tolist(),
        "summary": summary.reset_index(),
        "assigned": d[["id", "cluster", "roi", "win_rate", "robustness"]].reset_index(drop=True),
    }

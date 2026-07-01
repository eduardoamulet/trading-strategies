"""Significancia estadística — ¿las diferencias son producto del azar? Tests honestos + caveats.

Regla del PRD: si no hay ventaja estadística suficiente, decirlo (NO OPERAR). Y ser honesto sobre la
NO independencia de las corridas (mismo período, 3 tickers correlacionados) → los p-values son
optimistas; se reportan como guía, no como prueba.
"""
from __future__ import annotations

import pandas as pd


def tests(df: pd.DataFrame) -> dict:
    out: dict = {"caveats": []}
    from scipy import stats

    roi = pd.to_numeric(df.get("roi"), errors="coerce").dropna()
    if len(roi) >= 8:
        t, p = stats.ttest_1samp(roi, 0.0)
        out["roi_vs_zero"] = {
            "mean_roi": round(float(roi.mean()), 2), "t": round(float(t), 2),
            "p_value": round(float(p), 4), "sig": bool(p < 0.05),
            "interp": ("El ROI medio entre escenarios difiere de 0 (significativo)" if p < 0.05
                       else "El ROI medio NO se distingue de 0 — sin ventaja clara a nivel agregado"),
        }

    if {"n_trades", "n_pos", "robustness"}.issubset(df.columns):
        best = df.sort_values("robustness", ascending=False).iloc[0]
        n = int(best.get("n_trades") or 0)
        wins = int(best.get("n_pos") or 0)
        if n >= 20:
            p = stats.binomtest(wins, n, 0.5, alternative="greater").pvalue
            out["best_winrate_vs_50"] = {
                "id": best["id"], "win_rate": round(float(best.get("win_rate") or 0), 1),
                "n": n, "p_value": round(float(p), 4), "sig": bool(p < 0.05),
                "interp": ("El win-rate del mejor escenario supera 50% de forma significativa" if p < 0.05
                           else "El win-rate del mejor escenario no supera 50% de forma significativa"),
            }

    out["caveats"].append(
        "Las corridas dentro de un escenario NO son independientes (mismo período, tickers "
        "correlacionados) → los p-values son OPTIMISTAS. Úsalos como guía, no como prueba definitiva.")
    return out

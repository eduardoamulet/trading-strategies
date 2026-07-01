"""Análisis a granularidad DETAILED (1 fila por ticker×día×escenario).

Habilita lo que el agregado NO puede: por-ticker, día-de-la-semana (real), OOS train/test y métricas
por-corrida REALES (Sharpe/Sortino/Calmar/Profit Factor/max drawdown) desde la serie de ROIs por
posición (position_roi = ganancia/inversión). Puro; sin Streamlit.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_WD_ES = {0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue", 4: "Vie", 5: "Sáb", 6: "Dom"}
_WD_ORDER = ["Lun", "Mar", "Mié", "Jue", "Vie"]


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Añade position_roi (%), pnl, win, date, weekday a las filas por-posición."""
    d = df.copy()
    d["pnl"] = pd.to_numeric(d["ganancia"], errors="coerce")
    inv = pd.to_numeric(d["inv"], errors="coerce").replace(0, np.nan)
    d["position_roi"] = d["pnl"] / inv * 100.0
    d["win"] = d["pnl"] > 0
    d["date"] = pd.to_datetime(d["fecha"], errors="coerce")
    d["weekday"] = d["date"].dt.weekday.map(_WD_ES)
    d["ticker"] = d["ticker"].astype(str).str.strip()
    return d.dropna(subset=["position_roi"])


def series_metrics(roi) -> dict:
    """Sharpe/Sortino/Calmar/PF/maxDD/win-rate/avg de una serie de ROIs (%) por posición."""
    r = pd.to_numeric(pd.Series(roi), errors="coerce").dropna()
    if len(r) < 2:
        return {}
    mean, std = float(r.mean()), float(r.std(ddof=1))
    downside = float(r[r < 0].std(ddof=1)) if (r < 0).sum() >= 2 else np.nan
    wins, losses = float(r[r > 0].sum()), float(-r[r < 0].sum())
    cum = r.cumsum()
    dd = float((cum - cum.cummax()).min())              # max drawdown de la curva acumulada de ROI
    return {
        "n": int(len(r)),
        "avg_roi": round(mean, 3),
        "win_rate": round(float((r > 0).mean() * 100), 1),
        "net_roi": round(float(r.sum()), 2),
        "sharpe": round(mean / std, 3) if std > 0 else np.nan,
        "sortino": round(mean / downside, 3) if downside and downside > 0 else np.nan,
        "calmar": round(mean / abs(dd), 3) if dd < 0 else np.nan,
        "profit_factor": round(wins / losses, 3) if losses > 0 else np.nan,
        "max_dd": round(dd, 2),
    }


def by_scenario(d: pd.DataFrame) -> pd.DataFrame:
    rows = [{"id": sid, **series_metrics(g["position_roi"])} for sid, g in d.groupby("id")]
    df = pd.DataFrame(rows)
    return df.sort_values("sharpe", ascending=False, na_position="last").reset_index(drop=True)


def by_ticker(d: pd.DataFrame) -> pd.DataFrame:
    rows = [{"ticker": tk, **series_metrics(g["position_roi"])} for tk, g in d.groupby("ticker")]
    return pd.DataFrame(rows).sort_values("avg_roi", ascending=False).reset_index(drop=True)


def by_weekday(d: pd.DataFrame, min_n: int = 8) -> dict:
    """Mejor escenario por día de la semana + OPERAR/NO OPERAR (regla del PRD)."""
    per_day = {}
    for wd in _WD_ORDER:
        sub = d[d["weekday"] == wd]
        if sub.empty:
            continue
        cand = []
        for sid, g in sub.groupby("id"):
            m = series_metrics(g["position_roi"])
            if m.get("n", 0) >= min_n:
                cand.append({"scenario": sid, **m})
        if not cand:
            per_day[wd] = {"recommendation": "NO OPERAR", "reason": f"muestra insuficiente (n<{min_n})"}
            continue
        best = max(cand, key=lambda x: (x.get("sharpe") if x.get("sharpe") == x.get("sharpe") else -9))
        shp = best.get("sharpe")
        operar = best["win_rate"] > 55 and best["avg_roi"] > 0 and (shp == shp and shp > 0)
        per_day[wd] = {**best, "recommendation": "OPERAR" if operar else "NO OPERAR",
                       "reason": ("ventaja: WR>55% · ROI>0 · Sharpe>0" if operar else
                                  "sin ventaja (WR≤55% o ROI≤0 o Sharpe≤0)")}
    n_op = sum(1 for v in per_day.values() if v.get("recommendation") == "OPERAR")
    return {"available": True, "source": "results detallado (ticker×día)", "per_day": per_day,
            "n_operar": n_op, "n_dias": len(per_day)}


def oos_split(d: pd.DataFrame, ratio: float = 0.7) -> dict:
    """Split CRONOLÓGICO de días 70/30 → train vs test por escenario → clasificación de robustez."""
    days = sorted(pd.Series(d["date"].dropna().unique()))
    if len(days) < 4:
        return {"available": False, "reason": "muy pocos días para un split OOS confiable"}
    cut = days[int(len(days) * ratio)]
    tr, te = d[d["date"] < cut], d[d["date"] >= cut]
    rows = []
    for sid in d["id"].unique():
        mtr = series_metrics(tr[tr["id"] == sid]["position_roi"])
        mte = series_metrics(te[te["id"] == sid]["position_roi"])
        if not mtr or not mte:
            continue
        rows.append({
            "id": sid, "train_roi": mtr.get("avg_roi"), "test_roi": mte.get("avg_roi"),
            "train_wr": mtr.get("win_rate"), "test_wr": mte.get("win_rate"),
            "clasificacion": _classify(mtr.get("avg_roi", 0), mte.get("avg_roi", 0)),
        })
    tbl = pd.DataFrame(rows)
    counts = tbl["clasificacion"].value_counts().to_dict() if not tbl.empty else {}
    return {"available": True, "cut_date": str(pd.Timestamp(cut).date()),
            "n_train_days": int(sum(1 for x in days if x < cut)),
            "n_test_days": int(sum(1 for x in days if x >= cut)),
            "ratio": ratio, "table": tbl, "counts": counts}


def _classify(rtr: float, rte: float) -> str:
    """Robusto / Moderadamente Robusto / Poco Robusto / Sobreajustado (según consistencia train↔test)."""
    if rtr <= 0:
        return "Poco Robusto"
    if rte <= 0:
        return "Sobreajustado"                 # bueno en train, malo/negativo en test
    ratio = rte / rtr if rtr else 0.0
    if ratio >= 0.6:
        return "Robusto"
    if ratio >= 0.3:
        return "Moderadamente Robusto"
    return "Poco Robusto"

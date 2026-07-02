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
    n_insuf = 0
    for wd in _WD_ORDER:
        sub = d[d["weekday"] == wd]
        if sub.empty:
            continue
        # Mejor escenario del día por Sharpe (mostramos SIEMPRE sus métricas; el veredicto gatea por n).
        cand = [{"scenario": sid, **series_metrics(g["position_roi"])} for sid, g in sub.groupby("id")]
        cand = [c for c in cand if c.get("n")]
        if not cand:
            per_day[wd] = {"recommendation": "NO OPERAR", "reason": "sin datos ese día"}
            continue
        best = max(cand, key=lambda x: (x.get("sharpe") if x.get("sharpe") == x.get("sharpe") else -9))
        n = int(best.get("n") or 0)
        shp = best.get("sharpe")
        enough = n >= min_n
        edge = best["win_rate"] > 55 and best["avg_roi"] > 0 and (shp == shp and shp > 0)
        if not enough:
            n_insuf += 1
        per_day[wd] = {**best, "recommendation": "OPERAR" if (enough and edge) else "NO OPERAR",
                       "reason": ("ventaja: WR>55% · ROI>0 · Sharpe>0" if (enough and edge) else
                                  (f"muestra insuficiente (n={n} < {min_n} por día)" if not enough else
                                   "sin ventaja (WR≤55% o ROI≤0 o Sharpe≤0)"))}
    n_op = sum(1 for v in per_day.values() if v.get("recommendation") == "OPERAR")
    return {"available": True, "source": "results detallado (ticker×día)", "per_day": per_day,
            "n_operar": n_op, "n_dias": len(per_day), "n_insuf": n_insuf, "min_n": min_n}


def by_weekday_portfolio(d: pd.DataFrame, min_n: int = 5) -> dict:
    """Día de la semana a nivel CARTERA: para cada (día, escenario) el ROI por FECHA es
    Σganancia/Σinversión de los tickers ese día (refleja el colectivo de verdad); las métricas se
    calculan sobre esos ROI de cartera por día. Elige el mejor escenario por día. n = nº de fechas."""
    per_day = {}
    n_insuf = 0
    for wd in _WD_ORDER:
        sub = d[d["weekday"] == wd]
        if sub.empty:
            continue
        cand = []
        for sid, g in sub.groupby("id"):
            byd = g.groupby("date").agg(pnl=("pnl", "sum"), inv=("inv", "sum"))
            roi = (byd["pnl"] / byd["inv"].replace(0, np.nan) * 100.0).dropna()
            m = series_metrics(roi)
            if m:
                cand.append({"scenario": sid, **m})
        if not cand:
            per_day[wd] = {"recommendation": "NO OPERAR", "reason": "sin datos ese día"}
            continue
        best = max(cand, key=lambda x: (x.get("sharpe") if x.get("sharpe") == x.get("sharpe") else -9))
        n = int(best.get("n") or 0)
        shp = best.get("sharpe")
        enough = n >= min_n
        edge = best["win_rate"] > 55 and best["avg_roi"] > 0 and (shp == shp and shp > 0)
        if not enough:
            n_insuf += 1
        per_day[wd] = {**best, "recommendation": "OPERAR" if (enough and edge) else "NO OPERAR",
                       "reason": ("ventaja: WR>55% · ROI cartera>0 · Sharpe>0" if (enough and edge) else
                                  (f"muestra insuficiente (n={n} < {min_n} días)" if not enough else
                                   "sin ventaja (WR≤55% o ROI≤0 o Sharpe≤0)"))}
    n_op = sum(1 for v in per_day.values() if v.get("recommendation") == "OPERAR")
    return {"available": True, "source": "CARTERA (Σganancia/Σinversión por día, refleja el colectivo)",
            "per_day": per_day, "n_operar": n_op, "n_dias": len(per_day), "n_insuf": n_insuf,
            "min_n": min_n, "level": "cartera"}


def by_ticker_weekday(d: pd.DataFrame, min_n: int = 5) -> pd.DataFrame:
    """Mejor escenario por (TICKER × día de la semana) → ver si el patrón difiere entre activos.
    n por celda ≈ nº de semanas (÷3 vs el combinado), por eso el mínimo es más bajo."""
    rows = []
    for tk in sorted(d["ticker"].dropna().unique()):
        for wd in _WD_ORDER:
            sub = d[(d["ticker"] == tk) & (d["weekday"] == wd)]
            if sub.empty:
                continue
            cand = [{"scenario": sid, **series_metrics(g["position_roi"])} for sid, g in sub.groupby("id")]
            cand = [c for c in cand if c.get("n")]
            if not cand:
                continue
            best = max(cand, key=lambda x: (x.get("sharpe") if x.get("sharpe") == x.get("sharpe") else -9))
            n = int(best.get("n") or 0)
            shp = best.get("sharpe")
            enough = n >= min_n
            edge = best["win_rate"] > 55 and best["avg_roi"] > 0 and (shp == shp and shp > 0)
            rows.append({"Ticker": tk, "Día": wd, "Escenario": best["scenario"],
                         "Win Rate %": round(best["win_rate"], 1), "ROI %": round(best["avg_roi"], 2),
                         "Sharpe": best.get("sharpe"), "n": n,
                         "Recomendación": "OPERAR" if (enough and edge) else "NO OPERAR"})
    return pd.DataFrame(rows)


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


_CONTEXT_COLS = ["md_score", "md_confidence", "call_spread", "put_spread", "duration_min",
                 "spot_at_start", "call_delta", "call_gamma", "call_iv", "put_delta", "put_iv"]


def context_correlation(d: pd.DataFrame) -> pd.DataFrame:
    """Correlación del CONTEXTO de mercado a la entrada (md_score/confianza/spread/duración) con el
    ROI de la posición. Responde «¿el contexto predice el resultado?» — el corazón del objetivo del
    usuario. Requiere el results ENRIQUECIDO (columnas md_*/spread). Pearson + Spearman."""
    cols = [c for c in _CONTEXT_COLS
            if c in d.columns and pd.to_numeric(d[c], errors="coerce").notna().sum() >= 5]
    if not cols or "position_roi" not in d.columns:
        return pd.DataFrame()
    y = pd.to_numeric(d["position_roi"], errors="coerce")
    rows = []
    for c in cols:
        x = pd.to_numeric(d[c], errors="coerce")
        dd = pd.DataFrame({"x": x, "y": y}).dropna()
        if len(dd) < 5 or dd["x"].nunique() < 2:
            continue
        rows.append({"contexto": c, "pearson": round(float(dd["x"].corr(dd["y"])), 3),
                     "spearman": round(float(dd["x"].corr(dd["y"], method="spearman")), 3),
                     "n": int(len(dd))})
    res = pd.DataFrame(rows)
    if not res.empty:
        res = (res.assign(_a=res["spearman"].abs()).sort_values("_a", ascending=False)
               .drop(columns="_a").reset_index(drop=True))
    return res


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

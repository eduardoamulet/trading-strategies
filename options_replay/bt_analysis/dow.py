"""Análisis por DÍA DE LA SEMANA — del file DOW (ID×día) o del results detailed.

Regla del PRD: NO asumir que cada día tiene estrategia ganadora. Si un día no tiene ventaja
estadística suficiente → **NO OPERAR** (mejor perder oportunidades que operar sin ventaja).
"""
from __future__ import annotations

import pandas as pd

_DOW_ORDER = ["Lun", "Mar", "Mié", "Jue", "Vie"]

# Umbral de "ventaja": win-rate y $ promedio y Sharpe deben ser favorables + muestra mínima.
_MIN_N = 10
_WR_EDGE = 55.0


def analyze_dow(dow_df, detailed_df=None, scored=None) -> dict:
    """{available, source, per_day: {día: mejor config + métricas + OPERAR/NO OPERAR}}."""
    if dow_df is not None and not dow_df.empty and "dia" in dow_df.columns:
        return _from_dow_file(dow_df)
    if detailed_df is not None and not detailed_df.empty:
        return {"available": False,
                "reason": "results detailed presente pero el desglose por-día aún no está cableado en "
                          "esta versión; pasá el file DOW (Analisis_ID_x_diasemana)."}
    return {"available": False,
            "reason": "Sin datos por día de la semana. Pasá el file DOW o un results detallado."}


def _from_dow_file(dow: pd.DataFrame) -> dict:
    per_day = {}
    for dia in _DOW_ORDER:
        d = dow[dow["dia"] == dia].copy()
        if d.empty:
            continue
        d["n"] = pd.to_numeric(d.get("n"), errors="coerce")
        d = d[d["n"].fillna(0) >= _MIN_N]
        if d.empty:
            per_day[dia] = {"recommendation": "NO OPERAR", "reason": f"muestra insuficiente (n<{_MIN_N})"}
            continue
        d = d.sort_values(["win_rate", "sharpe"], ascending=False)
        b = d.iloc[0]
        wr = float(b.get("win_rate") or 0)
        avg = float(b.get("avg_usd") or 0)
        shp = float(b.get("sharpe") or 0)
        operar = (wr > _WR_EDGE) and (avg > 0) and (shp > 0)
        per_day[dia] = {
            "scenario": b.get("id"), "params": b.get("params"),
            "win_rate": round(wr, 1), "avg_usd": round(avg, 1), "sharpe": round(shp, 2),
            "peor_usd": round(float(b.get("peor_usd") or 0), 1), "n": int(b.get("n") or 0),
            "recommendation": "OPERAR" if operar else "NO OPERAR",
            "reason": ("ventaja: WR>55%, $ prom>0 y Sharpe>0" if operar
                       else "sin ventaja (WR≤55% o $ prom≤0 o Sharpe≤0)"),
        }
    n_op = sum(1 for v in per_day.values() if v.get("recommendation") == "OPERAR")
    return {"available": True, "source": "file DOW (ID×día)", "per_day": per_day,
            "n_operar": n_op, "n_dias": len(per_day)}

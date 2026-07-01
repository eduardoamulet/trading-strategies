"""ROI metrics por POSICIÓN — sobre los chequeos por minuto del df (cada `granularidad_seg`).

Una posición = un IterationResult (un ticker, un día, un escenario). Devuelve las columnas del
template de salida (menos ID/Ticker/Fecha, que pone el runner):
  Inversión total · Ganancia · {min,max,avg} ROI (%/$) · #ROI>0 · #ROI≤0 · #errores.

ROI por minuto:
  • con refuerzo → `ref_roi` (fracción) y `ref_value−ref_invested` ($) ya vienen multi-tranche en el df.
  • sin refuerzo → se computa de call_px/put_px contra la prima de entrada.
El df YA viene truncado al cierre real (incluido el colectivo, que muta el IterationResult antes).
"""
from __future__ import annotations

# claves del dict de salida (orden = columnas del template, sin ID/Ticker/Fecha)
COLS = ["inversion", "ganancia", "roi_min_pct", "roi_min_usd", "roi_max_pct", "roi_max_usd",
        "roi_avg_pct", "roi_avg_usd", "n_roi_pos", "n_roi_neg", "n_err"]


def error_row() -> dict:
    """Métricas para una posición que falló (sin contrato / sin datos)."""
    return {k: (1 if k == "n_err" else 0.0) for k in COLS}


def position_metrics(it) -> dict:
    """Métricas de una posición OK. `it` = IterationResult con `.df`."""
    df = it.df
    if df is None or len(df) == 0:
        return error_row()
    if "ref_roi" in df.columns and "ref_value" in df.columns:
        roi_pct = df["ref_roi"]                         # fracción, multi-tranche
        roi_usd = df["ref_value"] - df["ref_invested"]
    else:
        ce, pe = it.call_entry_premium, it.put_entry_premium
        ic, ip = it.invest_call, it.invest_put
        val = (ic * (df["call_px"] / ce) if ce else 0.0) + (ip * (df["put_px"] / pe) if pe else 0.0)
        inv = (ic or 0.0) + (ip or 0.0)
        roi_usd = val - inv
        roi_pct = (roi_usd / inv) if inv else roi_usd * 0.0
    return {
        "inversion": float(it.invest_total),
        "ganancia": float(it.gain_total),
        "roi_min_pct": float(roi_pct.min()) * 100.0,
        "roi_min_usd": float(roi_usd.min()),
        "roi_max_pct": float(roi_pct.max()) * 100.0,
        "roi_max_usd": float(roi_usd.max()),
        "roi_avg_pct": float(roi_pct.mean()) * 100.0,
        "roi_avg_usd": float(roi_usd.mean()),
        "n_roi_pos": int((roi_pct > 0).sum()),
        "n_roi_neg": int((roi_pct <= 0).sum()),
        "n_err": 0,
    }

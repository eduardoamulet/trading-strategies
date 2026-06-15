"""Configuración — tablas editables (no hardcodeadas) que alimentan el motor de backtest:
  1) Filtro de spread por strike (Opción 1)  → strike_spread_config.json
  2) Rango de precios de opción por ticker    → ticker_info.json

Se guardan a JSON; el motor los toma en el SIGUIENTE backtest (ambos loaders cachean por
mtime, así que no hace falta reiniciar la app).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
STRIKE_CFG_PATH = HERE / "strike_spread_config.json"
TICKER_INFO_PATH = HERE / "ticker_info.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


st.header("⚙️ Configuración")
st.caption("Estas tablas alimentan el motor de backtest (Opción 1). Se guardan a JSON — no están "
           "hardcodeadas — y el motor las toma en el **siguiente backtest** (sin reiniciar).")

# ═══════════════════════ 1) Filtro de spread por strike ═══════════════════════
st.subheader("1 · Filtro de spread por strike — Opción 1")
_sc = _read_json(STRIKE_CFG_PATH, {"enabled": True, "buckets": []})
_enabled = st.checkbox(
    "Filtro de spread por strike **activo**", value=bool(_sc.get("enabled", True)),
    help="Si lo desactivás, Opción 1 no filtra por strike (cae a la compuerta por precio del subyacente).")
_buckets = _sc.get("buckets", [])
_df_s = (pd.DataFrame(_buckets) if _buckets else
         pd.DataFrame(columns=["strike_min", "strike_max", "spread_min_contrato", "spread_max_contrato"]))
_ed_s = st.data_editor(
    _df_s, num_rows="dynamic", use_container_width=True, hide_index=True, key="cfg_strike_ed",
    column_config={
        "strike_min": st.column_config.NumberColumn("Strike mín", min_value=0.0, step=1.0),
        "strike_max": st.column_config.NumberColumn("Strike máx", min_value=0.0, step=1.0),
        "spread_min_contrato": st.column_config.NumberColumn("Spread mín ($/contrato)", min_value=0.0, step=1.0),
        "spread_max_contrato": st.column_config.NumberColumn("Spread máx ($/contrato)", min_value=0.0, step=1.0),
    })
st.caption("Valores **por contrato** (×100). El motor compara el spread **por acción = valor ÷ 100** "
           "(ej.: $1–$5 = spread de $0.01–$0.05 por acción). Un contrato cuyo spread quede FUERA de "
           "[mín, máx] de su bucket de strike se descarta.")
if st.button("💾 Guardar filtro de spread", type="primary", key="save_strike"):
    rows = []
    for _, r in _ed_s.iterrows():
        try:
            rows.append({"strike_min": float(r["strike_min"]), "strike_max": float(r["strike_max"]),
                         "spread_min_contrato": float(r["spread_min_contrato"]),
                         "spread_max_contrato": float(r["spread_max_contrato"])})
        except (TypeError, ValueError, KeyError):
            continue   # fila incompleta → se ignora
    out = {"enabled": bool(_enabled),
           "_comment": "Filtro de spread por strike (Opción 1). spread_*_contrato en $ POR CONTRATO "
                       "(x100); el motor compara por accion = valor/100.",
           "buckets": rows}
    STRIKE_CFG_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    st.success(f"Guardado: {len(rows)} bucket(s), filtro {'ACTIVO' if _enabled else 'DESACTIVADO'}. "
               "El motor lo toma en el próximo backtest.")

st.divider()

# ═══════════════════════ 2) Rango de precios por ticker ═══════════════════════
st.subheader("2 · Rango de precios de opción por ticker")
_ti = _read_json(TICKER_INFO_PATH, {})
_rows = [{"Ticker": k,
          "Óptimo mín": v.get("rango_optimo_lo"), "Óptimo máx": v.get("rango_optimo_hi"),
          "Ext mín": v.get("min"), "Ext máx": v.get("max"),
          "Nombre": v.get("nombre"), "Sector": v.get("bloque_sector")}
         for k, v in sorted(_ti.items())]
_df_t = pd.DataFrame(_rows, columns=["Ticker", "Óptimo mín", "Óptimo máx", "Ext mín", "Ext máx", "Nombre", "Sector"])
_ed_t = st.data_editor(
    _df_t, num_rows="dynamic", use_container_width=True, hide_index=True, height=460, key="cfg_ti_ed",
    column_config={
        "Ticker": st.column_config.TextColumn("Ticker", required=True, width="small"),
        "Óptimo mín": st.column_config.NumberColumn("Óptimo mín", min_value=0.0, step=5.0),
        "Óptimo máx": st.column_config.NumberColumn("Óptimo máx", min_value=0.0, step=5.0),
        "Ext mín": st.column_config.NumberColumn("Ext mín", min_value=0.0, step=5.0),
        "Ext máx": st.column_config.NumberColumn("Ext máx", min_value=0.0, step=5.0),
        "Nombre": st.column_config.TextColumn("Nombre", disabled=True),
        "Sector": st.column_config.TextColumn("Sector", disabled=True),
    })
st.caption("Rangos de **prima por contrato** (×100; el motor usa ÷100 = por acción). **Óptimo** = rango "
           "preferido (backtest manual); **Ext** = rango extendido (más ancho), que se usa si el óptimo "
           "queda vacío — y es el rango del backtest de **señales**.")
if st.button("💾 Guardar rangos por ticker", type="primary", key="save_ti"):
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    new = {}
    for _, r in _ed_t.iterrows():
        tk = str(r.get("Ticker") or "").strip().upper()
        if not tk:
            continue
        e = dict(_ti.get(tk, {}))   # preservar metadata (sectores, indice, fecha, etc.)
        e["ticker"] = tk
        e["rango_optimo_lo"] = _num(r.get("Óptimo mín"))
        e["rango_optimo_hi"] = _num(r.get("Óptimo máx"))
        e["min"] = _num(r.get("Ext mín"))
        e["max"] = _num(r.get("Ext máx"))
        if e.get("rango_optimo_lo") is not None and e.get("rango_optimo_hi") is not None:
            e["rango_optimo_text"] = f"${e['rango_optimo_lo']:g} - ${e['rango_optimo_hi']:g}"
        if e.get("min") is not None and e.get("max") is not None:
            e["min_max_text"] = f"MIN ${e['min']:g} MAX ${e['max']:g}"
        new[tk] = e
    TICKER_INFO_PATH.write_text(json.dumps(new, indent=2, ensure_ascii=False), encoding="utf-8")
    st.success(f"Guardado: {len(new)} ticker(s). El motor lo toma en el próximo backtest.")

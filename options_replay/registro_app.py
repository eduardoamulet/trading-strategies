"""Página 'Registro de trades' (Herramientas) — bitácora diaria con el template:
encabezado + checklist de requisitos + grilla por día + tabla del trade. Persistencia
en trades_db (SQLite local, gitignored).

Patrón de estado: las keys de los widgets se versionan con `reg_form_v` (_fv). Al
crear/editar/guardar se bumpea _fv → keys nuevas sin estado previo → el `value=`
(default, tomado del registro cargado o en blanco) prevalece. Durante la edición, las
keys son estables → se conserva lo que el usuario escribe.
"""
from __future__ import annotations

import json as _jsonlib
import sys
from datetime import date as _date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import trades_db as tdb  # noqa: E402

try:
    st.set_page_config(page_title="Registro de trades", layout="wide")
except Exception:
    pass

_now = lambda: datetime.now().isoformat(timespec="seconds")
SI_NO = ["—", "✅ Se cumple", "❌ No se cumple"]
TIPO_OPTS = ["CALL", "PUT"]
DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]
GRID_ROWS = ["Distancia", "Spot Price", "Strike Price"]
TRADE_COLS = ["Fecha Exp", "Tipo", "Fecha", "Hora", "N° Contratos",
              "Trade Price", "Rentabilidad $", "Plan %"]


def _idx(val):
    return SI_NO.index(val) if val in SI_NO else 0


def _form_defaults(loaded):
    """dict de defaults para los widgets (desde el registro cargado o en blanco)."""
    cl = (loaded or {}).get("checklist", {}) or {}
    grid = (loaded or {}).get("grid") or []
    trades = (loaded or {}).get("trades") or []
    # Grilla 3×5 (Concepto + 5 días). Si no hay guardada, una vacía.
    if grid:
        grid_df = pd.DataFrame(grid)
        for _c in ["Concepto"] + DIAS:
            if _c not in grid_df.columns:
                grid_df[_c] = ""
        grid_df = grid_df[["Concepto"] + DIAS]
    else:
        grid_df = pd.DataFrame({"Concepto": GRID_ROWS, **{d: ["", "", ""] for d in DIAS}})
    # Tabla del trade (al menos 1 fila vacía).
    if trades:
        trades_df = pd.DataFrame(trades)
        for _c in TRADE_COLS:
            if _c not in trades_df.columns:
                trades_df[_c] = "" if _c in ("Fecha Exp", "Tipo", "Fecha", "Hora") else 0.0
        trades_df = trades_df[TRADE_COLS]
    else:
        trades_df = pd.DataFrame([{"Fecha Exp": "", "Tipo": "CALL", "Fecha": "", "Hora": "",
                                   "N° Contratos": 0, "Trade Price": 0.0,
                                   "Rentabilidad $": 0.0, "Plan %": 0.0}])
    try:
        _f = _date.fromisoformat((loaded or {}).get("fecha") or "")
    except Exception:
        _f = datetime.now().date()
    return {
        "fecha": _f,
        "ticker": (loaded or {}).get("ticker", ""),
        "rango_precio": (loaded or {}).get("rango_precio", ""),
        "fed": cl.get("fed", "—"), "earning": cl.get("earning", "—"),
        "bollinger": cl.get("bollinger", "—"), "pm_notas": cl.get("pm_notas", ""),
        "rupturas": cl.get("rupturas", "—"), "gap": cl.get("gap", "—"),
        "bid": float(cl.get("bid") or 0.0), "ask": float(cl.get("ask") or 0.0),
        "grid_df": grid_df, "trades_df": trades_df,
        "notas": (loaded or {}).get("notas", ""),
    }


st.title("📓 Registro de trades")
st.caption("Bitácora diaria: checklist de requisitos + datos del trade. Se guarda local.")

_editing = st.session_state.get("reg_editing")
_loaded = tdb.get_trade(_editing) if _editing else None
_fv = st.session_state.get("reg_form_v", 0)
_d = _form_defaults(_loaded)
_k = lambda name: f"reg_{name}_{_fv}"   # key versionada

# ── Formulario (nuevo / edición) ─────────────────────────────────────────────
with st.container(border=True):
    st.subheader(("✏️ Editar registro" if _editing else "➕ Nuevo registro"))
    h1, h2, h3 = st.columns([1, 1, 1])
    fecha = h1.date_input("Fecha", value=_d["fecha"], key=_k("fecha"))
    ticker = h2.text_input("Ticker", value=_d["ticker"], key=_k("ticker")).upper().strip()
    rango = h3.text_input("Rango precio", value=_d["rango_precio"], key=_k("rango"))

    st.markdown("**Requisitos**")
    c1, c2 = st.columns(2)
    fed = c1.radio("1. Reunión FED (cada 45 días)", SI_NO, index=_idx(_d["fed"]),
                   horizontal=True, key=_k("fed"))
    earning = c2.radio("2. Earning (cada 3 meses)", SI_NO, index=_idx(_d["earning"]),
                       horizontal=True, key=_k("earning"))
    bollinger = c1.radio("3. Bollinger (15/Hora/Diario · punto medio)", SI_NO,
                         index=_idx(_d["bollinger"]), horizontal=True, key=_k("boll"))
    rupturas = c2.radio("5. Ruptura de líneas de tendencia", SI_NO,
                        index=_idx(_d["rupturas"]), horizontal=True, key=_k("rupt"))
    gap = c1.radio("6. Salto al alza / a la baja (GAP)", SI_NO, index=_idx(_d["gap"]),
                   horizontal=True, key=_k("gap"))
    pm_notas = st.text_area("4. Promedios móviles (techos/pisos · analizar hora/día)",
                            value=_d["pm_notas"], key=_k("pm"), height=70)

    b1, b2, b3 = st.columns(3)
    bid = b1.number_input("7. Precio BID", value=_d["bid"], step=0.01, format="%.2f", key=_k("bid"))
    ask = b2.number_input("7. Precio ASK", value=_d["ask"], step=0.01, format="%.2f", key=_k("ask"))
    b3.metric("Diferencia BID-ASK", f"{(ask - bid):.2f}")

    st.markdown("**8. Distancia / Spot / Strike por día**")
    grid_df = st.data_editor(_d["grid_df"], key=_k("grid"), hide_index=True,
                             use_container_width=True, disabled=["Concepto"])

    st.markdown("**Trade**")
    trades_df = st.data_editor(
        _d["trades_df"], key=_k("trades"), num_rows="dynamic", hide_index=True,
        use_container_width=True,
        column_config={
            "Tipo": st.column_config.SelectboxColumn("Tipo", options=TIPO_OPTS, required=True),
            "N° Contratos": st.column_config.NumberColumn("N° Contratos", step=1, format="%d"),
            "Trade Price": st.column_config.NumberColumn("Trade Price", format="$%.2f"),
            "Rentabilidad $": st.column_config.NumberColumn("Rentabilidad $", format="$%.2f"),
            "Plan %": st.column_config.NumberColumn("Plan %", format="%.0f%%"),
        })

    notas = st.text_area("Notas", value=_d["notas"], key=_k("notas"), height=70)

    _s1, _s2, _s3 = st.columns([1.4, 1.4, 5])
    if _s1.button("💾 Guardar", type="primary", use_container_width=True, key=_k("save")):
        _rec = {
            "fecha": fecha.isoformat(), "ticker": ticker, "rango_precio": rango,
            "checklist": {"fed": fed, "earning": earning, "bollinger": bollinger,
                          "pm_notas": pm_notas, "rupturas": rupturas, "gap": gap,
                          "bid": float(bid), "ask": float(ask)},
            "grid": grid_df.to_dict("records"),
            "trades": trades_df.fillna("").to_dict("records"),
            "notas": notas,
        }
        if _editing:
            tdb.update_trade(_editing, _rec, _now())
            st.toast("✏️ Registro actualizado")
        else:
            tdb.add_trade(_rec, _now())
            st.toast("✅ Registro guardado")
        st.session_state.pop("reg_editing", None)
        st.session_state["reg_form_v"] = _fv + 1
        st.rerun()
    if _editing and _s2.button("✖ Cancelar edición", use_container_width=True, key=_k("cancel")):
        st.session_state.pop("reg_editing", None)
        st.session_state["reg_form_v"] = _fv + 1
        st.rerun()

# ── Historial ────────────────────────────────────────────────────────────────
st.divider()
st.subheader("📚 Historial de registros")
_df = tdb.load_trades()
if _df.empty:
    st.info("Todavía no hay registros. Completá el formulario de arriba y tocá **Guardar**.")
    st.stop()

def _count_trades(j):
    try:
        return len(_jsonlib.loads(j) or [])
    except Exception:
        return 0


_df["# trades"] = _df["trades_json"].apply(_count_trades)
_show = _df[["fecha", "ticker", "rango_precio", "# trades", "rentabilidad_total"]].rename(
    columns={"fecha": "Fecha", "ticker": "Ticker", "rango_precio": "Rango precio",
             "rentabilidad_total": "Rentabilidad $"})
_event = st.dataframe(
    _show, use_container_width=True, hide_index=True, key="reg_hist",
    on_select="rerun", selection_mode="single-row",
    column_config={"Rentabilidad $": st.column_config.NumberColumn("Rentabilidad $",
                                                                    format="$%.2f")})
_sel = _event.selection.rows if (_event and _event.selection) else []
if _sel:
    _row = _df.iloc[_sel[0]]
    _rid = str(_row["id"])
    st.markdown(f"Seleccionado: **{_row['ticker']}** · {_row['fecha']} · "
                f"${float(_row['rentabilidad_total'] or 0):.2f}")
    _e1, _e2, _e3 = st.columns([1.4, 1.6, 5])
    if _e1.button("✏️ Editar", use_container_width=True, key="reg_edit_btn"):
        st.session_state["reg_editing"] = _rid
        st.session_state["reg_form_v"] = _fv + 1
        st.rerun()
    _del_ok = _e3.checkbox("Confirmar borrado (irreversible)", key="reg_del_ok")
    if _e2.button("🗑 Eliminar registro", use_container_width=True, disabled=not _del_ok,
                  key="reg_del_btn"):
        tdb.delete_trades([_rid])
        st.session_state.pop("reg_editing", None)
        st.session_state.pop("reg_del_ok", None)
        st.session_state["reg_form_v"] = _fv + 1
        st.toast("🗑 Registro borrado")
        st.rerun()

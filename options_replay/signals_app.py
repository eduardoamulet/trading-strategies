"""Página 'Alertas' — Historial de Señales con filas EXPANDIBLES.

Cada fila se expande para mostrar los Criterios de la estrategia (✓/✗) + la Gráfica
de la señal, y permite editar Estado / Ganancia. Importación: pegar JSON · subir .eml
· poller IMAP. Selección MULTI-fila → "Backtestear señales" redirige a la página
Backtesting con esas señales cargadas como iteraciones.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import external_signals as xs  # noqa: E402
import signals_db as db        # noqa: E402

try:
    st.set_page_config(page_title="Alertas", layout="wide")
except Exception:
    pass

st.markdown("<style>.block-container{padding-top:2rem !important;}</style>",
            unsafe_allow_html=True)
st.title("📡 Historial de Señales")
st.caption("Alertas de investepacademyia (Trend Reversal) — importadas a tu app")

_now = lambda: datetime.now().isoformat(timespec="seconds")

# ── Importar ─────────────────────────────────────────────────────────────────
with st.expander("📥 Importar señales", expanded=False):
    t_json, t_eml, t_mail = st.tabs(["Pegar JSON", "Subir email (.eml)", "Revisar correo (auto)"])
    with t_json:
        _txt = st.text_area("JSON del Historial", height=140, label_visibility="collapsed",
                            placeholder='{ "items": [ { "symbol": "AAPL", ... } ] }')
        if st.button("Importar JSON"):
            try:
                n = xs.import_payload(_txt, now_iso=_now())
                st.success(f"Importadas {n} señales nuevas.") if n else st.info("Sin señales nuevas.")
            except Exception as e:
                st.error(f"No se pudo parsear el JSON: {e}")
    with t_eml:
        _files = st.file_uploader("Emails (.eml)", type=["eml"], accept_multiple_files=True,
                                  label_visibility="collapsed")
        if _files and st.button("Importar email(s)"):
            tot = sum(xs.import_email(f.read(), now_iso=_now()) for f in _files)
            st.success(f"Importadas {tot} señales nuevas de {len(_files)} archivo(s).")
    with t_mail:
        st.caption("Lee tu Gmail por IMAP (requiere signals_secrets.py con un App Password).")
        if st.button("📧 Revisar correo ahora"):
            try:
                st.success(f"Importadas {xs.fetch_from_email(now_iso=_now())} señales nuevas.")
            except xs.ScraperNotConfigured as e:
                st.warning(str(e))
            except Exception as e:
                st.error(f"Error IMAP: {e}")

df = xs.load_signals()
if df.empty:
    st.info("Todavía no hay señales. Importá pegando el JSON, subiendo un .eml, o por correo.")
    st.stop()

# ── Filtros ──────────────────────────────────────────────────────────────────
f1, f2, f3, f4 = st.columns(4)
sel_estr = f1.selectbox("Estrategia", ["(todas)"] + sorted(df["estrategia"].dropna().unique().tolist()))
sel_sym = f2.selectbox("Acción", ["(todas)"] + sorted(df["symbol"].dropna().unique().tolist()))
sel_est = f3.selectbox("Estado", ["(todos)"] + xs.ESTADOS)
sel_tipo = f4.selectbox("Tipo", ["(todos)", "CALL", "PUT"])
_fechas = pd.to_datetime(df["fecha"], errors="coerce").dropna()
g1, g2, g3 = st.columns([1, 1, 2])
d_desde = g1.date_input("Desde", value=(_fechas.min().date() if len(_fechas) else datetime.now().date()))
d_hasta = g2.date_input("Hasta", value=(_fechas.max().date() if len(_fechas) else datetime.now().date()))
_page = g3.selectbox("Filas por página", [10, 25, 50, 100, "Todas"], index=4)

fdf = df.copy()
if sel_estr != "(todas)": fdf = fdf[fdf["estrategia"] == sel_estr]
if sel_sym != "(todas)": fdf = fdf[fdf["symbol"] == sel_sym]
if sel_est != "(todos)": fdf = fdf[fdf["estado"] == sel_est]
if sel_tipo != "(todos)": fdf = fdf[fdf["tipo"] == sel_tipo]
fdf = fdf[(fdf["fecha"] >= d_desde.isoformat()) & (fdf["fecha"] <= d_hasta.isoformat())]
if _page != "Todas": fdf = fdf.head(int(_page))

# ── Métricas ─────────────────────────────────────────────────────────────────
m1, m2, m3, m4 = st.columns(4)
m1.metric("Señales en lista", len(fdf))
m2.metric("💲 Dinero ganado con señales en lista", f"${fdf['ganancia'].sum():,.2f}")
m3.metric("CALL / PUT", f"{int((fdf['tipo'] == 'CALL').sum())} / {int((fdf['tipo'] == 'PUT').sum())}")
m4.metric("Aprovechadas", int((fdf["estado"] == "Aprovechada").sum()))

st.divider()


@st.dialog("📊 Detalle de la señal")
def _render_detalle(r):
    st.markdown(
        f"**{r.get('symbol', '')} · {r.get('tipo', '')} · "
        f"{r.get('fecha', '')} {r.get('hora', '')}** — {r.get('estrategia', '')}"
    )
    st.markdown("**Criterios de la estrategia:**")
    try:
        crits = json.loads(r["criterios_json"]) if r.get("criterios_json") else []
    except Exception:
        crits = []
    if crits:
        for c in crits:
            st.markdown(("✅ " if c.get("ok") else "❌ ") + str(c.get("nombre", "")))
    else:
        st.caption(f"Sin detalle de criterios ({r.get('criterios') or '—'}).")

    st.markdown("**Gráfica de la señal:**")
    if r.get("chart_url"):
        st.image(r["chart_url"], use_container_width=True)
    else:
        st.caption("Sin gráfica.")

    _i = xs.ESTADOS.index(r["estado"]) if r["estado"] in xs.ESTADOS else 0
    ne = st.selectbox("Estado", xs.ESTADOS, index=_i, key=f"est_{r['id']}")
    ng = st.number_input("Ganancia ($)", value=float(r["ganancia"] or 0), step=10.0,
                         key=f"gan_{r['id']}")
    if st.button("💾 Guardar", key=f"save_{r['id']}", use_container_width=True, type="primary"):
        db.update_user_fields(r["id"], estado=ne, ganancia=float(ng))
        st.toast("Guardado")
        st.rerun()   # cierra el modal y refresca


# Tabla ORDENABLE con selección MULTI-fila (casillas a la izquierda).
fdf = fdf.reset_index(drop=True)
_show = pd.DataFrame({
    "Acción": fdf["symbol"].values,
    "Hora": fdf["hora"].values,
    "Fecha": fdf["fecha"].values,
    "Estrategia": fdf["estrategia"].values,
    "% Cumpl.": pd.to_numeric(fdf["probabilidad"], errors="coerce").values,
    "Tipo": fdf["tipo"].values,
    "Criterios": fdf["criterios"].values,
    "Estado": fdf["estado"].values,
    "Ganancia": pd.to_numeric(fdf["ganancia"], errors="coerce").fillna(0.0).values,
    "Gráfica": fdf["chart_url"].values,
})
_sel = st.dataframe(
    _show, use_container_width=True, hide_index=True,
    on_select="rerun", selection_mode="multi-row",
    column_config={
        "% Cumpl.": st.column_config.NumberColumn("% Cumpl.", format="%.0f%%"),
        "Ganancia": st.column_config.NumberColumn("Ganancia", format="$%.0f"),
        "Gráfica": st.column_config.LinkColumn("Gráfica", display_text="📈 Ver"),
    },
)
st.caption("Clic en los **encabezados** para ordenar · marcá las **casillas** de la "
           "izquierda para seleccionar señales (1 fila → ver detalle · varias → backtest).")
_rows = _sel.selection.rows if (_sel and getattr(_sel, "selection", None)) else []

# Detalle en MODAL: al seleccionar 1 fila aparece "Ver detalle" → abre el modal
# (Criterios + Gráfica + Estado/Ganancia). La selección sola NO vuelca nada inline.
if len(_rows) == 1:
    if st.button("📈 Ver detalle de la señal seleccionada", use_container_width=True):
        _render_detalle(fdf.iloc[_rows[0]])

# ── Backtest de señales seleccionadas → redirige a la página Backtesting ─────
st.divider()
st.subheader("🔬 Backtest de señales seleccionadas")
st.caption(
    "Cada señal seleccionada = 1 iteración (ticker=Acción · fecha/hora de la señal · "
    "Sólo CALL/PUT según Tipo · Opción 1 menor spread · mismo día). El botón te lleva "
    "a **Backtesting**, con las señales ya cargadas como iteraciones para correrlas."
)
if not _rows:
    st.info("Marcá una o más filas (casilla a la izquierda de la tabla) para backtestearlas.")
else:
    if st.button(f"▶ Backtestear {len(_rows)} señal(es)  →  Backtesting", type="primary"):
        st.session_state["bt_signals_handoff"] = [
            {"symbol": str(fdf.iloc[i]["symbol"]), "fecha": str(fdf.iloc[i]["fecha"]),
             "hora": str(fdf.iloc[i]["hora"]), "tipo": str(fdf.iloc[i]["tipo"])}
            for i in _rows
        ]
        st.switch_page("options_replay/app.py")

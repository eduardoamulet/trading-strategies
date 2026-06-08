"""Página 'Señales' (Historial de Señales de investepacademyia).

Importación: pegar JSON · subir .eml · poller IMAP (automático).
Tabla estilo el sitio: filtros (estrategia/acción/estado/tipo/rango de fechas),
"Dinero ganado con señales en lista", y ESTADO/GANANCIA editables por fila.
"""
from __future__ import annotations

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
    st.set_page_config(page_title="Señales", layout="wide")
except Exception:
    pass

st.title("📡 Historial de Señales")
st.caption("Alertas de investepacademyia (estrategia Trend Reversal) — importadas a tu app")

_now = lambda: datetime.now().isoformat(timespec="seconds")

# ── Importar ─────────────────────────────────────────────────────────────────
with st.expander("📥 Importar señales", expanded=False):
    t_json, t_eml, t_mail = st.tabs(["Pegar JSON", "Subir email (.eml)", "Revisar correo (auto)"])
    with t_json:
        _txt = st.text_area("JSON del Historial", height=150, label_visibility="collapsed",
                            placeholder='{ "items": [ { "symbol": "AAPL", ... } ] }')
        if st.button("Importar JSON"):
            try:
                n = xs.import_payload(_txt, now_iso=_now())
                st.success(f"Importadas {n} señales nuevas.") if n else st.info("Sin señales nuevas.")
            except Exception as e:
                st.error(f"No se pudo parsear el JSON: {e}")
    with t_eml:
        _files = st.file_uploader("Emails de alerta (.eml)", type=["eml"],
                                  accept_multiple_files=True, label_visibility="collapsed")
        if _files and st.button("Importar email(s)"):
            tot = 0
            for f in _files:
                try:
                    tot += xs.import_email(f.read(), now_iso=_now())
                except Exception as e:
                    st.error(f"{f.name}: {e}")
            st.success(f"Importadas {tot} señales nuevas de {len(_files)} archivo(s).")
    with t_mail:
        st.caption("Lee tu Gmail por IMAP (requiere signals_secrets.py con un Gmail "
                   "App Password — gitignored). Filtra por remitente investepacademyia.")
        if st.button("📧 Revisar correo ahora"):
            try:
                n = xs.fetch_from_email(now_iso=_now())
                st.success(f"Importadas {n} señales nuevas del correo.")
            except xs.ScraperNotConfigured as e:
                st.warning(str(e))
            except Exception as e:
                st.error(f"Error IMAP: {e}")

df = xs.load_signals()
if df.empty:
    st.info("Todavía no hay señales. Importá pegando el JSON, subiendo un .eml, o por correo.")
    st.stop()

# ── Filtros (como en el sitio) ───────────────────────────────────────────────
f1, f2, f3, f4 = st.columns(4)
sel_estr = f1.selectbox("Estrategia", ["(todas)"] + sorted(df["estrategia"].dropna().unique().tolist()))
sel_sym = f2.selectbox("Acción", ["(todas)"] + sorted(df["symbol"].dropna().unique().tolist()))
sel_est = f3.selectbox("Estado", ["(todos)"] + xs.ESTADOS)
sel_tipo = f4.selectbox("Tipo", ["(todos)", "CALL", "PUT"])

_fechas = pd.to_datetime(df["fecha"], errors="coerce").dropna()
g1, g2, g3 = st.columns([1, 1, 2])
_dmin = _fechas.min().date() if len(_fechas) else datetime.now().date()
_dmax = _fechas.max().date() if len(_fechas) else datetime.now().date()
d_desde = g1.date_input("Desde", value=_dmin)
d_hasta = g2.date_input("Hasta", value=_dmax)
_page = g3.selectbox("Filas por página", [10, 25, 50, 100, "Todas"], index=4)

fdf = df.copy()
if sel_estr != "(todas)":
    fdf = fdf[fdf["estrategia"] == sel_estr]
if sel_sym != "(todas)":
    fdf = fdf[fdf["symbol"] == sel_sym]
if sel_est != "(todos)":
    fdf = fdf[fdf["estado"] == sel_est]
if sel_tipo != "(todos)":
    fdf = fdf[fdf["tipo"] == sel_tipo]
fdf = fdf[(fdf["fecha"] >= d_desde.isoformat()) & (fdf["fecha"] <= d_hasta.isoformat())]
if _page != "Todas":
    fdf = fdf.head(int(_page))

# ── Métricas ─────────────────────────────────────────────────────────────────
m1, m2, m3, m4 = st.columns(4)
m1.metric("Señales en lista", len(fdf))
m2.metric("💲 Dinero ganado con señales en lista", f"${fdf['ganancia'].sum():,.2f}")
m3.metric("CALL / PUT", f"{int((fdf['tipo'] == 'CALL').sum())} / {int((fdf['tipo'] == 'PUT').sum())}")
m4.metric("Aprovechadas", int((fdf["estado"] == "Aprovechada").sum()))

# ── Tabla con Estado/Ganancia EDITABLES ──────────────────────────────────────
ids = fdf["id"].tolist()
disp = pd.DataFrame({
    "Acción": fdf["symbol"].values,
    "Hora": fdf["hora"].values,
    "Fecha": fdf["fecha"].values,
    "Estrategia": fdf["estrategia"].values,
    "% Cumplimiento": fdf["probabilidad"].map(lambda x: f"{x:.0f}%" if pd.notna(x) else "").values,
    "Tipo": fdf["tipo"].values,
    "Criterios": fdf["criterios"].values,
    "Estado": fdf["estado"].fillna("Por definir").values,
    "Ganancia": pd.to_numeric(fdf["ganancia"], errors="coerce").fillna(0.0).values,
    "Activa": fdf["is_active"].map(lambda x: "🟢" if x == 1 else "⚪").values,
    "Gráfica": fdf["chart_url"].values,
})
edited = st.data_editor(
    disp, use_container_width=True, hide_index=True, num_rows="fixed", key="signals_editor",
    disabled=["Acción", "Hora", "Fecha", "Estrategia", "% Cumplimiento", "Tipo",
              "Criterios", "Activa", "Gráfica"],
    column_config={
        "Estado": st.column_config.SelectboxColumn("Estado", options=xs.ESTADOS, required=True),
        "Ganancia": st.column_config.NumberColumn("Ganancia", format="$%.2f", step=10.0),
        "Gráfica": st.column_config.LinkColumn("Gráfica", display_text="📈 Ver"),
    },
)

# Persistir ediciones de Estado/Ganancia
_chg = 0
for i in range(len(disp)):
    ne, oe = edited.iloc[i]["Estado"], disp.iloc[i]["Estado"]
    ng, og = float(edited.iloc[i]["Ganancia"] or 0), float(disp.iloc[i]["Ganancia"] or 0)
    if ne != oe or ng != og:
        db.update_user_fields(ids[i], estado=ne, ganancia=ng)
        _chg += 1
if _chg:
    st.toast(f"💾 {_chg} señal(es) actualizada(s)")

st.caption("Estado y Ganancia se guardan en SQLite (dedup por id). Próximo: "
           "backtestear una señal con un click.")

"""Página 'Señales' de la Trading Suite — Historial de Señales de investepacademyia.

Importación: pegar el JSON del Historial (manual, funciona ya) o, a futuro, ingesta
automática por email. Standalone-safe (set_page_config en try/except).
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

try:
    st.set_page_config(page_title="Señales", layout="wide")
except Exception:
    pass

st.title("📡 Señales")
st.caption("Historial de Señales — investepacademyia (estrategia Trend Reversal)")

# ── Importar pegando el JSON del Historial de Señales ────────────────────────
with st.expander("📥 Importar señales (pegar JSON del Historial)", expanded=False):
    st.caption("Pegá el JSON que devuelve el Historial de Señales (el objeto con "
               "`items`). Se importan con dedup por `id` (reimportar no duplica).")
    _txt = st.text_area("JSON", height=160, label_visibility="collapsed",
                        placeholder='{ "items": [ { "id": "...", "symbol": "AAPL", ... } ], ... }')
    if st.button("Importar JSON", type="primary"):
        try:
            n = xs.import_payload(_txt, now_iso=datetime.now().isoformat(timespec="seconds"))
            if n:
                st.success(f"Importadas {n} señales nuevas.")
            else:
                st.info("Sin señales nuevas (ya estaban todas, o el JSON venía vacío).")
        except Exception as e:
            st.error(f"No se pudo parsear el JSON: {e}")

# ── Ingesta automática por email (a futuro) ──────────────────────────────────
if st.button("📧 Revisar correo (alertas) — automático"):
    try:
        n = xs.fetch_from_email()
        st.success(f"Importadas {n} señales nuevas.")
    except xs.ScraperNotConfigured as e:
        st.warning(str(e))
    except Exception as e:  # pragma: no cover
        st.error(f"Error: {e}")

df = xs.load_signals()

if df.empty:
    st.info("Todavía no hay señales. Importá pegando el JSON del Historial (arriba).")
    st.stop()

# ── Filtros ──────────────────────────────────────────────────────────────────
f1, f2, f3, f4 = st.columns(4)
sel_estr = f1.selectbox("Estrategia", ["(todas)"] + sorted(df["estrategia"].dropna().unique().tolist()))
sel_sym = f2.selectbox("Acción", ["(todas)"] + sorted(df["symbol"].dropna().unique().tolist()))
sel_est = f3.selectbox("Estado", ["(todos)"] + sorted(df["estado"].dropna().unique().tolist()))
sel_tipo = f4.selectbox("Tipo", ["(todos)", "CALL", "PUT"])

fdf = df.copy()
if sel_estr != "(todas)":
    fdf = fdf[fdf["estrategia"] == sel_estr]
if sel_sym != "(todas)":
    fdf = fdf[fdf["symbol"] == sel_sym]
if sel_est != "(todos)":
    fdf = fdf[fdf["estado"] == sel_est]
if sel_tipo != "(todos)":
    fdf = fdf[fdf["tipo"] == sel_tipo]

# ── Métricas ─────────────────────────────────────────────────────────────────
m1, m2, m3, m4 = st.columns(4)
m1.metric("Señales en lista", len(fdf))
m2.metric("💲 Dinero ganado (lista)", f"${fdf['ganancia'].sum():,.2f}")
m3.metric("CALL / PUT", f"{int((fdf['tipo'] == 'CALL').sum())} / {int((fdf['tipo'] == 'PUT').sum())}")
m4.metric("Activas", int((fdf["is_active"] == 1).sum()))

# ── Tabla ────────────────────────────────────────────────────────────────────
show = fdf[["symbol", "hora", "fecha", "estrategia", "probabilidad", "tipo",
            "criterios", "estado", "ganancia", "is_active", "chart_url"]].copy()
show["probabilidad"] = show["probabilidad"].map(lambda x: f"{x:.0f}%" if pd.notna(x) else "")
show["ganancia"] = show["ganancia"].map(lambda x: f"${x:,.0f}" if pd.notna(x) else "")
show["is_active"] = show["is_active"].map(lambda x: "🟢" if x == 1 else "⚪")
show = show.rename(columns={
    "symbol": "Acción", "hora": "Hora", "fecha": "Fecha", "estrategia": "Estrategia",
    "probabilidad": "Probabilidad", "tipo": "Tipo", "criterios": "Criterios",
    "estado": "Estado", "ganancia": "Ganancia", "is_active": "Activa", "chart_url": "Gráfica",
})
st.dataframe(
    show, use_container_width=True, hide_index=True,
    column_config={"Gráfica": st.column_config.LinkColumn("Gráfica", display_text="📈 Ver")},
)

st.caption("Dedup por `id`. Las señales quedan en SQLite local (gitignored) y "
           "disponibles para el backtester (próximo: backtestear una señal con un click).")

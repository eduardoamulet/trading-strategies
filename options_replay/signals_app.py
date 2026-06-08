"""Página 'Señales' de la Trading Suite — Historial de Señales importado de
investepacademyia.com/app/alertas (lectura + importación con botón).

Standalone-safe: set_page_config envuelto en try/except (lo llama trading_suite).
"""
from __future__ import annotations

import sys
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
st.caption("Historial de Señales importado de investepacademyia.com/app/alertas")

# ── Botones de importación ───────────────────────────────────────────────────
cmail, cweb, _ = st.columns([1, 1, 2])
do_email = cmail.button("📧 Revisar correo (alertas)", type="primary",
                        use_container_width=True)
do_web = cweb.button("🔄 Importar del sitio", use_container_width=True)

df = xs.load_signals()


def _run_import(fn, label):
    try:
        with st.spinner(f"{label}…"):
            n = fn()
        st.success(f"Importadas {n} señales nuevas.")
        return xs.load_signals()
    except xs.ScraperNotConfigured as e:
        st.warning(str(e))
    except Exception as e:  # pragma: no cover
        st.error(f"Error: {e}")
    return df


if do_email:
    df = _run_import(xs.fetch_from_email, "Revisando correo")
elif do_web:
    df = _run_import(xs.fetch_and_store, "Importando del sitio")

if len(df) and (df["fuente"] == "demo").all():
    st.info("ℹ️ Mostrando **señales de ejemplo** (de tus capturas). El botón de "
            "importación en vivo se activa apenas conectemos el acceso al sitio.")

# ── Filtros ──────────────────────────────────────────────────────────────────
f1, f2, f3, f4 = st.columns(4)
_estr = ["(todas)"] + sorted(df["estrategia"].dropna().unique().tolist())
_acc = ["(todas)"] + sorted(df["accion"].dropna().unique().tolist())
_est = ["(todos)"] + sorted(df["estado"].dropna().unique().tolist())
sel_estr = f1.selectbox("Estrategia", _estr)
sel_acc = f2.selectbox("Acción", _acc)
sel_est = f3.selectbox("Estado", _est)
sel_tipo = f4.selectbox("Tipo", ["(todos)", "CALL", "PUT"])

fdf = df.copy()
if sel_estr != "(todas)":
    fdf = fdf[fdf["estrategia"] == sel_estr]
if sel_acc != "(todas)":
    fdf = fdf[fdf["accion"] == sel_acc]
if sel_est != "(todos)":
    fdf = fdf[fdf["estado"] == sel_est]
if sel_tipo != "(todos)":
    fdf = fdf[fdf["tipo"] == sel_tipo]

# ── Métricas ─────────────────────────────────────────────────────────────────
m1, m2, m3 = st.columns(3)
m1.metric("Señales en lista", len(fdf))
m2.metric("💲 Dinero ganado (lista)", f"${fdf['ganancia'].sum():,.2f}")
m3.metric("CALL / PUT", f"{int((fdf['tipo'] == 'CALL').sum())} / {int((fdf['tipo'] == 'PUT').sum())}")

# ── Tabla ────────────────────────────────────────────────────────────────────
show = fdf[["accion", "hora", "fecha", "estrategia", "cumplimiento",
            "tipo", "estado", "ganancia"]].copy()
show["cumplimiento"] = show["cumplimiento"].map(
    lambda x: f"{x:.0f}%" if pd.notna(x) else "")
show["ganancia"] = show["ganancia"].map(
    lambda x: f"${x:,.0f}" if pd.notna(x) else "")
show = show.rename(columns={
    "accion": "Acción", "hora": "Hora", "fecha": "Fecha", "estrategia": "Estrategia",
    "cumplimiento": "% Cumpl.", "tipo": "Tipo", "estado": "Estado", "ganancia": "Ganancia",
})
st.dataframe(show, use_container_width=True, hide_index=True)

st.caption("Las señales importadas se guardan localmente y quedan disponibles para "
           "el backtester (próximo paso: backtestear una señal con un click).")

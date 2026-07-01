"""Página 'Dashboard' — vista general (resumen de señales + accesos)."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import external_signals as xs  # noqa: E402

try:
    st.set_page_config(page_title="Dashboard", layout="wide")
except Exception:
    pass

st.title("🏠 Dashboard")
st.caption("Resumen de tu actividad de señales y backtesting")

df = xs.load_signals()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Señales totales", len(df))
c2.metric("Activas", int((df["is_active"] == 1).sum()) if len(df) else 0)
c3.metric("Aprovechadas", int((df["estado"] == "Aprovechada").sum()) if len(df) else 0)
c4.metric("Ganancia acumulada",
          f"${pd.to_numeric(df['ganancia'], errors='coerce').sum():,.0f}" if len(df) else "$0")

st.divider()
if len(df):
    st.subheader("🔔 Últimas señales")
    last = df.head(8)[["symbol", "tipo", "estrategia", "probabilidad", "fecha", "hora", "estado"]]
    last = last.rename(columns={"symbol": "Acción", "tipo": "Tipo", "estrategia": "Estrategia",
                                "probabilidad": "% Cumpl.", "fecha": "Fecha", "hora": "Hora",
                                "estado": "Estado"})
    st.dataframe(last, use_container_width=True, hide_index=True)
    st.caption("Detalle completo + importar en **🔔 Alertas**.")
else:
    st.info("Todavía no hay señales. Andá a **🔔 Alertas** para importarlas.")

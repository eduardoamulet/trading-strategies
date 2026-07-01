"""Página 'Evaluar dirección del mercado'.

Sección 1 — Universo operable: los activos para 0DTE optimizados por sus características intrínsecas
medidas (núcleo diario + roster de viernes), con lo operable HOY según el día.
Sección 2 — Dirección del mercado: el medidor del motor (en construcción)."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from market_direction.ui.universe_view import build_universe_df, tradeable_today_summary

try:
    st.set_page_config(page_title="Evaluar dirección del mercado", layout="wide")
except Exception:
    pass

st.title("🧭 Evaluar dirección del mercado")

# ── Sección 1 · Universo operable ──────────────────────────────────────────────
st.header("📋 Universo operable")
st.caption("Activos para 0DTE optimizados por sus **características intrínsecas medidas**. "
           "**Núcleo diario** = 0DTE todos los días (índices, movimiento moderado). "
           "**Roster de viernes** = grandes movers (acciones / ETFs), 0DTE solo el viernes.")

weekday = datetime.now().weekday()
_, msg = tradeable_today_summary(weekday)
st.info(msg)

filtro = st.radio("Mostrar:", ["Operables hoy", "Núcleo diario", "Roster viernes", "Todos"],
                  horizontal=True, index=0, key="md_universe_filtro")
df = build_universe_df(weekday=weekday, filtro=filtro)
st.dataframe(df, hide_index=True, use_container_width=True)

st.caption(
    "**Movimiento** = rango medio intradía (~60 días), con SPY ≈ 1%/día como piso · "
    "✓✓✓ ≥4% · ✓✓ 2.8–4% · ✓ 1.8–2.8% · ~ 1.2–1.8% · ✗ <1.2%.  "
    "**0DTE diario** existe solo en índices (SPX/SPY/QQQ/IWM/DIA); acciones y ETFs no-core = **solo viernes**.  "
    "⚠️ **Earnings:** evitá 0DTE de acciones en su semana de reporte (movimiento binario)."
)

# ── Sección 2 · Dirección del mercado (motor) ──────────────────────────────────
st.divider()
st.header("🧭 Dirección del mercado")
st.info("🚧 **Motor en construcción.** Acá va a ir el medidor **CALL / PUT / NO TRADE** por activo "
        "(fuerza 0–100 + confianza 0–1), sin look-ahead. Todavía no hay señal para mostrar.")
st.progress(0.45)
st.caption("✅ Dominio · Datos · Calibración · Universo  →  ⏳ **Indicadores** (en curso) · "
           "Confirmación · Modelo · Decisión · Engine")

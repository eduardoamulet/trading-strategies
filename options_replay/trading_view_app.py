"""Página 'Trading view' (submenú de Alertas) — EN CONSTRUCCIÓN.

Acá va a vivir la integración con TradingView (alertas / webhooks). Por ahora es un
placeholder; la construimos más adelante.
"""
from __future__ import annotations

import streamlit as st

try:
    st.set_page_config(page_title="Trading view", layout="wide")
except Exception:  # noqa: BLE001 — set_page_config ya llamado por el entry point
    pass

st.title("📈 Trading view")
st.caption("Alertas desde TradingView — próximamente")

st.info("🚧 **En construcción.** Esta sección va a integrar las alertas de TradingView "
        "(webhooks / señales). La construimos más adelante.")

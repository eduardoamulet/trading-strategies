"""Página 'Yoel AI' — asistente de IA (placeholder)."""
from __future__ import annotations
import streamlit as st

try:
    st.set_page_config(page_title="Yoel AI", layout="wide")
except Exception:
    pass

st.title("💬 Yoel AI")
st.caption("Asistente de IA para tus señales y estrategias")

st.info("🚧 Próximamente: un asistente que responda sobre tus señales, estrategias y "
        "resultados de backtesting.")

st.chat_input("Escribí tu pregunta… (aún no conectado)")

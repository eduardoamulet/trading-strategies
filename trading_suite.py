"""Trading Suite — entry point con menú lateral (estilo Investep).

Menú (izquierda):
  🏠 Dashboard · 🔔 Alertas · 🎯 Estrategias · 📈 Activos · 💬 Yoel AI · 👤 Perfil · ❓ Ayuda
  Herramientas: 🔬 Backtesting · 🟢 Live

Correr desde la raíz (Traiding/):
    py -m streamlit run trading_suite.py

Notas: cada página es un script Streamlit independiente; set_page_config se llama una
sola vez acá (los sub-apps lo envuelven en try/except para correr standalone).
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Trading Suite",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Menú principal (espejo de Investep) ──────────────────────────────────────
dashboard = st.Page("options_replay/dashboard_app.py", title="Dashboard", icon="🏠",
                    url_path="dashboard", default=True)
alertas = st.Page("options_replay/signals_app.py", title="Alertas", icon="🔔",
                  url_path="alertas")
estrategias = st.Page("options_replay/estrategias_app.py", title="Estrategias", icon="🎯",
                      url_path="estrategias")
activos = st.Page("options_replay/activos_app.py", title="Activos", icon="📈",
                  url_path="activos")
yoel = st.Page("options_replay/yoel_ai_app.py", title="Yoel AI", icon="💬",
               url_path="yoel-ai")
perfil = st.Page("options_replay/perfil_app.py", title="Perfil", icon="👤",
                 url_path="perfil")
ayuda = st.Page("options_replay/ayuda_app.py", title="Ayuda", icon="❓",
                url_path="ayuda")

# ── Herramientas propias ─────────────────────────────────────────────────────
backtesting = st.Page("options_replay/app.py", title="Backtesting", icon="🔬",
                      url_path="simulation")
live = st.Page("live_trader/ui/app.py", title="Live", icon="🟢", url_path="live")

pg = st.navigation(
    {
        "Menú": [dashboard, alertas, estrategias, activos, yoel, perfil, ayuda],
        "Herramientas": [backtesting, live],
    },
    position="sidebar",
)
pg.run()

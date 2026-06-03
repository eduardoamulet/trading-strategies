"""Trading Suite — entry point unificado con dos secciones:

  🔬 Simulation  → options_replay (backtest histórico con datos de Polygon)
  🟢 Live        → live_trader (trading de opciones en vivo, Tradier SANDBOX)

Correr desde la raíz del proyecto (Traiding/):
    py -m streamlit run trading_suite.py

Notas de arquitectura:
- Cada sección es un script Streamlit independiente; st.navigation los corre como
  "páginas". set_page_config se llama una sola vez acá (los sub-apps lo envuelven
  en try/except para seguir funcionando standalone).
- No hay colisión de módulos: options_replay usa `config` (Polygon) y live_trader
  usa `settings` (Tradier) — nombres distintos a propósito.
- La sección Live solo CONTROLA/visualiza; el monitoreo + auto-TP corren en el
  daemon aparte:  cd live_trader && py -m daemon.runner
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Trading Suite",
    layout="wide",
    initial_sidebar_state="expanded",
)

simulation = st.Page(
    "options_replay/app.py",
    title="Simulation",
    icon="🔬",
    url_path="simulation",   # explícito: ambos scripts se llaman app.py
    default=True,
)
live = st.Page(
    "live_trader/ui/app.py",
    title="Live",
    icon="🟢",
    url_path="live",
)

pg = st.navigation(
    {"Secciones": [simulation, live]},
    position="sidebar",
)
pg.run()

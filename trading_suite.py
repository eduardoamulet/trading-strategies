"""Trading Suite — entry point con login + menú lateral (estilo Investep).

Flujo:
  1) Gate de autenticación (auth.require_login): si no hay sesión → login; si no hay
     usuarios → crea el primer admin. Frena la app hasta entrar.
  2) Menú según rol: la sección Administración (👥 Usuarios) solo la ven los admin.

Menú: 🏠 Dashboard · 🔔 Alertas · 🎯 Estrategias · 📈 Activos · 👤 Perfil · ❓ Ayuda
Herramientas: 🔬 Backtesting · 🟢 Live   ·   Administración (admin): 👥 Usuarios

Correr desde la raíz (Traiding/):  py -m streamlit run trading_suite.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Trading Suite", layout="wide", initial_sidebar_state="expanded")

# Ocultar la barra superior de Streamlit (Deploy + menú ⋮): se consolida en el menú
# de usuario propio (arriba a la derecha).
st.markdown("<style>[data-testid='stToolbar']{display:none !important;}</style>",
            unsafe_allow_html=True)

sys.path.insert(0, str(Path(__file__).parent / "options_replay"))
import auth  # noqa: E402

# ── 1) Gate de autenticación ─────────────────────────────────────────────────
user = auth.require_login()   # frena si no hay sesión / crea el primer admin

# ── 2) Páginas ───────────────────────────────────────────────────────────────
dashboard = st.Page("options_replay/dashboard_app.py", title="Dashboard", icon="🏠",
                    url_path="dashboard", default=True)
alertas = st.Page("options_replay/signals_app.py", title="Alertas", icon="🔔",
                  url_path="alertas")
estrategias = st.Page("options_replay/estrategias_app.py", title="Estrategias", icon="🎯",
                      url_path="estrategias")
activos = st.Page("options_replay/activos_app.py", title="Activos", icon="📈",
                  url_path="activos")
perfil = st.Page("options_replay/perfil_app.py", title="Perfil", icon="👤", url_path="perfil")
ayuda = st.Page("options_replay/ayuda_app.py", title="Ayuda", icon="❓", url_path="ayuda")
backtesting = st.Page("options_replay/app.py", title="Backtesting", icon="🔬",
                      url_path="simulation")
live = st.Page("live_trader/ui/app.py", title="Live", icon="🟢", url_path="live")
usuarios = st.Page("options_replay/usuarios_app.py", title="Usuarios", icon="👥",
                   url_path="usuarios")

# ── 3) Navegación según rol ──────────────────────────────────────────────────
nav = {
    "Menú": [dashboard, alertas, estrategias, activos, perfil, ayuda],
    "Herramientas": [backtesting, live],
}
if user.get("rol") == "admin":
    nav["Administración"] = [usuarios]

pg = st.navigation(nav, position="sidebar")
auth.top_user_menu(user, perfil)   # menú de usuario arriba a la derecha
pg.run()

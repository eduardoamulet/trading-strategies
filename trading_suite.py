"""SignalForge — entry point con login + menú lateral (estilo Investep).

Flujo:
  1) Gate de autenticación (auth.require_login): si no hay sesión → login; si no hay
     usuarios → crea el primer admin. Frena la app hasta entrar.
  2) Menú según rol: la sección Administración (👥 Usuarios) solo la ven los admin.

Menú: 🏠 Dashboard · 🔔 Alertas · 🎯 Estrategias · 📈 Activos · 👤 Perfil · ❓ Ayuda
Herramientas: 🔬 Backtesting · 🟢 Live · 📓 Registro · 🔄 Datos   ·   Administración (admin): 👥 Usuarios

Correr desde la raíz (Traiding/):  py -m streamlit run trading_suite.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="SignalForge", page_icon="📈", layout="wide",
                   initial_sidebar_state="expanded")

# Ocultar SOLO el botón Deploy y el menú ⋮ de Streamlit (no toda la barra: si se
# oculta stToolbar entero se pierde el control para re-expandir la barra lateral).
# Además: el indicador de "Running…" (spinner) se mueve de la esquina superior
# DERECHA al CENTRO horizontal de arriba.
st.markdown(
    "<style>"
    "[data-testid='stAppDeployButton'],[data-testid='stMainMenu']{display:none !important;}"
    "[data-testid='stStatusWidget']{position:fixed !important; left:50% !important;"
    " right:auto !important; transform:translateX(-50%) !important; top:8px !important;"
    " z-index:9999998 !important;}"
    # Subir el header de cada página hacia arriba (sin que se oculte bajo la barra).
    # Selector ESPECÍFICO para ganarle al padding-top default (~6rem) de Streamlit;
    # aplica a TODAS las páginas del menú. 1.5rem = alto pero libre de la toolbar.
    "[data-testid='stMainBlockContainer'],"
    "[data-testid='stAppViewContainer'] > .main > .block-container"
    "{padding-top:1.5rem !important;}"
    # Menú lateral pegado al margen superior (sin el gap por defecto de Streamlit).
    "section[data-testid='stSidebar'] > div:first-child,"
    "section[data-testid='stSidebar'] [data-testid='stSidebarContent'],"
    "section[data-testid='stSidebar'] [data-testid='stSidebarUserContent'],"
    "section[data-testid='stSidebar'] .block-container"
    "{padding-top:0 !important; margin-top:0 !important;}"
    "section[data-testid='stSidebar'] [data-testid='stSidebarHeader']"
    "{padding:0 !important; min-height:0 !important; height:auto !important;}"
    # Ancho INICIAL del panel izquierdo (menú) = 250px. Backtesting lo duplica (500px)
    # en su propio CSS; al volver al menú, esta regla restaura el ancho inicial.
    "section[data-testid='stSidebar'][aria-expanded='true']"
    "{min-width:300px !important; max-width:300px !important;}"
    # Pequeño espacio encima del logo SignalForge.
    "[data-testid='stHeaderLogo']{margin-top:1rem !important;}"
    "</style>", unsafe_allow_html=True)

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
registro = st.Page("options_replay/registro_app.py", title="Registro", icon="📓",
                   url_path="registro")
datos = st.Page("options_replay/update_status_app.py", title="Datos", icon="🔄",
                url_path="datos")
tareas = st.Page("options_replay/tareas_app.py", title="Tareas", icon="🧰",
                 url_path="tareas")
usuarios = st.Page("options_replay/usuarios_app.py", title="Usuarios", icon="👥",
                   url_path="usuarios")

# ── 3) Navegación según rol ──────────────────────────────────────────────────
nav = {
    "Menú": [dashboard, alertas, estrategias, activos, perfil, ayuda],
    "Herramientas": [backtesting, live, registro, datos, tareas],
}
if user.get("rol") == "admin":
    nav["Administración"] = [usuarios]

# Logo arriba del menú: st.logo lo fija en el TOPE del panel izquierdo (sobre la
# navegación) → "SignalForge" siempre en el top, en todas las páginas.
_LOGO_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="150" height="30">'
             '<text x="0" y="23" font-family="sans-serif" font-size="22" font-weight="800">'
             '<tspan fill="#1f2937">Signal</tspan><tspan fill="#16a34a">Forge</tspan></text></svg>')
st.logo(_LOGO_SVG)
pg = st.navigation(nav, position="sidebar")
auth.top_user_menu(user, perfil)   # menú de usuario arriba a la derecha
pg.run()

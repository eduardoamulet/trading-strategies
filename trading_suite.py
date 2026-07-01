"""SignalForge — entry point con login + menú lateral (estilo Investep).

Flujo:
  1) Gate de autenticación (auth.require_login): si no hay sesión → login; si no hay
     usuarios → crea el primer admin. Frena la app hasta entrar.
  2) Menú según rol: la sección Administración (👥 Usuarios) solo la ven los admin.

Menú: 🏠 Dashboard · 🎯 Estrategias · 📈 Activos · 👤 Perfil · ❓ Ayuda
Alertas: 🔔 Investep Academy IA · 📈 Trading view (en construcción)
Herramientas: 🔬 Backtesting · 🟢 Live · 📓 Registro · 🔄 Datos   ·   Administración (admin): 👥 Usuarios

Correr desde la raíz (Traiding/):  py -m streamlit run trading_suite.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Sidebar (panel de config manual) CONTRAÍDA al llegar por handoff de señales (Investep /
# Trading view); expandida en cualquier otro caso. La flag la setean esos botones antes de saltar.
_sb_state = "collapsed" if st.session_state.get("bt_sidebar_collapse") else "expanded"
st.set_page_config(page_title="SignalForge", page_icon="📈", layout="wide",
                   initial_sidebar_state=_sb_state)
st.session_state.pop("bt_sidebar_collapse", None)   # one-shot: solo colapsa en la llegada

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
alertas = st.Page("options_replay/signals_app.py", title="Investep Academy IA",
                  icon="🔔", url_path="alertas")
trading_view = st.Page("options_replay/trading_view_app.py", title="Trading view", icon="📈",
                       url_path="trading_view")
estrategias = st.Page("options_replay/estrategias_app.py", title="Estrategias", icon="🎯",
                      url_path="estrategias")
activos = st.Page("options_replay/activos_app.py", title="Activos", icon="📈",
                  url_path="activos")
evaluar_direccion = st.Page("options_replay/evaluar_direccion_app.py",
                            title="Tendencia del mercado", icon="🧭",
                            url_path="evaluar_direccion")
perfil = st.Page("options_replay/perfil_app.py", title="Perfil", icon="👤", url_path="perfil")
ayuda = st.Page("options_replay/ayuda_app.py", title="Ayuda", icon="❓", url_path="ayuda")
backtesting = st.Page("options_replay/app.py", title="Backtesting", icon="🔬",
                      url_path="simulation")
live = st.Page("options_replay/live_app.py", title="Operar", icon="🟢", url_path="live")
registro = st.Page("options_replay/registro_app.py", title="Registro", icon="📓",
                   url_path="registro")
datos = st.Page("options_replay/update_status_app.py", title="Datos", icon="🔄",
                url_path="datos")
tareas = st.Page("options_replay/tareas_app.py", title="Tareas", icon="🧰",
                 url_path="tareas")
configuracion = st.Page("options_replay/configuracion_app.py", title="Configuración", icon="⚙️",
                        url_path="configuracion")
usuarios = st.Page("options_replay/usuarios_app.py", title="Usuarios", icon="👥",
                   url_path="usuarios")

# ── 3) Navegación según rol ──────────────────────────────────────────────────
nav = {
    "Menú": [dashboard, estrategias, activos, evaluar_direccion, perfil, ayuda],
    "Alertas": [alertas, trading_view],
    "Herramientas": [backtesting, live, registro, datos, tareas, configuracion],
}
if user.get("rol") == "admin":
    nav["Administración"] = [usuarios]

# Logo arriba del menú: st.logo lo fija en el TOPE del panel izquierdo (sobre la
# navegación) → "SignalForge" siempre en el top, en todas las páginas.
_LOGO_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="150" height="30">'
             '<text x="0" y="23" font-family="sans-serif" font-size="22" font-weight="800">'
             '<tspan fill="#1f2937">Signal</tspan><tspan fill="#16a34a">Forge</tspan></text></svg>')
st.logo(_LOGO_SVG)

# ── 🔔 Campanita de notificaciones — señales del DÍA (estilo Investep) ──────────
# Lee la base de señales (ingestadas por API/email) y muestra las de HOY con badge de
# "nuevas". Fija arriba a la derecha, a la IZQUIERDA del menú "EM" (con espacio entre ambos);
# el panel abre hacia la izquierda. Visible en todas las páginas. Nunca rompe la app.
try:
    import pandas as _pd
    import signals_db as _sdb
    _allsig = _sdb.load_signals()
    # Auto-fetch UNA vez por sesión (si hay credenciales en signals_secrets) → la campanita se
    # llena sola al abrir la app, sin tocar botones (como el sitio de Investep).
    if not st.session_state.get("_notif_autofetched"):
        st.session_state["_notif_autofetched"] = True
        try:
            import external_signals as _xs0
            _au, _ap, _at = _xs0._api_secrets()
            if (_au and _ap) or _at:
                _xs0.fetch_from_api(days_back=1)
                _allsig = _sdb.load_signals()
        except Exception:
            pass
    _hoy = _pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")
    _hoysig = (_allsig[_allsig["fecha"].astype(str) == _hoy]
               if _allsig is not None and not _allsig.empty else _pd.DataFrame())
    _seen = st.session_state.setdefault("_notif_seen", set())
    _nnew = 0 if _hoysig.empty else int((~_hoysig["id"].astype(str).isin(_seen)).sum())
    # Campanita FIJA arriba a la derecha, a la izquierda de EM (que está en right:16px). El
    # contenedor se colapsa (absolute, 0×0) para no ocupar espacio en el flujo de la página.
    st.markdown(
        "<style>"
        ".st-key-ccd_notif_bell{position:absolute !important; height:0 !important;"
        " width:0 !important; margin:0 !important; padding:0 !important;}"
        ".st-key-ccd_notif_bell div[data-testid='stPopover']{position:fixed !important; top:6px;"
        " right:125px; left:auto !important; width:auto !important; min-width:0 !important;"
        " z-index:9999999 !important;}"
        ".st-key-ccd_notif_bell div[data-testid='stPopover'] > button{width:auto !important;}"
        "</style>", unsafe_allow_html=True)
    _bell_box = st.container(key="ccd_notif_bell")
    with _bell_box.popover(f"🔔 {_nnew} nuevas" if _nnew else "🔔 Notificaciones"):
        _nb1, _nb2 = st.columns(2)
        if _nb1.button("🔄 Bajar API", key="_notif_fetch", use_container_width=True):
            try:
                import external_signals as _xs
                _k = _xs.fetch_from_api(days_back=1)
                st.toast(f"📡 {_k} señal(es) nueva(s) de la API")
            except Exception as _e:
                st.toast(f"⚠️ {str(_e)[:70]}")
            st.rerun()
        if _nb2.button("✓ Leer todas", key="_notif_read", use_container_width=True,
                       disabled=_hoysig.empty):
            _seen.update(_hoysig["id"].astype(str).tolist())
            st.rerun()
        st.caption(f"**{len(_hoysig)}** señales hoy · {_hoy}"
                   + (f" · **{_nnew}** nuevas" if _nnew else ""))
        if _hoysig.empty:
            st.caption("Sin señales hoy. Tocá «Bajar API» para traerlas.")
        for _, _r in _hoysig.sort_values("hora", ascending=False).head(25).iterrows():
            _ic = "📈" if str(_r.get("tipo")).upper() == "CALL" else "📉"
            _dot = "🔵 " if str(_r.get("id")) not in _seen else ""
            try:
                _pr = f"{float(_r.get('probabilidad')):.0f}%"
            except Exception:
                _pr = ""
            st.markdown(
                f"{_dot}{_ic} **{_r.get('tipo')} {_r.get('symbol')}** · {_pr} · {_r.get('estrategia')}  \n"
                f"<span style='color:#888;font-size:0.78em'>{_r.get('hora')}</span>",
                unsafe_allow_html=True)
except Exception as _ne:
    st.caption(f"🔔 campanita no disponible · {str(_ne)[:60]}")

pg = st.navigation(nav, position="sidebar")
auth.top_user_menu(user, perfil)   # menú de usuario arriba a la derecha
pg.run()

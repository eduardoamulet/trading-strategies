"""Gate de autenticación para SignalForge.

- require_login(): si no hay sesión, muestra login (o el arranque para crear el primer
  admin si la base de usuarios está vacía) y FRENA la app. Devuelve el usuario logueado.
- logout_button(): info del usuario + cerrar sesión en la barra lateral.
- require_role(): guard de defensa en profundidad para páginas de admin.

La sesión vive en st.session_state (por pestaña). Al refrescar hay que volver a entrar
(no hay cookies persistentes). Las contraseñas se validan con users_db.verify (hash).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import users_db as udb  # noqa: E402


def _auth_css() -> None:
    """Estilo de las pantallas de acceso: tarjeta centrada y angosta, wordmark."""
    st.markdown(
        "<style>"
        "[data-testid='stMainBlockContainer']{max-width:520px; margin:0 auto;"
        " padding-top:6rem !important;}"
        ".sf-wordmark{font-size:2.4rem; font-weight:800; letter-spacing:-1px; line-height:1;}"
        ".sf-wordmark .a{color:#16a34a;}"
        ".sf-tag{color:#6b7280; font-size:.95rem; margin:.35rem 0 1.4rem;}"
        ".sf-foot{color:#9ca3af; font-size:.8rem; text-align:center; margin-top:1.1rem;}"
        "</style>", unsafe_allow_html=True)


def _brand() -> None:
    st.markdown(
        "<div class='sf-wordmark'>Signal<span class='a'>Forge</span></div>"
        "<div class='sf-tag'>Señales → backtest → ejecución de opciones</div>",
        unsafe_allow_html=True)


def _login_form() -> None:
    _auth_css()
    _brand()
    with st.container(border=True):
        st.markdown("#### Iniciar sesión")
        with st.form("login_form"):
            email = st.text_input("Email", placeholder="tu@email.com")
            pwd = st.text_input("Contraseña", type="password", placeholder="••••••••")
            if st.form_submit_button("Entrar", type="primary", use_container_width=True):
                u = udb.verify(email, pwd)
                if u:
                    st.session_state["auth_user"] = u
                    st.rerun()
                else:
                    st.error("Email o contraseña incorrectos (o usuario inactivo).")
    st.markdown("<div class='sf-foot'>🔒 Acceso privado · SignalForge</div>",
                unsafe_allow_html=True)


def _bootstrap_admin() -> None:
    _auth_css()
    _brand()
    with st.container(border=True):
        st.markdown("#### 👋 Creá el primer usuario")
        st.caption("No hay usuarios todavía — este será el **administrador**.")
        with st.form("bootstrap_form"):
            nombre = st.text_input("Nombre")
            email = st.text_input("Email", placeholder="tu@email.com")
            pwd = st.text_input("Contraseña", type="password", help="Mínimo 6 caracteres")
            if st.form_submit_button("Crear administrador", type="primary",
                                     use_container_width=True):
                try:
                    udb.add_user(nombre, email, pwd, "admin", True,
                                 creado_en=datetime.now().isoformat(timespec="seconds"))
                    st.session_state["auth_user"] = udb.verify(email, pwd)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


def require_login() -> dict:
    """Devuelve el usuario logueado; si no hay sesión, muestra login/arranque y frena."""
    udb.init_db()
    user = st.session_state.get("auth_user")
    if user:
        return user
    # Sin sesión: ocultar el menú lateral (que st.navigation deja "pegado") para que
    # la pantalla de login quede limpia.
    st.markdown(
        "<style>section[data-testid='stSidebar']{display:none !important;}"
        "[data-testid='collapsedControl']{display:none !important;}</style>",
        unsafe_allow_html=True,
    )
    if udb.count() == 0:
        _bootstrap_admin()
    else:
        _login_form()
    st.stop()


def _initials(user) -> str:
    name = user.get("nombre") or user.get("email", "")
    parts = [p for p in name.replace("@", " ").replace(".", " ").split() if p]
    return ("".join(p[0] for p in parts[:2]).upper() or "U")


def top_user_menu(user, perfil_page=None) -> None:
    """Menú de usuario ARRIBA A LA DERECHA: avatar (iniciales) → nombre/rol, Ajustes
    y Cerrar sesión (como en el sitio)."""
    if not user:
        return
    # Fijar el menú en la esquina superior derecha, POR ENCIMA del header de Streamlit
    # (z-index alto: sino el header lo tapaba). width:auto para que el botón NO se
    # estire a todo el ancho (Streamlit le pone width:100% al contenedor por defecto).
    st.markdown(
        "<style>"
        "div[data-testid='stPopover']{position:fixed !important; top:6px; right:16px;"
        " left:auto !important; width:auto !important; min-width:0 !important;"
        " z-index:9999999 !important;}"
        "div[data-testid='stPopover'] > button{width:auto !important;}"
        "</style>", unsafe_allow_html=True)
    with st.popover(f"👤 {_initials(user)}"):
        st.markdown(
            f"**{user.get('nombre') or user['email']}**  \n"
            f"<span style='color:#888;font-size:12px'>{user['email']} · "
            f"{user['rol']}</span>", unsafe_allow_html=True)
        st.divider()
        if perfil_page is not None:
            st.page_link(perfil_page, label="Ajustes", icon="⚙️")
        if st.button("🔄 Recargar (rerun)", use_container_width=True, key="rerun_top"):
            st.rerun()
        if st.button("🧹 Limpiar caché", use_container_width=True, key="clearcache_top"):
            try:
                st.cache_data.clear()
                st.cache_resource.clear()
            except Exception:
                pass
            st.toast("Caché limpiada")
        st.divider()
        if st.button("↪ Cerrar sesión", use_container_width=True, key="logout_top"):
            st.session_state.pop("auth_user", None)
            st.rerun()


def logout_button() -> None:  # compat: variante en la barra lateral (no se usa)
    user = st.session_state.get("auth_user")
    if not user:
        return
    with st.sidebar:
        st.divider()
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.pop("auth_user", None)
            st.rerun()


def require_role(*roles: str) -> dict:
    """Guard para páginas restringidas (defensa en profundidad). Frena si el usuario
    no tiene el rol requerido."""
    user = st.session_state.get("auth_user")
    if not user:
        st.error("Iniciá sesión para ver esta página.")
        st.stop()
    if roles and user.get("rol") not in roles:
        st.error("No tenés permisos para ver esta sección.")
        st.stop()
    return user

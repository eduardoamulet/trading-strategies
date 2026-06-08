"""Página 'Administración → Usuarios' — alta/baja/edición de usuarios.

Las contraseñas se guardan hasheadas (ver users_db). No hay login todavía: esta
sección queda abierta hasta que se agregue el gate de autenticación (próximo paso).
"""
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import users_db as udb  # noqa: E402
import auth  # noqa: E402

try:
    st.set_page_config(page_title="Usuarios", layout="wide")
except Exception:
    pass

auth.require_role("admin")   # defensa en profundidad: solo admin

st.title("👥 Administración de Usuarios")
st.caption("Alta, edición y baja de usuarios. Contraseñas hasheadas (PBKDF2 + salt).")

# ── Agregar usuario ──────────────────────────────────────────────────────────
with st.form("alta_usuario", clear_on_submit=True):
    st.markdown("#### ➕ Agregar usuario")
    c1, c2 = st.columns(2)
    nombre = c1.text_input("Nombre")
    email = c2.text_input("Email")
    c3, c4, c5 = st.columns([2, 1, 1])
    pwd = c3.text_input("Contraseña", type="password", help="Mínimo 6 caracteres.")
    rol = c4.selectbox("Rol", udb.ROLES)
    activo = c5.checkbox("Activo", value=True)
    if st.form_submit_button("Agregar usuario", type="primary"):
        try:
            udb.add_user(nombre, email, pwd, rol, activo,
                         creado_en=datetime.now().isoformat(timespec="seconds"))
            st.success(f"Usuario **{email}** creado ({rol}).")
        except ValueError as e:
            st.error(str(e))

st.divider()

# ── Lista de usuarios ────────────────────────────────────────────────────────
df = udb.list_users()
st.markdown(f"#### Usuarios registrados ({len(df)})")
if df.empty:
    st.info("Todavía no hay usuarios. Agregá el primero (te sugiero rol **admin**) arriba.")
    st.stop()

_show = df.copy()
_show["activo"] = _show["activo"].map(lambda x: "🟢 Activo" if x == 1 else "⚪ Inactivo")
st.dataframe(
    _show.rename(columns={"id": "ID", "nombre": "Nombre", "email": "Email", "rol": "Rol",
                          "activo": "Estado", "creado_en": "Creado"}),
    hide_index=True, use_container_width=True)

# ── Administrar un usuario ───────────────────────────────────────────────────
with st.expander("⚙️ Administrar un usuario"):
    opts = {f"#{r.id} · {r.email} ({r.rol})": int(r.id) for r in df.itertuples()}
    sel = st.selectbox("Usuario", list(opts.keys()))
    uid = opts[sel]
    cur = df[df["id"] == uid].iloc[0]

    a1, a2 = st.columns(2)
    with a1:
        st.markdown("**Rol y estado**")
        nuevo_rol = st.selectbox("Rol", udb.ROLES, index=udb.ROLES.index(cur["rol"])
                                 if cur["rol"] in udb.ROLES else 1, key="rol_edit")
        if st.button("Guardar rol"):
            udb.set_role(uid, nuevo_rol); st.success("Rol actualizado."); st.rerun()
        if st.button(("Desactivar" if cur["activo"] == 1 else "Activar") + " usuario"):
            udb.set_active(uid, cur["activo"] != 1); st.rerun()
    with a2:
        st.markdown("**Contraseña**")
        np = st.text_input("Nueva contraseña", type="password", key="pwd_reset")
        if st.button("Resetear contraseña"):
            try:
                udb.set_password(uid, np); st.success("Contraseña actualizada.")
            except ValueError as e:
                st.error(str(e))

    st.divider()
    st.markdown("**Eliminar**")
    if st.checkbox(f"Confirmo eliminar a {cur['email']}", key="del_ok"):
        if st.button("🗑️ Eliminar usuario", type="secondary"):
            udb.delete_user(uid); st.success("Usuario eliminado."); st.rerun()

st.info("🔐 Por ahora no hay pantalla de login: esta sección está abierta. El siguiente "
        "paso es agregar el **gate de autenticación** (login) para que solo los **admin** "
        "vean esta página y cada usuario entre con su contraseña.")

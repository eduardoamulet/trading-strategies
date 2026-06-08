"""Página 'Perfil' — Ajustes del perfil (nombre/apellido/país + notificaciones) y
estado de las integraciones."""
from __future__ import annotations
import sys
from pathlib import Path
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import users_db as udb  # noqa: E402

try:
    st.set_page_config(page_title="Perfil", layout="wide")
except Exception:
    pass

st.title("⚙️ Ajustes")
st.caption("Aquí podés ajustar tus datos de perfil y configurar tus notificaciones.")

PAISES = ["Estados Unidos", "Argentina", "México", "Colombia", "Chile", "Perú",
          "España", "Venezuela", "Ecuador", "Uruguay", "Otro"]

prof = udb.get_profile()

# ── Editar datos del perfil ──────────────────────────────────────────────────
with st.container(border=True):
    st.markdown("#### 👤 Editar datos del perfil")
    with st.form("perfil_form"):
        c1, c2 = st.columns(2)
        nombre = c1.text_input("Nombre", value=prof["nombre"])
        apellido = c2.text_input("Apellido", value=prof["apellido"])
        _pais_idx = PAISES.index(prof["pais"]) if prof["pais"] in PAISES else 0
        pais = st.selectbox("País", PAISES, index=_pais_idx)
        notif = st.checkbox("Recibir notificaciones por email", value=prof["notif_email"])
        if st.form_submit_button("Guardar Cambios", type="primary"):
            udb.save_profile(nombre.strip(), apellido.strip(), pais, notif)
            st.success("Cambios guardados.")
            st.rerun()

_full = f"{prof['nombre']} {prof['apellido']}".strip()
if _full:
    st.caption(f"Perfil actual: **{_full}** · {prof['pais']}")

st.divider()

# ── Estado de integraciones ──────────────────────────────────────────────────
st.markdown("#### 🔌 Integraciones")


def _ok(cond: bool) -> str:
    return "🟢 Configurado" if cond else "⚪ Sin configurar"


_polygon = False
try:
    import config  # type: ignore
    _polygon = bool(getattr(config, "POLYGON_API_KEY", ""))
except Exception:
    pass
_gmail = (HERE / "signals_secrets.py").exists()
_tradier = (HERE.parent / "live_trader" / "secrets.py").exists()

for label, status in [("📈 Polygon (datos de mercado)", _ok(_polygon)),
                      ("📧 Gmail / Alertas por email", _ok(_gmail)),
                      ("🟢 Tradier (live, sandbox)", _ok(_tradier))]:
    c1, c2 = st.columns([3, 1])
    c1.write(label)
    c2.write(status)

if not _gmail:
    st.info("Para automatizar las alertas por email: copiá `signals_secrets.example.py` → "
            "`signals_secrets.py` y completá tu Gmail App Password.")

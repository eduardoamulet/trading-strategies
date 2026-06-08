"""Página 'Perfil' — estado de integraciones / configuración."""
from __future__ import annotations
import sys
from pathlib import Path
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

try:
    st.set_page_config(page_title="Perfil", layout="wide")
except Exception:
    pass

st.title("👤 Perfil")
st.caption("Estado de las integraciones de tu app")


def _ok(cond: bool) -> str:
    return "🟢 Configurado" if cond else "⚪ Sin configurar"


# Polygon (datos de mercado)
_polygon = False
try:
    import config  # type: ignore
    _polygon = bool(getattr(config, "POLYGON_API_KEY", ""))
except Exception:
    pass

# Gmail (ingesta de alertas por email)
_gmail = (HERE / "signals_secrets.py").exists()

# Tradier (live)
_tradier = (HERE.parent / "live_trader" / "secrets.py").exists()

rows = [
    ("📈 Polygon (datos de mercado)", _ok(_polygon)),
    ("📧 Gmail / Alertas por email", _ok(_gmail)),
    ("🟢 Tradier (live, sandbox)", _ok(_tradier)),
]
for label, status in rows:
    c1, c2 = st.columns([3, 1])
    c1.write(label)
    c2.write(status)

st.divider()
st.markdown("#### Alertas por email")
if _gmail:
    st.success("Gmail configurado — el botón 'Revisar correo' en 🔔 Alertas trae las señales.")
else:
    st.info("Para automatizar las alertas por email: copiá `signals_secrets.example.py` → "
            "`signals_secrets.py` y completá tu Gmail App Password (ver instrucciones en el "
            "archivo de ejemplo).")

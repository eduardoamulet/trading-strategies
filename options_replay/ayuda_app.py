"""Página 'Ayuda' — guía rápida de las secciones."""
from __future__ import annotations
import streamlit as st

try:
    st.set_page_config(page_title="Ayuda", layout="wide")
except Exception:
    pass

st.title("❓ Ayuda")
st.caption("Guía rápida de la app")

st.markdown(
    """
### Secciones

- **🏠 Dashboard** — resumen: cantidad de señales, activas, aprovechadas y ganancia.
- **🔔 Alertas** — Historial de Señales (importadas de investepacademyia). Importás
  *subiendo el email .eml* o *por correo automático* (IMAP). Filtrás por
  estrategia/acción/estado/tipo/fechas y editás **Estado** y **Ganancia**.
- **🎯 Estrategias** — definición de las estrategias (Trend Reversal, etc.).
- **📈 Activos** — universo de símbolos y su rango óptimo.
- **🔬 Backtesting** — simulación histórica de opciones 0DTE/1DTE con datos de Polygon.
- **🟢 Live** — control del trading en vivo (Tradier sandbox).
- **👤 Perfil** — estado de las integraciones (Polygon / Gmail / Tradier).

### Importar alertas por email (automático)
1. Copiá `signals_secrets.example.py` → `signals_secrets.py`.
2. Completá tu **Gmail App Password** (Cuenta de Google → Seguridad → Contraseñas de
   aplicaciones).
3. En **🔔 Alertas → Revisar correo (auto)**, tocá *Revisar correo ahora*.

### Seguridad
- Las credenciales viven en archivos **gitignored** (nunca en git).
- La base de datos de señales es **SQLite local** (`data/signals.db`, gitignored).
"""
)

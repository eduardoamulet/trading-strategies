"""Página 'Estrategias' — listado de estrategias en tarjetas (como el sitio):
búsqueda, "N señales hoy", acciones cumpliendo y Ver Detalle."""
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import signals_db as sdb  # noqa: E402

try:
    st.set_page_config(page_title="Estrategias", layout="wide")
except Exception:
    pass

# (nombre, clave, descripción)
ESTRATEGIAS = [
    ("Rebote Punto Medio", "midpoint-bounce",
     "Rebote del precio desde el punto medio de un rango/canal."),
    ("Cambio de Tendencia en Hora", "trend-reversal",
     "Reversión sobre la tendencia horaria (MM20H), confirmada en marcos menores."),
    ("Cambio de Tendencia en 15m", "trend-reversal-15m",
     "Reversión en marco de 15 minutos usando SMA20 + Bandas de Bollinger."),
    ("Efecto Imán", "magnet-effect",
     "Atracción del precio hacia un nivel/medida de referencia."),
]
CRITERIOS = {
    "trend-reversal": ["Tendencia Previa (+2 días)", "Ruptura Línea de Tendencia y MM20H",
                       "Tendencia B15m", "Integridad MM"],
    "trend-reversal-15m": ["Cierre de ayer vs SMA20", "Precio actual vs SMA20 recalculado",
                           "Posición respecto a Bandas de Bollinger", "Confirmación de ruptura"],
}

try:
    sig = sdb.load_signals()
except Exception:
    sig = pd.DataFrame()
HOY = datetime.now().strftime("%Y-%m-%d")

st.title("🎯 Estrategias")
st.markdown("#### Filtros")
q = st.text_input("🔍 Buscar por nombre de estrategia", "", label_visibility="collapsed").strip().lower()
items = [e for e in ESTRATEGIAS if q in e[0].lower()] if q else ESTRATEGIAS


@st.dialog("Detalle de la estrategia")
def _detalle(nombre, clave, desc):
    st.subheader(nombre)
    st.caption(f"`{clave}`")
    st.write(desc)
    if clave in CRITERIOS:
        st.markdown("**Criterios de la estrategia:**")
        for c in CRITERIOS[clave]:
            st.markdown(f"- {c}")
    if len(sig):
        s = sig[sig["estrategia"] == nombre]
        if len(s):
            st.markdown(f"**Señales de esta estrategia ({len(s)}):**")
            st.dataframe(
                s[["symbol", "tipo", "probabilidad", "fecha", "hora", "estado"]].head(25),
                hide_index=True, use_container_width=True)
        else:
            st.info("Sin señales registradas de esta estrategia.")


for row in range(0, len(items), 3):
    cols = st.columns(3)
    for j, (nombre, clave, desc) in enumerate(items[row:row + 3]):
        s = sig[sig["estrategia"] == nombre] if len(sig) else pd.DataFrame()
        n_hoy = int((s["fecha"] == HOY).sum()) if len(s) else 0
        cumpliendo = sorted(s[s["is_active"] == 1]["symbol"].unique().tolist()) if len(s) else []
        with cols[j]:
            with st.container(border=True):
                ca, cb = st.columns([3, 1])
                ca.markdown(f"**{nombre}**")
                if cb.button("Ver Detalle", key=f"est_{clave}", use_container_width=True):
                    _detalle(nombre, clave, desc)
                st.markdown(
                    f"<span style='background:#eef;color:#445;padding:2px 10px;border-radius:10px;"
                    f"font-size:12px'>{n_hoy} señales hoy</span>", unsafe_allow_html=True)
                if cumpliendo:
                    st.caption("✅ Cumpliendo: " + ", ".join(cumpliendo))
                else:
                    st.caption("No hay acciones cumpliendo actualmente")

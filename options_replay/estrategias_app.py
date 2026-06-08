"""Página 'Estrategias' — definición de las estrategias de señales."""
from __future__ import annotations
import streamlit as st

try:
    st.set_page_config(page_title="Estrategias", layout="wide")
except Exception:
    pass

st.title("🎯 Estrategias")
st.caption("Estrategias de detección de señales (Trend Reversal y variantes)")

ESTRATEGIAS = [
    ("Cambio de Tendencia en Hora", "trend-reversal",
     "Reversión sobre la tendencia horaria (MM20H). Criterios: Tendencia Previa "
     "(+2 días) · Ruptura Línea de Tendencia y MM20H · Tendencia B15m · Integridad MM."),
    ("Cambio de Tendencia en 15m", "trend-reversal-15m",
     "Reversión en marco de 15 minutos usando SMA20 + Bandas de Bollinger. Criterios "
     "sobre cierre vs SMA20, ruptura y posición respecto a las bandas."),
    ("Efecto Imán", "magnet-effect",
     "Atracción del precio hacia un nivel/medida de referencia."),
    ("Rebote Punto Medio", "midpoint-bounce",
     "Rebote desde el punto medio de un rango/canal."),
]

for nombre, clave, desc in ESTRATEGIAS:
    with st.container(border=True):
        st.markdown(f"### {nombre}")
        st.caption(f"`{clave}`")
        st.write(desc)

st.info("Las señales de estas estrategias se importan y administran en **🔔 Alertas**. "
        "Más adelante se puede portar la estrategia (ej. tu Trend Reversal de Pine) para "
        "generar señales propias con tus datos de Polygon.")

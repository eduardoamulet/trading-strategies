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
# Estrategias de BACKTESTING de SignalForge (modos del motor — los de «Estrategia / Tipo de operación»).
ESTRATEGIAS_SF = [
    ("CALL y PUT",
     "Se compran ambas piernas (50/50) y la salida es combinada por ROI total (Umbral de ROI / Stop "
     "loss sobre la suma de las dos). Termina al umbral, al stop o al cierre del día."),
    ("CALL y PUT (Refuerzo)",
     "Martingala por pierna. Igual que CALL y PUT (50/50): cada pierna mira su propio ROI y, cuando cae "
     "a ≤ −Umbral de pérdida refuerzo (%), se refuerza la pierna que más pierde comprando más de ESA "
     "misma pierna (mismo tipo, nunca la contraria) con su inversión inicial. Termina cuando el ROI "
     "total (ambas piernas) alcanza el Umbral de ROI (%) (gana) o cae al −Stop loss (%) (corta — stop "
     "sobre el TOTAL, no por pierna), o al cierre del día."),
    ("CALL y PUT (plus)",
     "Se compran ambas piernas (50/50) y se venden las dos solo en el Horario de salida (sin Umbral de "
     "ROI ni Stop loss). Termina al horario o al cierre del día."),
    ("Sólo CALL",
     "Una sola pierna (100% CALL). Sale por su Umbral de ROI o su Stop loss. Termina al umbral, al stop "
     "o al cierre del día."),
    ("Sólo PUT",
     "Una sola pierna (100% PUT). Sale por su Umbral de ROI o su Stop loss. Termina al umbral, al stop "
     "o al cierre del día."),
    ("CALL o PUT",
     "Se compran ambas piernas y se venden las dos en cuanto cualquiera alcanza +100% (se duplica). No "
     "depende de Umbral de ROI ni Stop loss. Termina al +100% o al cierre del día."),
    ("CALL o PUT (plus)",
     "Se compran ambas piernas. La 1ª pierna que alcanza el Umbral de salida (%) se vende; la otra se "
     "vende cuando, entre lo bancado y su valor, se recupera la inversión total. Termina ahí o al "
     "cierre del día."),
    ("CALL o PUT (End of Day)",
     "Se compran ambas piernas (50/50) y se venden las dos al cierre del día. No depende de Umbral de "
     "ROI ni Stop loss del ticker — el umbral/stop COLECTIVO (si el alcance lo incluye) sí puede "
     "cerrarlas antes; para un EOD puro elegí «Aplicar solo a tickers»."),
    ("Sólo CALL (End of Day)",
     "Una sola pierna (100% CALL) que se vende al cierre del día. No depende de Umbral de ROI ni Stop "
     "loss del ticker — el colectivo (si el alcance lo incluye) sí puede cerrarla antes."),
    ("Sólo PUT (End of Day)",
     "Una sola pierna (100% PUT) que se vende al cierre del día. No depende de Umbral de ROI ni Stop "
     "loss del ticker — el colectivo (si el alcance lo incluye) sí puede cerrarla antes."),
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
items_sf = [e for e in ESTRATEGIAS_SF if q in e[0].lower()] if q else ESTRATEGIAS_SF


@st.dialog("Detalle de la estrategia", width="large")
def _detalle(nombre, clave, desc):
    _izq, _der = st.columns([1, 1.4])
    with _izq:
        st.subheader(nombre)
        st.caption(f"`{clave}`")
        st.write(desc)
        if clave in CRITERIOS:
            st.markdown("**Criterios de la estrategia:**")
            for c in CRITERIOS[clave]:
                st.markdown(f"- {c}")
    with _der:
        s = sig[sig["estrategia"] == nombre] if len(sig) else pd.DataFrame()
        if len(s):
            st.markdown(f"**Señales de esta estrategia ({len(s)}):**")
            st.dataframe(
                s[["symbol", "tipo", "probabilidad", "fecha", "hora", "estado"]].head(25),
                hide_index=True, use_container_width=True)
        else:
            st.info("Sin señales registradas de esta estrategia.")


st.subheader("🏛️ Estrategias Investep Academy")
if not items:
    st.caption("Ninguna estrategia coincide con la búsqueda.")
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

st.divider()
st.subheader("⚙️ Estrategias SignalForge")
st.caption("Modos de operación del motor de backtesting — los que elegís en «Estrategia» (antes «Tipo "
           "de operación») del backtest manual y de señales.")
if not items_sf:
    st.caption("Ninguna estrategia coincide con la búsqueda.")
for _row in range(0, len(items_sf), 2):
    _cols = st.columns(2)
    for _j, (_nombre, _desc) in enumerate(items_sf[_row:_row + 2]):
        with _cols[_j]:
            with st.container(border=True):
                st.markdown(f"**{_nombre}**")
                st.caption(_desc)

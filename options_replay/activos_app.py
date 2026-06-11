"""Página 'Activos / Acciones' — universo de tickers en vista de TARJETAS: grid de
2 columnas (cada tarjeta a media anchura), badge ACTIVO, ESTADO (señal actual),
búsqueda y paginación.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import signals_db as _sdb  # noqa: E402

try:
    st.set_page_config(page_title="Acciones", layout="wide")
except Exception:
    pass

_top1, _top2 = st.columns([4, 1])
_top1.title("📈 Acciones")

_path = HERE / "ticker_info.json"
info = json.loads(_path.read_text(encoding="utf-8")) if _path.exists() else {}
tickers = sorted(info.keys())

try:
    _sig = _sdb.load_signals()
except Exception:
    _sig = pd.DataFrame()


def _estado(tk):
    """(texto, tipo) de la señal actual del ticker; 'Sin señal' si no hay."""
    if len(_sig):
        s = _sig[_sig["symbol"] == tk]
        act = s[s["is_active"] == 1]
        use = act if len(act) else s
        if len(use):
            r = use.iloc[0]
            p = f" {r['probabilidad']:.0f}%" if pd.notna(r.get("probabilidad")) else ""
            return (f"{r['tipo']}{p}", r["tipo"])
    return ("Sin señal", None)


@st.dialog("Detalle del activo")
def _detalle(tk):
    i = info.get(tk, {})
    st.subheader(f"{tk} — {i.get('nombre') or ''}")
    c1, c2 = st.columns(2)
    c1.markdown("<span style='color:#888;font-size:12px'>Índice</span><br>"
                f"<b style='font-size:15px'>{i.get('indice') or '—'}</b>", unsafe_allow_html=True)
    c2.markdown("<span style='color:#888;font-size:12px'>Sector</span><br>"
                f"<b style='font-size:15px'>{i.get('bloque_sector') or '—'}</b>", unsafe_allow_html=True)
    st.write(f"**Rango óptimo:** {i.get('rango_optimo_text') or '—'}  ·  "
             f"**Mín/Máx:** {i.get('min_max_text') or '—'}")
    est, _ = _estado(tk)
    st.write(f"**Estado actual:** {est}")
    if len(_sig):
        s = _sig[_sig["symbol"] == tk]
        if len(s):
            st.markdown("**Señales recientes:**")
            st.dataframe(
                s[["fecha", "hora", "tipo", "estrategia", "probabilidad", "estado"]].head(10),
                hide_index=True, use_container_width=True)


# Búsqueda
q = st.text_input("🔍 Buscar por compañía o símbolo", "").strip().upper()
if st.session_state.get("activos_q") != q:
    st.session_state["activos_q"] = q
    st.session_state["activos_page"] = 1
flt = ([t for t in tickers
        if q in t.upper() or q in (info[t].get("nombre") or "").upper()] if q else tickers)

# Paginación (10 por página → grid de 2 columnas = 5 filas)
PER, total = 10, len(flt)
pages = max(1, (total + PER - 1) // PER)
pg = min(max(1, st.session_state.get("activos_page", 1)), pages)
start = (pg - 1) * PER
page_items = flt[start:start + PER]

# Grid de tarjetas: 2 columnas → cada tarjeta ocupa la mitad del ancho.
for r in range(0, len(page_items), 2):
    cols = st.columns(2)
    for j, tk in enumerate(page_items[r:r + 2]):
        i = info[tk]
        est, tipo = _estado(tk)
        ecol = "#ef4444" if tipo == "PUT" else ("#10b981" if tipo == "CALL" else "#9ca3af")
        with cols[j]:
            with st.container(border=True):
                st.markdown(
                    "<div style='display:flex;justify-content:space-between;align-items:center'>"
                    f"<span style='font-size:18px;font-weight:700'>{tk}</span>"
                    "<span style='background:#10b981;color:#fff;padding:2px 8px;border-radius:10px;"
                    "font-size:10px;font-weight:600'>✓ ACTIVO ⭐</span></div>",
                    unsafe_allow_html=True)
                st.caption(i.get("nombre") or "—")
                st.markdown(
                    "<span style='color:#888;font-size:12px'>ESTADO</span> &nbsp;"
                    f"<span style='background:{ecol};color:#fff;padding:2px 10px;border-radius:8px;"
                    f"font-size:12px'>{est}</span>", unsafe_allow_html=True)
                if st.button("Ver Detalle", key=f"det_{tk}", use_container_width=True):
                    _detalle(tk)

# Pie + paginación
st.divider()
p1, p2, p3 = st.columns([2, 1, 1])
p1.caption(f"Mostrando {start + 1 if total else 0} a {min(start + PER, total)} de {total} registros")
if p2.button("◀ Anterior", disabled=(pg <= 1), use_container_width=True):
    st.session_state["activos_page"] = pg - 1
    st.rerun()
if p3.button("Siguiente ▶", disabled=(pg >= pages), use_container_width=True):
    st.session_state["activos_page"] = pg + 1
    st.rerun()

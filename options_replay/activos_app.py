"""Página 'Activos' — universo de tickers (de ticker_info.json)."""
from __future__ import annotations
import json
import sys
from pathlib import Path
import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

try:
    st.set_page_config(page_title="Activos", layout="wide")
except Exception:
    pass

st.title("📈 Activos")
st.caption("Universo de símbolos disponibles para señales y backtesting")

_path = HERE / "ticker_info.json"
if not _path.exists():
    st.warning("No se encontró ticker_info.json.")
    st.stop()

info = json.loads(_path.read_text(encoding="utf-8"))
df = pd.DataFrame(info.values())

c1, c2 = st.columns(2)
sectores = ["(todos)"] + sorted(df["bloque_sector"].dropna().unique().tolist())
sel_sec = c1.selectbox("Sector", sectores)
q = c2.text_input("Buscar ticker / nombre", "").strip().upper()

fdf = df.copy()
if sel_sec != "(todos)":
    fdf = fdf[fdf["bloque_sector"] == sel_sec]
if q:
    fdf = fdf[fdf["ticker"].str.upper().str.contains(q, na=False)
              | fdf["nombre"].fillna("").str.upper().str.contains(q, na=False)]

st.metric("Símbolos", len(fdf))
show = fdf[["ticker", "nombre", "indice", "bloque_sector", "rango_optimo_text", "min_max_text"]].rename(
    columns={"ticker": "Ticker", "nombre": "Nombre", "indice": "Índice",
             "bloque_sector": "Sector", "rango_optimo_text": "Rango óptimo",
             "min_max_text": "Mín/Máx"})
st.dataframe(show, use_container_width=True, hide_index=True)
st.caption("El rango óptimo (÷100) es el rango de prima por defecto en el **🔬 Backtesting**.")

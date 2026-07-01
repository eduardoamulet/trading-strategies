"""Página 'Evaluar dirección del mercado'.

Sección 1 — Universo operable: los activos para 0DTE optimizados por sus características intrínsecas
medidas (núcleo diario + roster de viernes), con lo operable HOY según el día.
Sección 2 — Dirección del mercado: el medidor del motor (en construcción)."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from market_direction.ui.universe_view import build_universe_df, tradeable_today_summary

try:
    st.set_page_config(page_title="Tendencia del mercado", layout="wide")
except Exception:
    pass

st.title("🧭 Tendencia del mercado")

# ── Sección 1 · Universo operable ──────────────────────────────────────────────
with st.expander("📋 Universo operable", expanded=False):
    st.caption("Activos para 0DTE optimizados por sus **características intrínsecas medidas**. "
               "**Núcleo diario** = 0DTE todos los días (índices, movimiento moderado). "
               "**Roster** = grandes movers (acciones / ETFs), 0DTE menos frecuente.")

    weekday = datetime.now().weekday()
    _, msg = tradeable_today_summary(weekday)
    st.info(msg)

    filtro = st.radio("Mostrar:", ["Operables hoy", "Núcleo diario", "Roster viernes", "Todos"],
                      horizontal=True, index=0, key="md_universe_filtro")
    df = build_universe_df(weekday=weekday, filtro=filtro)
    st.dataframe(df, hide_index=True, use_container_width=True)

    st.caption(
        "**Movimiento** = rango medio intradía (~60 días), con SPY ≈ 1%/día como piso · "
        "✓✓✓ ≥4% · ✓✓ 2.8–4% · ✓ 1.8–2.8% · ~ 1.2–1.8% · ✗ <1.2%.  "
        "**0DTE diario** solo SPX/SPY/QQQ/IWM; megacaps (NVDA/AAPL/AVGO/META/MSFT/TSLA/AMZN + "
        "GLD/SLV/USO) = **Lun/Mié/Vie**; el resto (incl. **DIA**) = **solo viernes**.  "
        "⚠️ **Earnings:** evitá 0DTE de acciones en su semana de reporte (movimiento binario)."
    )

# ── Sección 2 · Dirección del mercado (motor) ──────────────────────────────────
st.divider()
st.header("🧭 Dirección del mercado")
st.caption("Dirección probable de un activo en un minuto dado — **sin look-ahead** (usa solo velas "
           "≤ la hora). Combina indicadores propios + confirmación cross-asset (SPY ancla) + reglas "
           "ponderadas. La confianza aún es heurística; se calibrará con el backtest.")

import datetime as _dt
import streamlit.components.v1 as _components
from market_direction.engine import market_direction_engine
from market_direction.ui.gauge import build_gauge_html
from market_direction.domain import ticker_profile as _mdtp


@st.cache_resource
def _md_provider():
    from market_direction.data import default_provider
    return default_provider()


def _md_bump(_delta):
    """Suma/resta `_delta` minutos a la hora y dispara la evaluación."""
    _cur = st.session_state.get("md_time", _dt.time(10, 0))
    _tot = (_cur.hour * 60 + _cur.minute + _delta) % 1440
    st.session_state["md_time"] = _dt.time(_tot // 60, _tot % 60)
    st.session_state["md_do_eval"] = True


_tks = [p.ticker for p in _mdtp.all_profiles()]
st.session_state.setdefault("md_time", _dt.time(10, 0))
_c1, _c2, _c3, _c4 = st.columns([2, 1.8, 2.4, 1.3])
_md_tk = _c1.selectbox("Activo", _tks, index=(_tks.index("SPY") if "SPY" in _tks else 0), key="md_tk")
_md_date = _c2.date_input("Fecha", value=_dt.date(2025, 5, 12), key="md_date")
with _c3:
    _md_time = st.time_input("Hora (ET)", key="md_time")
    _bm1, _bm2 = st.columns(2)
    _bm1.button("− 1 min", on_click=_md_bump, args=(-1,), use_container_width=True, key="md_minus")
    _bm2.button("+ 1 min", on_click=_md_bump, args=(1,), use_container_width=True, key="md_plus")
_go = _c4.button("Evaluar", type="primary", use_container_width=True, key="md_eval")

if _go or st.session_state.pop("md_do_eval", False):
    _sig = market_direction_engine(_md_tk, _md_date.isoformat(), _md_time.strftime("%H:%M"),
                                   provider=_md_provider())
    st.session_state["md_sig"] = _sig.to_dict()
    st.session_state["md_svg"] = build_gauge_html(_sig)

if st.session_state.get("md_sig"):
    _d = st.session_state["md_sig"]
    _gc, _mc = st.columns([1.4, 1])
    with _gc:
        _components.html(st.session_state["md_svg"], height=215)
    with _mc:
        st.metric("Acción", _d["action"])
        _r1, _r2 = st.columns(2)
        _r1.metric("Fuerza", f"{_d['score']:.0f}/100")
        _r2.metric("Confianza", f"{_d['confidence']:.0%}")
        st.caption(f"{_d['ticker']} · {_d['date']} · {_d['entry_time']} ET · tendencia **{_d['trend']}**")
    if _d["action"] != "NO TRADE":
        _l1, _l2, _l3, _l4 = st.columns(4)
        _l1.metric("Entry", _d["entry_price"])
        _l2.metric("Stop", _d["stop"])
        _l3.metric("Target", _d["target"])
        _l4.metric("R:R", _d["risk_reward"])
    with st.expander("🔎 Razones — desglose del score", expanded=False):
        for _rz in _d["reasons"]:
            st.markdown(f"- {_rz}")

# ── Sección 3 · Gráfico de TradingView (activo seleccionado) ────────────────────
st.divider()
_tv_show = st.checkbox(
    "📈 Ver gráfico de TradingView del activo", value=False, key="md_tv_show",
    help="Chart en vivo del activo seleccionado. El widget gratuito de TradingView abre en los datos "
         "MÁS RECIENTES (no salta a una fecha histórica puntual): la fecha/hora elegida se muestra "
         "como referencia y hay un link para navegar al símbolo en TradingView.")
if _tv_show:
    from market_direction.ui.tradingview import (build_tradingview_html, tradingview_symbol,
                                                 tradingview_url)
    _tv_sym = tradingview_symbol(_md_tk)
    _tv_theme = "dark" if str(st.get_option("theme.base") or "light").lower() == "dark" else "light"
    _ivc, _txc = st.columns([1, 3])
    _tv_int = _ivc.selectbox(
        "Intervalo", ["1", "5", "15", "60", "D"], index=1, key="md_tv_int",
        format_func=lambda x: {"1": "1 min", "5": "5 min", "15": "15 min", "60": "1 h", "D": "Diario"}[x])
    with _txc:
        st.markdown(f"**{_tv_sym}** · zona horaria **ET**")
        st.caption(f"Referencia elegida: **{_md_date.isoformat()} · {_md_time.strftime('%H:%M')} ET** — "
                   f"el widget abre en los datos más recientes · "
                   f"[Abrir en TradingView ↗]({tradingview_url(_tv_sym, _tv_int)})")
    _components.html(build_tradingview_html(_tv_sym, interval=_tv_int, theme=_tv_theme, height=500),
                     height=520)

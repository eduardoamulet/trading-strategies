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
# Default = fecha y hora ACTUAL en horario del Este (la etiqueta es «Hora (ET)»); pandas trae la tz.
import pandas as _pd
_now_et = _pd.Timestamp.now(tz="America/New_York")
st.session_state.setdefault("md_time", _dt.time(_now_et.hour, _now_et.minute))
_c1, _c2, _c3, _c4 = st.columns([2, 1.8, 2.4, 1.3])
_md_tk = _c1.selectbox("Activo", _tks, index=(_tks.index("SPY") if "SPY" in _tks else 0), key="md_tk")
_md_date = _c2.date_input("Fecha", value=_now_et.date(), key="md_date")
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

# ── Sección 4 · Simulación Intradía ─────────────────────────────────────────────
from ui_charts import _render_lwc_chart                       # el MISMO gráfico de Backtesting
from market_direction import simulation as _msim
from market_direction.ui import simulation_view as _msv


@st.cache_resource
def _sim_downloader():
    """Downloader (Polygon + cache) para el gráfico reusado. Cacheado 1× por sesión."""
    import sys as _s
    _root = str(HERE.parent)
    if _root not in _s.path:
        _s.path.insert(0, _root)
    import config
    from adapter_polygon import PolygonAdapter
    from downloader import Downloader
    return Downloader(PolygonAdapter(config.POLYGON_API_KEY), HERE / "data")


st.divider()
# El panel queda ABIERTO cuando hay resultados (así el click en una celda —que hace rerun— no lo cierra).
with st.expander("🎬 Simulación Intradía", expanded=bool(st.session_state.get("sim_results"))):
    st.caption("Reproducí **minuto a minuto** cómo habría respondido el motor durante una sesión "
               "histórica. Cada celda guarda el **TradeSignal completo** (pasá el cursor para verlo) "
               "y el motor no se modifica: solo se lo llama por minuto con los datos cacheados.")

    _s1, _s2, _s3, _s4 = st.columns([2.8, 1.5, 1.2, 1.2])
    _sim_tks = _s1.multiselect("Activos", _tks,
                               default=[t for t in ("SPY", "QQQ", "IWM") if t in _tks],
                               key="sim_tks")
    _sim_date = _s2.date_input("Fecha", value=_now_et.date(), key="sim_date")
    _sim_ini = _s3.time_input("Hora inicio", value=_dt.time(9, 30), key="sim_ini")
    _sim_fin = _s4.time_input("Hora final", value=_dt.time(16, 0), key="sim_fin")

    if st.button("▶ Simular", type="primary", key="sim_run", disabled=not _sim_tks):
        _pbar = st.progress(0.0, text="Iniciando simulación…")

        def _sim_cb(done, total, tk, mn):
            _pbar.progress(done / total, text=f"Procesando {tk}… minuto {mn} · {done}/{total}")

        _sim_out = _msim.simulate_session(
            _sim_tks, _sim_date.isoformat(), _sim_ini.strftime("%H:%M"),
            _sim_fin.strftime("%H:%M"), progress_cb=_sim_cb)
        _pbar.empty()
        st.session_state["sim_results"] = _sim_out
        for _k in ("sim_pick_tk", "sim_pick_mn"):   # reset de la selección para el nuevo rango
            st.session_state.pop(_k, None)
        st.rerun()

    _sim = st.session_state.get("sim_results")
    if _sim and _sim.get("tickers") and _sim.get("minutes"):
        # Click en una celda → llega como ?sim_cell=TICKER|MINUTO (+ nonce _sn). Lo aplicamos al
        # selector (que rige el gráfico) ANTES de crear los widgets. El guard por _sn evita re-aplicar
        # en reruns que no vienen de un click (p.ej. mover el selector a mano).
        _qp_cell, _qp_sn = st.query_params.get("sim_cell"), st.query_params.get("_sn")
        if _qp_cell and _qp_sn and _qp_sn != st.session_state.get("_sim_last_sn"):
            st.session_state["_sim_last_sn"] = _qp_sn
            try:
                _qtk, _qmn = _qp_cell.split("|", 1)
                if _qtk in _sim["tickers"] and _qmn in _sim["minutes"]:
                    st.session_state["sim_pick_tk"] = _qtk
                    st.session_state["sim_pick_mn"] = _qmn
            except Exception:
                pass
        st.markdown(_msv.legend_html(), unsafe_allow_html=True)
        _mx_html, _mx_h = _msv.build_matrix_html(_sim)
        _components.html(_mx_html, height=_mx_h)

        # Inspección de una celda → el MISMO gráfico de Backtesting + panel lateral con el TradeSignal.
        st.markdown("**🔍 Inspeccionar** — **clic en una celda** (o elegí abajo); la matriz muestra el "
                    "detalle completo en **hover**:")
        _pk1, _pk2 = st.columns([1, 3])
        _psel_tk = _pk1.selectbox("Activo", _sim["tickers"], key="sim_pick_tk")
        _psel_mn = _pk2.select_slider("Minuto", _sim["minutes"], key="sim_pick_mn")
        _sig = _sim["results"].get(_psel_tk, {}).get(_psel_mn, {})
        _cg, _cs = st.columns([3, 1])
        with _cg:
            _render_lwc_chart(_sim_downloader(), _psel_tk, _sim["date"], _psel_mn, "1m", key="simintra")
        with _cs:
            _act = _sig.get("action", "—")
            _acol = _msv.ACTION_COLOR.get(_act, "#9ca3af")
            st.markdown(
                f"<div style='font-size:1.4rem;font-weight:700;color:{_acol};line-height:1.1'>{_act}</div>"
                f"<div style='color:#888;font-size:0.82rem;margin-bottom:6px'>{_psel_tk} · {_psel_mn} ET</div>",
                unsafe_allow_html=True)
            for _lk, _lv in _msv.signal_lines(_sig):
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;font-size:0.86rem'>"
                    f"<span style='color:#888'>{_lk}</span><b>{_lv}</b></div>", unsafe_allow_html=True)
            _rz = _sig.get("reasons") or []
            if _rz:
                st.markdown("<div style='margin-top:6px;font-weight:600;font-size:0.86rem'>Razones</div>",
                            unsafe_allow_html=True)
                st.markdown("".join(f"<div style='font-size:0.82rem'>✓ {_r}</div>" for _r in _rz),
                            unsafe_allow_html=True)

        # Exportación CSV / Excel (1 fila por ticker×minuto con todos los campos del TradeSignal).
        _rows = _msim.results_to_rows(_sim)
        _df = _pd.DataFrame(_rows)
        _e1, _e2, _e3 = st.columns([1, 1, 3])
        _fn = f"simulacion_{'-'.join(_sim['tickers'])}_{_sim['date']}"
        _e1.download_button("⬇ CSV", _df.to_csv(index=False).encode("utf-8"),
                            file_name=_fn + ".csv", mime="text/csv", use_container_width=True)
        import io as _io
        _xbuf = _io.BytesIO()
        with _pd.ExcelWriter(_xbuf, engine="openpyxl") as _xw:
            _df.to_excel(_xw, index=False, sheet_name="Simulacion")
        _e2.download_button("⬇ Excel", _xbuf.getvalue(), file_name=_fn + ".xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True)
        _e3.caption(f"{len(_rows)} filas · {len(_sim['tickers'])} activo(s) × "
                    f"{len(_sim['minutes'])} minuto(s)")

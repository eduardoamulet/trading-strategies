"""🟢 Operar — paper trading EN VIVO. La MISMA estrategia que el backtest (trading_core),
en tiempo real. Dos fuentes (selector): **Tradier sandbox** (paper real) o **Replay/demo**
(reproduce un día pasado con los datos de Polygon cacheados).

⚠ SANDBOX/paper únicamente — no mueve dinero real (live_runner gated por
LIVE_TRADING_ENABLED=False). Las órdenes las confirma el usuario con un click.

Esta página es solo orquestación + render Streamlit; la lógica vive en live_core.py (pura,
testeable) sobre los puertos de trading_core → idéntica con datos de Replay o de Tradier.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "options_replay")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
import strategy_core
import live_core as lc
from adapter_polygon import PolygonAdapter
from downloader import Downloader
from trading_core.adapters.polygon_backtest import PolygonBacktestData
from trading_core.adapters.simulated_broker import SimulatedBroker
from trading_core.domain import OrderRequest, OrderSide, Right

ET = "America/New_York"
DATA_DIR = _ROOT / "options_replay" / "data"


@st.cache_resource
def _downloader() -> Downloader:
    return Downloader(PolygonAdapter(config.POLYGON_API_KEY), DATA_DIR)


def _build_source():
    """(market, broker, now, expiry, label) según la fuente; None si falta config."""
    src = st.session_state.get("live_src", "Replay / demo")
    if src == "Tradier sandbox":
        try:
            from trading_core.live_runner import build_tradier_ports
            market, broker = build_tradier_ports()              # gated a sandbox adentro
        except Exception as e:  # noqa: BLE001
            st.error(f"No se pudo conectar a Tradier sandbox: {e}")
            st.info("Poné tu **token de sandbox** en `live_trader/secrets.py` "
                    "(`TRADIER_SANDBOX_TOKEN` + `TRADIER_SANDBOX_ACCOUNT_ID`), dejá "
                    "`LIVE_TRADING_ENABLED = False`, o usá **Replay / demo** mientras tanto.")
            return None
        now = pd.Timestamp.now(tz=ET)
        ticker = st.session_state.get("live_ticker", "QQQ")
        expiry = market.nearest_expiry(ticker, now.strftime("%Y-%m-%d")) or now.strftime("%Y-%m-%d")
        return market, broker, now, expiry, "🟢 Tradier sandbox (paper)"
    if src == "Alpaca paper":
        try:
            from trading_core.live_runner import build_alpaca_ports
            market, broker = build_alpaca_ports()               # paper=True FIJO adentro
        except Exception as e:  # noqa: BLE001
            st.error(f"No se pudo conectar a Alpaca paper: {e}")
            st.info("Revisá `ALPACA_API_KEY` + `ALPACA_API_SECRET` en el `config.py` raíz. "
                    "El builder usa SIEMPRE el entorno **paper** (no puede tocar la cuenta real).")
            return None
        now = pd.Timestamp.now(tz=ET)
        ticker = st.session_state.get("live_ticker", "QQQ")
        expiry = market.nearest_expiry(ticker, now.strftime("%Y-%m-%d")) or now.strftime("%Y-%m-%d")
        return market, broker, now, expiry, "🦙 Alpaca paper (opciones)"
    dl = _downloader()
    dl.resolution = "1min"
    market = PolygonBacktestData(dl)
    date = str(st.session_state.get("live_date", "2026-06-11"))
    now = pd.Timestamp(f"{date} 09:30", tz=ET) + pd.Timedelta(minutes=int(st.session_state.get("sim_min", 0)))
    return market, SimulatedBroker(market), now, date, f"⏪ Replay {date} · {now:%H:%M}"


def _params() -> dict:
    return dict(
        ticker=st.session_state.get("live_ticker", "QQQ"),
        tipo=st.session_state.get("live_tipo", "CALL y PUT"),
        inversion=float(st.session_state.get("live_inv", 1000.0)),
        call_pct=float(st.session_state.get("live_callpct", 50.0)),
        umbral=float(st.session_state.get("live_umbral", 10.0)),
        stop=float(st.session_state.get("live_stop", -100.0)),
        pmin=float(st.session_state.get("live_pmin", 0.30)),
        pmax=float(st.session_state.get("live_pmax", 5.00)),
        max_spread=float(st.session_state.get("live_maxspread", 0.10)),
        refuerzo_loss=float(st.session_state.get("live_refloss", 50.0)),
        refuerzo_max=int(st.session_state.get("live_refmax", 2)),
    )


def _rango_prima_live(ticker: str) -> tuple:
    """Rango óptimo de prima del ticker (ticker_info.json, centavos → ÷100); fallback de
    la casa $0.30–$0.50 — el MISMO criterio del backtest y del shadow trader."""
    import json as _json
    try:
        info = _json.loads((_ROOT / "options_replay" / "ticker_info.json")
                           .read_text(encoding="utf-8"))
        t = info.get(ticker.upper()) or {}
        return (float(t.get("rango_optimo_lo") or t.get("min") or 30.0) / 100.0,
                float(t.get("rango_optimo_hi") or t.get("max") or 50.0) / 100.0)
    except Exception:  # noqa: BLE001
        return 0.30, 0.50


class _UnTick:
    """Clock de UN solo intento para la selección en la página (sin loop bloqueante)."""

    def ticks(self, start, end):
        yield start


def _pdt_guard(broker) -> tuple:
    """(ok, msg) — regla Pattern Day Trader ANTES de abrir posiciones: con equity < $25k en
    cuenta margin, máx 3 day-trades por 5 días hábiles (un 0DTE comprado y vendido el mismo
    día = 1 day-trade). Lee la VERDAD del broker (Alpaca expone daytrade_count y
    pattern_day_trader en la cuenta); fuentes sin ese dato (Replay/Tradier sandbox) pasan
    con nota. Solo bloquea COMPRAS — cerrar posiciones no se bloquea jamás (atrapar un 0DTE
    abierto sería peor que la multa)."""
    tc = getattr(broker, "_trade", None)
    if tc is None or not hasattr(tc, "get_account"):
        return True, "PDT: esta fuente no expone datos de cuenta (replay/sandbox) — sin límite."
    try:
        a = tc.get_account()
        eq = float(getattr(a, "equity", 0) or 0)
        dt = getattr(a, "daytrade_count", None)
        dt = int(dt) if dt is not None else None
        if eq >= 25000:
            return True, (f"PDT: equity ${eq:,.0f} ≥ $25k — sin límite de day-trades"
                          + (f" (usados 5d: {dt})" if dt is not None else "") + ".")
        if dt is not None and dt >= 3:
            return False, (f"PDT: equity ${eq:,.0f} < $25k y ya hay {dt} day-trades en la "
                           "ventana de 5 días hábiles — una compra 0DTE más (con su venta "
                           "hoy) violaría la regla. Compra bloqueada por seguridad.")
        return True, (f"PDT: equity ${eq:,.0f} < $25k — quedan {max(0, 3 - (dt or 0))} "
                      "day-trade(s) en 5 días hábiles; cada ticker 0DTE comprado y vendido "
                      "hoy consume uno.")
    except Exception as e:  # noqa: BLE001 — el guard nunca rompe la página
        return True, f"PDT: no se pudo leer la cuenta ({type(e).__name__}) — sin chequeo."


def _alerts_handoff_view() -> None:
    """🔔 Alertas traídas de «Investep Academy IA» (botón «Operar → Live»): previsualiza el
    contrato por alerta con la selección de la casa (menor spread en rango óptimo) contra la
    FUENTE elegida arriba (Alpaca paper / Tradier sandbox / Replay) y compra LIMIT al ask.
    v1: la venta/TP automático de estas posiciones llega con la Fase 2 — gestión manual."""
    _ho = st.session_state.get("live_alerts_handoff")
    if not _ho:
        return
    st.markdown("### 🔔 Operar alertas de Investep (paper)")
    src = _build_source()
    if src is None:
        st.warning("Elegí/configurá una fuente arriba para previsualizar las alertas.")
        return
    market, broker, now, _exp0, label = src
    st.caption(f"**{len(_ho)} alerta(s)** · fuente actual: **{label}** (cambiala arriba si "
               "querés otra) · 1 posición por alerta — Sólo CALL/PUT según la señal — con la "
               "selección de contrato de la casa y compra **LIMIT al ask**. ⚠ Paper, sin "
               "dinero real. 🎯 El take-profit automático multi-posición llega en Fase 2: "
               "por ahora la gestión post-compra es manual (dashboard del broker).")
    import pandas as _pd
    _seed = _pd.DataFrame([{"Señal": f"{a.get('symbol')} {a.get('tipo')}",
                            "Inversión $": 1000.0} for a in _ho])
    _ed = st.data_editor(_seed, key="live_ho_editor", hide_index=True,
                         use_container_width=True, disabled=["Señal"],
                         column_config={"Inversión $": st.column_config.NumberColumn(
                             min_value=1.0, step=100.0, format="$%.0f")})
    _c1, _c2 = st.columns([2, 1])
    if _c1.button(f"🔍 Previsualizar {len(_ho)} (sin comprar)", type="primary",
                  use_container_width=True, key="live_ho_prev"):
        from trading_core.selection import SelectionParams, make_range_gate, select_single
        _rows = []
        for _i, _a in enumerate(_ho):
            _tk = str(_a.get("symbol") or "").upper()
            _tp = str(_a.get("tipo") or "").upper()
            _right = Right.CALL if _tp == "CALL" else Right.PUT
            _inv = float(_ed.iloc[_i]["Inversión $"])
            _fila = {"Señal": f"{_tk} {_tp}", "Contrato": "—", "Strike": "—", "Bid": "—",
                     "Ask": "—", "Spread": "—", "Cant.": 0, "Costo": "—", "Estado": "",
                     "_occ": "", "_ask": 0.0, "_qty": 0}
            try:
                _exp = market.nearest_expiry(_tk, now.strftime("%Y-%m-%d")) \
                    or now.strftime("%Y-%m-%d")
                _lo, _hi = _rango_prima_live(_tk)
                _leg, _ = select_single(market, _UnTick(), _tk, _exp, _right, now,
                                        SelectionParams(premium_min=_lo, premium_max=_hi,
                                                        window_min=0.0),
                                        make_range_gate(_lo, _hi, 0.10))
                if _leg is None or _leg.quote is None or not _leg.quote.ask:
                    _fila["Estado"] = f"✗ sin contrato en rango ${_lo:.2f}–${_hi:.2f}"
                else:
                    _q = _leg.quote
                    _qty = int(_inv // (_q.ask * 100.0))
                    _fila.update({
                        "Contrato": _leg.occ, "Strike": f"{_leg.strike:g}",
                        "Bid": f"${_q.bid:.2f}" if _q.bid else "—",
                        "Ask": f"${_q.ask:.2f}", "Spread": f"${_q.spread:.2f}",
                        "Cant.": _qty, "Costo": f"${_q.ask * _qty * 100.0:,.0f}",
                        "Estado": ("✅ lista" if _qty >= 1
                                   else "⚠ la inversión no alcanza para 1 contrato"),
                        "_occ": _leg.occ, "_ask": float(_q.ask), "_qty": _qty})
            except Exception as _e:  # noqa: BLE001 — una alerta no tumba a las demás
                _fila["Estado"] = f"✗ {type(_e).__name__}: {_e}"
            _rows.append(_fila)
        st.session_state["live_ho_preview"] = _rows
        st.rerun()
    if _c2.button("🧹 Descartar alertas", use_container_width=True, key="live_ho_drop"):
        for _k in ("live_alerts_handoff", "live_ho_preview", "live_ho_results"):
            st.session_state.pop(_k, None)
        st.rerun()

    _pv = st.session_state.get("live_ho_preview")
    if _pv:
        st.dataframe(_pd.DataFrame(_pv).drop(columns=["_occ", "_ask", "_qty"]),
                     hide_index=True, use_container_width=True)
        _n_ok = sum(1 for r in _pv if r["Estado"] == "✅ lista")
        st.caption(f"{_n_ok} de {len(_pv)} lista(s). Si cambiás la inversión o la fuente, "
                   "volvé a previsualizar (la compra usa exactamente esta vista).")
        _ok_pdt, _msg_pdt = _pdt_guard(broker)
        st.caption(("🛡 " if _ok_pdt else "⛔ ") + _msg_pdt)
        if st.button(f"✅ Comprar {_n_ok} (paper) — LIMIT al ask", type="primary",
                     disabled=(_n_ok == 0) or not _ok_pdt, key="live_ho_buy"):
            _res = []
            for r in _pv:
                if r["Estado"] != "✅ lista":
                    _res.append({"Señal": r["Señal"], "Resultado": "— salteada"})
                    continue
                try:
                    _fill = broker.execute(OrderRequest(occ=r["_occ"], side=OrderSide.BUY,
                                                        qty=int(r["_qty"]),
                                                        limit=float(r["_ask"]), ts=now), now)
                    _res.append({"Señal": r["Señal"],
                                 "Resultado": (f"✅ comprada · {_fill.qty} @ ${_fill.price:.2f}"
                                               if _fill.qty else "⚠ no se llenó / cancelada")})
                except Exception as _e:  # noqa: BLE001
                    _res.append({"Señal": r["Señal"], "Resultado": f"⚠ {_e}"})
            st.session_state["live_ho_results"] = _res
            st.session_state.pop("live_ho_preview", None)
            st.rerun()
    _res = st.session_state.get("live_ho_results")
    if _res:
        st.markdown("##### Resultado de las compras")
        st.dataframe(_pd.DataFrame(_res), hide_index=True, use_container_width=True)
        st.caption("Las posiciones quedan en la cuenta **paper** del broker elegido "
                   "(en Alpaca: app.alpaca.markets → Paper → Positions).")
    st.divider()


# ════════════════════════════════════ Render ════════════════════════════════════
st.title("🟢 Operar — paper trading en vivo")
st.caption("La MISMA estrategia que el backtest (trading_core), en tiempo real. "
           "**SANDBOX/paper** — no mueve dinero real; las órdenes las confirmás vos con un click.")
# Que el contenido que se re-renderiza (la captura en vivo) NO se atenúe / vea deshabilitado.
st.markdown("<style>[data-stale='true']{opacity:1 !important;transition:none !important;}</style>",
            unsafe_allow_html=True)

c1, c2, c3 = st.columns([2, 2, 2])
c1.radio("Fuente", ["Replay / demo", "Tradier sandbox", "Alpaca paper"], key="live_src",
         horizontal=True)
c2.text_input("Ticker", value="QQQ", key="live_ticker")
c3.selectbox("Tipo de operación", list(lc.RIGHTS.keys()), key="live_tipo")
if st.session_state.get("live_src") == "Replay / demo":
    st.session_state.setdefault("sim_min", 0)
    d1, d2 = st.columns([3, 1])
    d1.text_input("Fecha (YYYY-MM-DD)", value="2026-06-11", key="live_date")
    if d2.button("⟲ Reiniciar reloj", use_container_width=True):
        st.session_state["sim_min"] = 0

# 🔔 Alertas de Investep (si llegaste con el botón «Operar → Live»): arriba de todo.
_alerts_handoff_view()

e1, e2, e3, e4, e5 = st.columns(5)
e1.number_input("Inversión ($)", min_value=1.0, value=1000.0, step=100.0, key="live_inv")
e2.number_input("Inversión CALL (%)", 0.0, 100.0, value=50.0, step=5.0, key="live_callpct")
e3.number_input("Umbral ROI (%)", value=10.0, step=5.0, key="live_umbral")
e4.number_input("Stop loss (%)", value=-100.0, step=10.0, key="live_stop")
e5.number_input("Spread máx ($)", min_value=0.0, value=0.10, step=0.01, key="live_maxspread")
f1, f2, f3, f4 = st.columns(4)
f1.number_input("Prima mín ($)", min_value=0.0, value=0.30, step=0.05, key="live_pmin")
f2.number_input("Prima máx ($)", min_value=0.0, value=5.00, step=0.05, key="live_pmax")
if st.session_state.get("live_tipo") == "CALL y PUT (Refuerzo)":
    f3.number_input("Umbral pérdida refuerzo (%)", min_value=1.0, max_value=99.0,
                    value=50.0, step=5.0, key="live_refloss")
    f4.number_input("Refuerzos (máx)", 0, 10, value=2, key="live_refmax")

# Barra de control de la captura en vivo (FUERA del fragment → no bloquea el resto al refrescar).
lc1, lc2, _lc3 = st.columns([2, 2, 4])
lc1.toggle("🔴 Captura en vivo", key="live_capture",
           help="Prende la actualización automática de la tabla de strikes (y la posición) a "
                "intervalos. Apagado = estático; tomá la data a demanda con «🔄 Actualizar ahora».")
if lc2.button("🔄 Actualizar ahora", use_container_width=True):
    st.session_state["live_fetch_once"] = True   # fuerza UNA captura puntual a demanda
    if st.session_state.get("live_src") == "Replay / demo":
        st.session_state["sim_min"] = min(380, int(st.session_state.get("sim_min", 0)) + 1)

st.divider()

if st.session_state.get("live_closed"):
    _c = st.session_state["live_closed"]
    st.success(f"✅ Operación cerrada ({_c['razon']}) · ROI {_c['roi']:+.1f}% · P&L ${_c['pnl']:+,.2f}")
    if st.button("Nueva operación"):
        st.session_state.pop("live_closed", None)
        st.rerun()

# run_every SIEMPRE con valor: cambiarlo de None→valor NO reinicia el timer del fragment en
# Streamlit (por eso la captura no arrancaba). El fragment aislado no bloquea el resto; el
# TRABAJO (avanzar reloj + pegarle a los quotes) se gatea según la captura, ADENTRO.
_refresh = "1.5s" if st.session_state.get("live_src") == "Replay / demo" else "3s"


@st.fragment(run_every=_refresh)
def live_view():
    _replay = st.session_state.get("live_src") == "Replay / demo"
    _cap = bool(st.session_state.get("live_capture", False))
    _once = bool(st.session_state.pop("live_fetch_once", False))
    built = _build_source()
    if built is None:
        return
    market, broker, now, expiry, label = built
    p = _params()
    st.markdown(f"**{label}**  ·  {p['ticker']} · {p['tipo']}")

    pos = st.session_state.get("live_pos")
    if pos is None:
        # ---------- PRE-ENTRADA: la cadena PRIMERO + candidatos + comprar ----------
        # Trae quotes SOLO con la captura prendida, a demanda, o si no hay snapshot todavía; si
        # no, re-renderiza el último snapshot (no pega a la API → on-demand de verdad).
        if _cap or _once or "live_chain" not in st.session_state:
            _cand = lc.candidates(market, p["ticker"], expiry, now, p)
            _df, _spot = lc.chain_df(market, p["ticker"], expiry, now, _cand)
            st.session_state["live_chain"] = {"df": _df, "spot": _spot, "cand": _cand, "now": str(now)[11:16]}
        _snap = st.session_state.get("live_chain", {"df": pd.DataFrame(), "spot": None, "cand": {}, "now": ""})
        cand, df, spot = _snap["cand"], _snap["df"], _snap["spot"]
        st.markdown((f"#### 📈 Strikes en vivo · spot ≈ {spot:.2f}" if spot else "#### 📈 Strikes")
                    + (f"  ·  🕐 {_snap['now']}" if _snap["now"] else ""))
        if df.empty:
            st.warning("Sin cadena para este ticker/fecha (¿0DTE disponible?).")
        else:
            st.dataframe(lc.chain_style(df, spot), hide_index=True, use_container_width=True,
                         column_config={
                             "C bid": st.column_config.NumberColumn("C bid", format="$%.2f"),
                             "C ask": st.column_config.NumberColumn("C ask", format="$%.2f"),
                             "P bid": st.column_config.NumberColumn("P bid", format="$%.2f"),
                             "P ask": st.column_config.NumberColumn("P ask", format="$%.2f"),
                             "Strike": st.column_config.NumberColumn("Strike", format="%.0f")})
            st.caption("🟩 In the money · ⬜ Out of the money · ◀ ATM · ✅ candidato del sistema")
        need = lc.RIGHTS.get(p["tipo"], ())
        ok = all(cand.get(r) is not None for r in need)
        prop = " · ".join(f"{r.value} {cand[r].strike:.0f} @ ${cand[r].quote.ask:.2f}"
                          for r in cand if cand.get(r) and cand[r].quote)
        if ok and prop:
            st.success(f"🎯 Propuesta del sistema: **{prop}**")
            if st.button("▶ Comprar propuesta (paper)", type="primary", use_container_width=True):
                _ok_pdt, _msg_pdt = _pdt_guard(broker)
                if not _ok_pdt:
                    st.error("⛔ " + _msg_pdt)
                else:
                    try:
                        st.session_state["live_pos"] = lc.buy_proposal(broker, market, now, p["ticker"], expiry, p, cand)
                        st.session_state["live_marks"] = []
                        st.session_state["live_fills"] = [
                            {"hora": str(now)[11:16], "occ": l["occ"], "lado": "BUY",
                             "qty": l["qty"], "precio": l["entry_price"]}
                            for l in st.session_state["live_pos"]["legs"]]
                        st.session_state["live_fetch_once"] = True
                        st.session_state.pop("live_m", None)
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(f"No se pudo comprar: {e}")
        else:
            st.info("Esperando un contrato candidato que pase Opción 1 (prima en rango + spread). "
                    "Prendé **🔴 Captura en vivo** o tocá **🔄 Actualizar ahora**.")
    else:
        # ---------- POSICIÓN ABIERTA: métricas en vivo + pestañas ----------
        if _cap or _once or "live_m" not in st.session_state:
            _m = lc.mark(market, pos, now)
            st.session_state["live_m"] = _m
            st.session_state["live_m_now"] = str(now)[11:16]
            st.session_state.setdefault("live_marks", []).append(
                {"ts": str(now)[11:16], "ROI %": round(_m["roi"], 1), "Combined %": round(_m["combined"], 1),
                 **{f"{r.value} %": round(d["pct"], 1) for r, d in _m["per"].items()}})
        m = st.session_state.get("live_m") or lc.mark(market, pos, now)
        marks = st.session_state.get("live_marks", [])
        sig = strategy_core.exit_decision(m["roi"], pos["umbral"], pos["stop"])
        head = ("🎯 Umbral alcanzado" if sig == "take_profit"
                else "🛑 Stop alcanzado" if sig == "stop_loss" else "⏳ En posición")
        arrow = "▲" if m["pnl"] >= 0 else "▼"
        _nr = len(pos.get("reinforcements", []))
        st.markdown(f"#### {arrow} {head} · {pos['tipo']} · entró {pos['entry_ts'][11:16]} → "
                    f"{st.session_state.get('live_m_now', str(now)[11:16])}"
                    + (f" · ➕{_nr} refuerzo(s)" if _nr else ""))
        g = st.columns(6)
        g[0].metric("Inversión", f"${m['cost']:,.2f}")
        for i, r in enumerate((Right.CALL, Right.PUT)):
            if r in m["per"]:
                d = m["per"][r]
                g[1 + i].metric(f"Ganancia {r.value}", f"${d['pnl']:+,.2f}", f"{d['pct']:+.1f}%")
        g[3].metric("Ganancia total", f"${m['pnl']:+,.2f}", f"{m['roi']:+.1f}%")
        g[4].metric("Combined % exit", f"{m['combined']:+.1f}%")
        g[5].metric("Capital acumulado", f"${m['capital']:,.2f}")

        # Salida tiene prioridad; si NO hay salida, se evalúa el REFUERZO (martingala).
        rcand = (lc.reinforce_candidate(pos, m)
                 if (pos["tipo"] == "CALL y PUT (Refuerzo)" and sig is None) else None)
        if sig is not None:
            st.warning(f"El sistema sugiere CERRAR ({head}). Confirmá con el botón.")
        elif rcand is not None:
            _rp = m["per"][rcand]["pct"]
            st.warning(f"🎯 La pierna **{rcand.value}** cae **{_rp:+.0f}%** — el sistema sugiere "
                       f"REFORZAR (refuerzo {_nr + 1}/{pos['refuerzo_max']}).")
            if st.button(f"➕ Reforzar {rcand.value} (paper)", use_container_width=True):
                try:
                    ev = lc.reinforce(broker, market, now, pos, rcand)
                    st.session_state["live_pos"] = pos
                    st.session_state.setdefault("live_fills", []).append(
                        {"hora": str(now)[11:16],
                         "occ": next(l["occ"] for l in pos["legs"] if l["right"] == rcand.value),
                         "lado": "BUY (refuerzo)", "qty": ev["qty"], "precio": ev["price"]})
                    st.session_state["live_fetch_once"] = True
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"No se pudo reforzar: {e}")
        if st.button("💵 Cerrar / Vender (paper)", type="primary", use_container_width=True):
            for r in (Right.CALL, Right.PUT):
                rl = [l for l in pos["legs"] if l["right"] == r.value]
                if rl:
                    qty = sum(l["qty"] for l in rl)
                    f = broker.execute(OrderRequest(rl[0]["occ"], OrderSide.SELL, qty, ts=now), now)
                    st.session_state.setdefault("live_fills", []).append(
                        {"hora": str(now)[11:16], "occ": rl[0]["occ"], "lado": "SELL",
                         "qty": qty, "precio": f.price})
            st.session_state["live_closed"] = {"roi": m["roi"], "pnl": m["pnl"], "razon": sig or "manual"}
            st.session_state["live_pos"] = None
            st.session_state.pop("live_m", None)
            st.session_state.pop("live_chain", None)
            st.session_state["live_fetch_once"] = True
            st.rerun()

        t1, t2, t3, t4 = st.tabs(["📑 Operaciones", "Strikes", "📈 Gráfico", "📋 Tabla minuto a minuto"])
        with t1:
            st.dataframe(pd.DataFrame(st.session_state.get("live_fills", [])),
                         hide_index=True, use_container_width=True)
        with t2:
            df2, spot2 = lc.chain_df(market, pos["ticker"], pos["expiry"], now,
                                     {r: None for r in (Right.CALL, Right.PUT)})
            st.dataframe(lc.chain_style(df2, spot2) if not df2.empty else df2,
                         hide_index=True, use_container_width=True)
            if not df2.empty:
                st.caption("🟩 In the money · ⬜ Out of the money · ◀ ATM")
        with t3:
            if len(marks) > 1:
                mdf = pd.DataFrame(marks)
                fig = go.Figure(go.Scatter(x=list(range(len(mdf))), y=mdf["ROI %"], line=dict(color="#1f77b4")))
                fig.add_hline(y=pos["umbral"], line_dash="dot", line_color="#2e7d32")
                fig.update_layout(height=260, margin=dict(l=10, r=10, t=10, b=10),
                                  yaxis_title="ROI %", xaxis_title="tick")
                st.plotly_chart(fig, use_container_width=True)
        with t4:
            st.dataframe(pd.DataFrame(marks), hide_index=True, use_container_width=True)

    # Avanzar el reloj de Replay SOLO con la captura prendida (auto-play). Al final, para que
    # el próximo tick del fragment muestre un minuto nuevo.
    if _replay and _cap:
        st.session_state["sim_min"] = min(380, int(st.session_state.get("sim_min", 0)) + 1)


live_view()


# ── 🧲 GEX del día: el clasificador de régimen (dealers largos/cortos gamma) ──
st.divider()
with st.expander("🧲 GEX del día — régimen de dealers (rango vs tendencia)", expanded=False):
    try:
        import gex as _gexm
        from datetime import date as _date
        _hoy_gx = _date.today().isoformat()
        _rows_gx = []
        for _tk_gx in ("QQQ", "SPY", "IWM"):
            _g = _gexm.gex_mas_reciente(_tk_gx, _hoy_gx)
            if not _g:
                _rows_gx.append({"Ticker": _tk_gx, "Referencia": "sin snapshot",
                                 "Régimen": "—", "GEX (M$/1%)": None, "Flip": None,
                                 "Spot": None, "Δ vs flip %": None,
                                 "Call wall": None, "Put wall": None})
                continue
            _rows_gx.append({"Ticker": _tk_gx,
                             "Referencia": f"{_g['fecha']} {_g['momento']}",
                             "Régimen": _g["regimen"],
                             "GEX (M$/1%)": _g["gex_total_musd"],
                             "Flip": _g["flip"], "Spot": _g["spot"],
                             "Δ vs flip %": _g["spot_vs_flip_pct"],
                             "Call wall": _g["call_wall"], "Put wall": _g["put_wall"]})
        st.dataframe(pd.DataFrame(_rows_gx).style.map(
            lambda v: ("color:#16a34a;font-weight:700" if "GEX+" in str(v)
                       else "color:#dc2626;font-weight:700" if "GEX-" in str(v) else ""),
            subset=["Régimen"]), hide_index=True, use_container_width=True)
        st.caption("**Cómo leerlo** — 🟢 rango (GEX+): dealers largos gamma amortiguan el "
                   "movimiento → pinning/lateral: el enemigo del straddle comprado es el "
                   "theta (time-stop, no girar). 🔴 tendencia (GEX−): sus coberturas "
                   "ACELERAN el movimiento → dejá correr la ganadora y cortá rápido la "
                   "perdedora; girar solo acá. **Flip** = nivel zero-gamma (cruzarlo cambia "
                   "el régimen) · **walls** = strikes imán/freno por gamma×OI. Convención "
                   "naive con OI D-1 y universo DTE≤5 ±10% — clasificador de régimen, no "
                   "oráculo. Fuente: snapshots 09:35/15:45 · `py gex.py`.")
    except Exception as _ge:  # noqa: BLE001 — el GEX nunca rompe la página
        st.caption(f"GEX no disponible: {_ge}")

# ── 🕶 Shadow trader (Fase 1): decisión diaria SIN operar ─────────────────────
st.divider()
with st.expander("🕶 Shadow trader — decisión diaria sin operar (Fase 1)", expanded=False):
    import json as _json
    import sqlite3 as _sq
    _SH_CFG = DATA_DIR / "shadow_config.json"
    try:
        _sh_cfg = _json.loads(_SH_CFG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — sin config todavía
        _sh_cfg = {}
    st.caption("Cada día hábil a las **09:31 ET** registra qué habría hecho el sistema — "
               "contratos elegidos con datos EN VIVO de Alpaca y a qué ask — **sin mandar "
               "órdenes**; a las **15:50** captura el bid de cierre (P&L hipotético). "
               "Base: `data/shadow_trader.db` · reporte: `py shadow_trader.py --reporte`.")
    _sh_pb = st.checkbox(
        "Usar el playbook para decidir (desmarcado: entra TODOS los días hábiles)",
        value=bool(_sh_cfg.get("usar_playbook", False)), key="shadow_usar_pb",
        help="Desmarcado (default): selecciona contratos todos los días hábiles — máxima "
             "recolección de datos de calibración. Marcado: replica la política del "
             "«(playbook automático)»: solo días OPERAR + estado 🟢 operable del veredicto "
             "vigente; el resto se saltea registrando el motivo.")
    if bool(_sh_cfg.get("usar_playbook", False)) != _sh_pb:
        _SH_CFG.parent.mkdir(parents=True, exist_ok=True)
        _SH_CFG.write_text(_json.dumps({**_sh_cfg, "usar_playbook": _sh_pb}, indent=2,
                                       ensure_ascii=False), encoding="utf-8")
        st.toast("🕶 Shadow: " + ("usará el playbook vigente"
                                  if _sh_pb else "entrará todos los días hábiles"))
    try:
        _con_sh = _sq.connect(str(DATA_DIR / "shadow_trader.db"))
        _tiene_gx = {r[1] for r in _con_sh.execute("PRAGMA table_info(shadow_decisions)")}
        _col_gx = ("COALESCE(gex_regimen,'')" if "gex_regimen" in _tiene_gx else "''")
        _rows_sh = _con_sh.execute(
            "SELECT fecha, weekday, ticker, decision, COALESCE(call_occ,''), "
            "COALESCE(call_ask,''), COALESCE(put_occ,''), COALESCE(put_ask,''), "
            f"COALESCE(costo_estimado,''), {_col_gx}, COALESCE(motivo,'') "
            "FROM shadow_decisions ORDER BY fecha DESC, ticker LIMIT 15").fetchall()
        _con_sh.close()
        if _rows_sh:
            st.dataframe(pd.DataFrame(_rows_sh, columns=[
                "Fecha", "Día", "Ticker", "Decisión", "CALL", "ask C", "PUT", "ask P",
                "Costo $", "Régimen GEX", "Motivo"]), hide_index=True,
                use_container_width=True)
        else:
            st.caption("Todavía sin decisiones registradas — la primera cae el próximo "
                       "día hábil a las 09:31.")
    except Exception:  # noqa: BLE001 — sin base todavía
        st.caption("Todavía sin decisiones registradas.")

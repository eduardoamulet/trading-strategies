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
    """(market, broker, now, expiry, label) según la fuente; None si falta config (Tradier)."""
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


# ════════════════════════════════════ Render ════════════════════════════════════
st.header("🟢 Operar — paper trading en vivo")
st.caption("La MISMA estrategia que el backtest (trading_core), en tiempo real. "
           "**SANDBOX/paper** — no mueve dinero real; las órdenes las confirmás vos con un click.")
# Que el contenido que se re-renderiza (la captura en vivo) NO se atenúe / vea deshabilitado.
st.markdown("<style>[data-stale='true']{opacity:1 !important;transition:none !important;}</style>",
            unsafe_allow_html=True)

c1, c2, c3 = st.columns([2, 2, 2])
c1.radio("Fuente", ["Replay / demo", "Tradier sandbox"], key="live_src", horizontal=True)
c2.text_input("Ticker", value="QQQ", key="live_ticker")
c3.selectbox("Tipo de operación", list(lc.RIGHTS.keys()), key="live_tipo")
if st.session_state.get("live_src") == "Replay / demo":
    st.session_state.setdefault("sim_min", 0)
    d1, d2 = st.columns([3, 1])
    d1.text_input("Fecha (YYYY-MM-DD)", value="2026-06-11", key="live_date")
    if d2.button("⟲ Reiniciar reloj", use_container_width=True):
        st.session_state["sim_min"] = 0
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
    if st.session_state.get("live_src") == "Replay / demo":
        st.session_state["sim_min"] = min(380, int(st.session_state.get("sim_min", 0)) + 1)

st.divider()

if st.session_state.get("live_closed"):
    _c = st.session_state["live_closed"]
    st.success(f"✅ Operación cerrada ({_c['razon']}) · ROI {_c['roi']:+.1f}% · P&L ${_c['pnl']:+,.2f}")
    if st.button("Nueva operación"):
        st.session_state.pop("live_closed", None)
        st.rerun()

# Solo auto-refresca si la "Captura en vivo" está prendida; si no, run_every=None (estático,
# se actualiza a demanda con el botón). El fragment aislado = no bloquea el resto de la pantalla.
_refresh = (("1.5s" if st.session_state.get("live_src") == "Replay / demo" else "3s")
            if st.session_state.get("live_capture") else None)


@st.fragment(run_every=_refresh)
def live_view():
    built = _build_source()
    if built is None:
        return
    market, broker, now, expiry, label = built
    p = _params()
    st.markdown(f"**{label}**  ·  {p['ticker']} · {p['tipo']}")

    pos = st.session_state.get("live_pos")
    if pos is None:
        # ---------- PRE-ENTRADA: la cadena PRIMERO + candidatos + comprar ----------
        cand = lc.candidates(market, p["ticker"], expiry, now, p)
        df, spot = lc.chain_df(market, p["ticker"], expiry, now, cand)
        st.markdown(f"#### 📈 Strikes en vivo · spot ≈ {spot:.2f}" if spot else "#### 📈 Strikes")
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
                try:
                    st.session_state["live_pos"] = lc.buy_proposal(broker, market, now, p["ticker"], expiry, p, cand)
                    st.session_state["live_marks"] = []
                    st.session_state["live_fills"] = [
                        {"hora": str(now)[11:16], "occ": l["occ"], "lado": "BUY",
                         "qty": l["qty"], "precio": l["entry_price"]}
                        for l in st.session_state["live_pos"]["legs"]]
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"No se pudo comprar: {e}")
        else:
            st.info("Esperando un contrato candidato que pase Opción 1 (prima en rango + spread). "
                    "En Replay, dejá correr el reloj.")
    else:
        # ---------- POSICIÓN ABIERTA: métricas en vivo + pestañas ----------
        m = lc.mark(market, pos, now)
        marks = st.session_state.setdefault("live_marks", [])
        marks.append({"ts": str(now)[11:16], "ROI %": round(m["roi"], 1),
                      "Combined %": round(m["combined"], 1),
                      **{f"{r.value} %": round(d["pct"], 1) for r, d in m["per"].items()}})
        sig = strategy_core.exit_decision(m["roi"], pos["umbral"], pos["stop"])
        head = ("🎯 Umbral alcanzado" if sig == "take_profit"
                else "🛑 Stop alcanzado" if sig == "stop_loss" else "⏳ En posición")
        arrow = "▲" if m["pnl"] >= 0 else "▼"
        _nr = len(pos.get("reinforcements", []))
        st.markdown(f"#### {arrow} {head} · {pos['tipo']} · entró {pos['entry_ts'][11:16]} → "
                    f"{str(now)[11:16]}" + (f" · ➕{_nr} refuerzo(s)" if _nr else ""))
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

    # Avanzar el reloj de Replay solo si la captura en vivo está prendida (auto-play).
    if st.session_state.get("live_src") == "Replay / demo" and st.session_state.get("live_capture"):
        st.session_state["sim_min"] = min(380, int(st.session_state.get("sim_min", 0)) + 1)


live_view()

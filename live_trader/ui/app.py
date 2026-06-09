"""UI Streamlit — CONTROL + DASHBOARD. NO ejecuta el monitoreo/auto-TP (eso vive
en el daemon). La UI manda comandos a la store y muestra el estado.

Correr:
    cd Traiding && py -m streamlit run live_trader/ui/app.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent.parent
# Solo live_trader/ al frente del path (NO Traiding/, que tiene otro settings.py).
sys.path.insert(0, str(HERE))           # live_trader/

import settings
from brokers.tradier import TradierAdapter
from core.order_manager import OrderManager
from core.risk import RiskGuard
from core.selector import ContractSelector, NoLiquidContract
from core.store import Store

# try/except: en modo standalone funciona normal; dentro del trading_suite
# (st.navigation) set_page_config ya se llamó en el entry → ignoramos el error.
try:
    st.set_page_config(page_title="Live Trader (sandbox)", layout="wide")
except Exception:
    pass


@st.cache_resource
def _services():
    store = Store(settings.DB_PATH)
    broker = TradierAdapter()
    return store, broker, OrderManager(broker, store), RiskGuard(store), ContractSelector()


# ---- Banner de modo ----
_mode_live = settings.LIVE_TRADING_ENABLED
st.title("🟢 Live Trader" + ("  —  ⚠ LIVE (dinero real)" if _mode_live else "  —  SANDBOX (paper)"))
if _mode_live:
    st.error("⚠ MODO LIVE ACTIVO — las órdenes usan dinero real.")
else:
    st.info("Modo SANDBOX (paper). Las órdenes son simuladas. Probá acá antes de pensar en real.")

try:
    store, broker, om, risk, selector = _services()
except Exception as e:
    st.error(f"No se pudo inicializar (¿falta TRADIER_SANDBOX_TOKEN?): {e}")
    st.stop()

# ===========================================================================
# Operar ALERTAS seleccionadas (llegan de la página "Alertas" → "Operar en vivo")
# ===========================================================================
import pandas as _pd  # noqa: E402

_alerts_ho = st.session_state.get("live_alerts_handoff")
if _alerts_ho:
    st.header("🔔 Operar alertas seleccionadas (paper)")
    st.caption(
        f"{len(_alerts_ho)} alerta(s) traídas de **Alertas**. Cada una abre 1 posición "
        "(Sólo CALL/PUT según Tipo) con la MISMA selección de contrato que el backtest. "
        "El daemon luego monitorea y vende al Umbral de ROI. **Requiere mercado abierto.**"
    )
    _aa1, _aa2, _aa3 = st.columns(3)
    _la_inv = _aa1.number_input("Inversión por alerta ($)", min_value=1.0, value=1000.0,
                                step=100.0, key="la_inv")
    _la_roi = _aa2.number_input("Umbral de ROI (%)", min_value=1.0, value=20.0, step=5.0, key="la_roi")
    _la_strat = _aa3.radio("Strike", ["atm", "itm"], horizontal=True, key="la_strat",
                           format_func=lambda s: "ATM" if s == "atm" else "1-ITM")
    st.dataframe(_pd.DataFrame([{"Acción": a.get("symbol"), "Tipo": a.get("tipo")} for a in _alerts_ho]),
                 hide_index=True, use_container_width=True)
    _oa1, _oa2 = st.columns([2, 1])
    if _oa1.button(f"▶ Operar {len(_alerts_ho)} alerta(s) (paper)", type="primary",
                   use_container_width=True):
        from core.alert_entry import AlertEntry, EntryError, enter_from_alert
        _res = []
        for a in _alerts_ho:
            ae = AlertEntry(alert_id=str(a.get("id")), underlying=str(a.get("symbol", "")).upper(),
                            side=str(a.get("tipo", "")).upper(), inversion=float(_la_inv),
                            roi_target_pct=float(_la_roi), strategy=_la_strat)
            try:
                pos = enter_from_alert(broker, store, selector, risk, om, ae)
                _res.append({"Acción": a.get("symbol"), "Tipo": a.get("tipo"),
                             "Estado": f"✅ comprada · {pos.qty} @ ${pos.entry_price:.2f}"})
            except EntryError as ex:
                _res.append({"Acción": a.get("symbol"), "Tipo": a.get("tipo"), "Estado": f"⚠ {ex}"})
            except Exception as ex:
                _res.append({"Acción": a.get("symbol"), "Tipo": a.get("tipo"), "Estado": f"⚠ error: {ex}"})
        st.session_state["live_alerts_results"] = _res
        st.session_state.pop("live_alerts_handoff", None)
        st.rerun()
    if _oa2.button("Descartar", use_container_width=True):
        st.session_state.pop("live_alerts_handoff", None)
        st.rerun()
    st.markdown("---")

_la_res = st.session_state.get("live_alerts_results")
if _la_res:
    st.markdown("##### Resultado de operar alertas")
    st.dataframe(_pd.DataFrame(_la_res), hide_index=True, use_container_width=True)
    if st.button("Limpiar resultado", key="la_clear"):
        st.session_state.pop("live_alerts_results", None)
        st.rerun()
    st.markdown("---")

# ===========================================================================
# Panel de ENTRADA
# ===========================================================================
st.header("1 · Analizar y comprar")
c1, c2, c3, c4 = st.columns(4)
ticker = c1.text_input("Ticker", value="SPY").upper().strip()
side = c2.selectbox("Tipo", ["CALL", "PUT"])
qty = c3.number_input("Contratos", min_value=1, value=1, step=1)
roi_target = c4.number_input("ROI objetivo (%)", min_value=1.0, value=20.0, step=5.0)
strategy = st.radio("Estrategia de strike", ["atm", "itm"], horizontal=True,
                    format_func=lambda s: "ATM (más cercano al spot)" if s == "atm" else "1-ITM")

# --- Filtros de liquidez (editables) ---
with st.expander("⚙ Filtros de liquidez", expanded=False):
    f1, f2 = st.columns(2)
    min_oi = f1.number_input(
        "Open Interest mínimo", min_value=0, value=int(settings.RISK["min_open_interest"]),
        step=50, key="lt_min_oi",
        help="Gate de liquidez principal (siempre poblado, incluso pre-market).")
    min_vol = f2.number_input(
        "Volumen mínimo", min_value=0, value=int(settings.RISK["min_volume"]),
        step=10, key="lt_min_vol",
        help="0 = filtro off. El volumen es ~0 al abrir el mercado y en sandbox; "
             "subilo solo si querés exigir flujo intradía ya formado.")

if st.button("🔎 Analizar mejor contrato", use_container_width=True):
    try:
        spot = broker.get_underlying_price(ticker)
        chain = broker.get_option_chain(ticker)
        # Selector con los filtros de la UI (no el cacheado con defaults).
        ui_selector = ContractSelector(min_oi=int(min_oi), min_vol=int(min_vol))
        best = ui_selector.select(chain, side, spot, strategy=strategy)
        st.session_state["selected"] = {
            "occ": best.occ, "underlying": best.underlying, "strike": best.strike,
            "expiry": best.expiry, "bid": best.bid, "ask": best.ask,
            "spread": best.spread, "delta": best.delta, "volume": best.volume,
            "oi": best.open_interest, "spot": spot, "qty": int(qty),
            "roi_target": float(roi_target),
        }
    except NoLiquidContract as e:
        st.warning(str(e))
    except Exception as e:
        st.error(f"Error obteniendo chain: {e}")

sel = st.session_state.get("selected")
if sel:
    st.subheader("Contrato seleccionado")
    m = st.columns(8)
    m[0].metric("Strike", f"{sel['strike']:g}")
    m[1].metric("Expiry", sel["expiry"])
    m[2].metric("Bid", f"${sel['bid']:.2f}")
    m[3].metric("Ask", f"${sel['ask']:.2f}")
    m[4].metric("Spread", f"${sel['spread']:.2f}")
    m[5].metric("Delta", f"{sel['delta']:.2f}" if sel["delta"] is not None else "n/a")
    m[6].metric("Volumen", f"{sel['volume']:,}")
    m[7].metric("Open Int.", f"{sel['oi']:,}")
    est_cost = sel["ask"] * sel["qty"] * 100
    st.caption(f"Costo estimado: **${est_cost:,.2f}** ({sel['qty']} contrato(s) @ ${sel['ask']:.2f}) · "
               f"OCC `{sel['occ']}` · spot ${sel['spot']:.2f}")

    # Validación de riesgo PRE-orden
    from core.models import Contract as _C
    _contract = _C(occ=sel["occ"], underlying=sel["underlying"], strike=sel["strike"],
                   expiry=sel["expiry"], right="C" if side == "CALL" else "P",
                   bid=sel["bid"], ask=sel["ask"], last=sel["ask"],
                   volume=sel["volume"], open_interest=sel["oi"], delta=sel["delta"])
    try:
        account = broker.get_account()
        errs = risk.validate_entry(_contract, sel["qty"], account)
    except Exception as e:
        errs = [f"No se pudo validar cuenta: {e}"]

    if errs:
        st.error("Validación de riesgo NO superada:\n\n- " + "\n- ".join(errs))
    else:
        st.success("Validación de riesgo OK.")
        confirm = True
        if _mode_live and settings.RISK["require_confirm_live"]:
            confirm = st.checkbox("Confirmo enviar orden REAL", value=False)
        if st.button("🟢 Comprar", type="primary", disabled=not confirm, use_container_width=True):
            try:
                key = f"buy_{sel['occ']}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                pos = om.buy(_contract, sel["qty"], sel["roi_target"], key)
                st.success(f"Comprado: {pos.qty} @ ${pos.entry_price:.2f} · "
                           f"costo ${pos.cost_total:,.2f} · order {pos.order_id}")
                st.session_state.pop("selected", None)
            except Exception as e:
                st.error(f"Error en la compra: {e}")

st.markdown("---")

# ===========================================================================
# Panel de POSICIONES (refresco en vivo sin bloquear)
# ===========================================================================
st.header("2 · Posiciones y monitoreo")
st.caption("El monitoreo de ROI y el auto take-profit corren en el **daemon** "
           "(`py -m daemon.runner`), no en esta UI. Acá solo controlás y visualizás.")


@st.fragment(run_every=2.0)
def _positions_panel():
    open_pos = store.open_positions()
    if not open_pos:
        st.info("Sin posiciones abiertas.")
        return
    for p in open_pos:
        roi = p.get("roi_pct") or 0.0
        pnl = p.get("pnl") or 0.0
        color = "#2e7d32" if pnl >= 0 else "#b71c1c"
        cc = st.columns([2, 1, 1, 1, 1, 1, 2])
        cc[0].markdown(f"**{p['underlying']}** · `{p['occ']}`")
        cc[1].metric("Qty", p["qty"])
        cc[2].metric("Entrada", f"${p['entry_price']:.2f}")
        cc[3].metric("Actual (bid)", f"${p.get('current_price') or 0:.2f}")
        cc[4].markdown(f"<div style='color:{color}'><b>ROI</b><br>{roi:+.1f}%</div>",
                       unsafe_allow_html=True)
        cc[5].markdown(f"<div style='color:{color}'><b>P&L</b><br>${pnl:+,.2f}</div>",
                       unsafe_allow_html=True)
        with cc[6]:
            armed = bool(p.get("tp_armed"))
            st.caption(f"🎯 objetivo {p['roi_target_pct']:.0f}% · "
                       + ("🟢 TP armado" if armed else "⚪ TP off"))
            b1, b2, b3 = st.columns(3)
            if not armed and b1.button("Activar TP", key=f"arm_{p['occ']}"):
                store.push_command("arm_tp", p["occ"]); st.rerun()
            if armed and b1.button("Desarmar", key=f"disarm_{p['occ']}"):
                store.push_command("disarm_tp", p["occ"]); st.rerun()
            if b2.button("Vender ya", key=f"sell_{p['occ']}"):
                store.push_command("manual_sell", p["occ"]); st.rerun()


_positions_panel()

st.markdown("---")
if st.button("🛑 KILL SWITCH — cerrar todo y desarmar", use_container_width=True):
    store.push_command("kill_switch")
    st.warning("Kill switch enviado al daemon.")

# ===========================================================================
# Historial + audit
# ===========================================================================
with st.expander("📜 Historial de posiciones cerradas"):
    closed = [p for p in store.all_positions() if p["status"] == "closed"]
    if closed:
        import pandas as pd
        st.dataframe(pd.DataFrame(closed)[
            ["underlying", "occ", "qty", "entry_price", "exit_price",
             "roi_final", "pnl_net", "entry_time", "exit_time"]
        ], use_container_width=True)
    else:
        st.caption("Sin posiciones cerradas todavía.")

with st.expander("🔍 Audit log (últimos 50 eventos)"):
    import pandas as pd
    audit = store.recent_audit(50)
    if audit:
        st.dataframe(pd.DataFrame(audit), use_container_width=True)

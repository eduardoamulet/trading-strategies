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
    st.caption("Editá los parámetros **por alerta** en la tabla (Inversión / Umbral ROI / Strike). "
               "Acción y Tipo vienen de la señal y no se editan.")
    _seed = _pd.DataFrame([{"Acción": a.get("symbol"), "Tipo": a.get("tipo"),
                            "Inversión $": 1000.0, "Umbral ROI %": 20.0, "Strike": "atm"}
                           for a in _alerts_ho])
    _edited = st.data_editor(
        _seed, key="la_params_editor", hide_index=True, use_container_width=True,
        disabled=["Acción", "Tipo"],
        column_config={
            "Inversión $": st.column_config.NumberColumn(min_value=1.0, step=100.0, format="$%.0f"),
            "Umbral ROI %": st.column_config.NumberColumn(min_value=1.0, step=5.0, format="%.0f%%"),
            "Strike": st.column_config.SelectboxColumn(options=["atm", "itm"], required=True),
        })
    _la_arm = st.checkbox("🎯 Auto-armar la venta automática (TP) al comprar", value=True, key="la_arm",
                          help="Arma el take-profit al comprar → el daemon vende SOLO al llegar al "
                               "Umbral de ROI de esa alerta. Si lo desmarcás, tenés que armar el TP "
                               "a mano en el panel de abajo.")
    st.caption("⚙ El monitoreo y la venta automática los hace el **daemon** (no esta UI): dejá "
               "corriendo `cd live_trader && py -m daemon.runner` en otra terminal.")

    def _mk_ae(i, a):
        from core.alert_entry import AlertEntry
        _row = _edited.iloc[i]
        return AlertEntry(alert_id=str(a.get("id")), underlying=str(a.get("symbol", "")).upper(),
                          side=str(a.get("tipo", "")).upper(), inversion=float(_row["Inversión $"]),
                          roi_target_pct=float(_row["Umbral ROI %"]), strategy=str(_row["Strike"]),
                          arm_tp=bool(_la_arm))

    _pc1, _pc2 = st.columns([2, 1])
    if _pc1.button(f"🔍 Previsualizar {len(_alerts_ho)} (sin comprar)", type="primary",
                   use_container_width=True):
        from core.alert_entry import preview_from_alert
        _pv = []
        for i, a in enumerate(_alerts_ho):
            p = preview_from_alert(broker, store, selector, risk, _mk_ae(i, a))
            _estado = ("✅ lista" if p.status == "ok"
                       else ("⚠ " + "; ".join(p.reasons)) if p.status == "blocked"
                       else ("✗ " + "; ".join(p.reasons)))
            _pv.append({"Acción": a.get("symbol"), "Tipo": a.get("tipo"),
                        "Contrato": p.occ or "—",
                        "Strike": (f"{p.strike:g}" if p.strike else "—"),
                        "Ask": (f"${p.ask:.2f}" if p.ask else "—"),
                        "Spread": (f"${p.spread:.2f}" if p.occ else "—"),
                        "OI": int(p.open_interest or 0), "Cant.": int(p.qty or 0),
                        "Costo": (f"${p.cost:,.0f}" if p.cost else "—"),
                        "Umbral": f"{float(_edited.iloc[i]['Umbral ROI %']):.0f}%", "Estado": _estado})
        st.session_state["live_alerts_preview"] = _pv
        st.session_state["live_alerts_n_ok"] = sum(1 for x in _pv if x["Estado"] == "✅ lista")
        st.rerun()
    if _pc2.button("Descartar", use_container_width=True):
        for _k in ("live_alerts_handoff", "live_alerts_preview", "live_alerts_n_ok"):
            st.session_state.pop(_k, None)
        st.rerun()

    _pv = st.session_state.get("live_alerts_preview")
    if _pv:
        st.markdown("##### Vista previa (no se compró nada todavía)")
        st.dataframe(_pd.DataFrame(_pv), hide_index=True, use_container_width=True)
        _n_ok = int(st.session_state.get("live_alerts_n_ok", 0))
        st.caption(f"{_n_ok} de {len(_pv)} lista(s). Si cambiás un parámetro, volvé a previsualizar. "
                   "Las ⚠/✗ se intentan igual al confirmar pero probablemente fallen.")
        if st.button("✅ Confirmar y comprar (paper)", type="primary",
                     disabled=_n_ok == 0, use_container_width=True):
            from core.alert_entry import EntryError, enter_from_alert
            _res = []
            for i, a in enumerate(_alerts_ho):
                try:
                    pos = enter_from_alert(broker, store, selector, risk, om, _mk_ae(i, a))
                    _res.append({"Acción": a.get("symbol"), "Tipo": a.get("tipo"),
                                 "Estado": f"✅ comprada · {pos.qty} @ ${pos.entry_price:.2f}"})
                except EntryError as ex:
                    _res.append({"Acción": a.get("symbol"), "Tipo": a.get("tipo"), "Estado": f"⚠ {ex}"})
                except Exception as ex:
                    _res.append({"Acción": a.get("symbol"), "Tipo": a.get("tipo"), "Estado": f"⚠ error: {ex}"})
            st.session_state["live_alerts_results"] = _res
            for _k in ("live_alerts_handoff", "live_alerts_preview", "live_alerts_n_ok"):
                st.session_state.pop(_k, None)
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
    # --- Estado del daemon (latido) ---
    _hb = store.get_meta("daemon_heartbeat")
    _age = None
    if _hb and _hb.get("ts"):
        try:
            _age = (datetime.utcnow() - datetime.fromisoformat(_hb["ts"])).total_seconds()
        except Exception:
            _age = None
    if _age is not None and _age < max(10.0, settings.POLL_INTERVAL_SEC * 4):
        st.success(f"🟢 Daemon activo · último latido hace {_age:.0f}s · monitoreo y auto-sell ON")
    else:
        _txt = f"último latido hace {_age:.0f}s" if _age is not None else "nunca latió"
        st.error(f"🔴 Daemon NO detectado ({_txt}) — el monitoreo y la venta automática NO corren. "
                 "Arrancalo en otra terminal: `cd live_trader && py -m daemon.runner`")

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
st.header("3 · Reporte de operaciones (paper)")
closed = [p for p in store.all_positions() if p["status"] == "closed"]
if not closed:
    st.caption("Sin operaciones cerradas todavía. Cuando el daemon venda una posición "
               "(o la cierres a mano), el reporte aparece acá.")
else:
    import pandas as pd

    def _f(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    _n = len(closed)
    _pnl_tot = sum(_f(p.get("pnl_net")) for p in closed)
    _wins = sum(1 for p in closed if _f(p.get("pnl_net")) > 0)
    _avg_roi = sum(_f(p.get("roi_final")) for p in closed) / _n
    _mc = st.columns(4)
    _mc[0].metric("Operaciones", _n)
    _mc[1].metric("P&L total", f"${_pnl_tot:+,.2f}")
    _mc[2].metric("Ganadoras", f"{_wins}/{_n}")
    _mc[3].metric("ROI promedio", f"{_avg_roi:+.1f}%")

    _rep = pd.DataFrame([{
        "Cerrada": str(p.get("exit_time") or "")[:19].replace("T", " "),
        "Contrato": f"{p['underlying']} · {p['occ']}",
        "Cant.": int(_f(p.get("qty"))),
        "Compra": f"${_f(p.get('entry_price')):.2f}",
        "Venta": f"${_f(p.get('exit_price')):.2f}",
        "ROI": f"{_f(p.get('roi_final')):+.1f}%",
        "P&L": f"${_f(p.get('pnl_net')):+,.2f}",
        "Objetivo": f"{_f(p.get('roi_target_pct')):.0f}%",
    } for p in sorted(closed, key=lambda p: str(p.get("exit_time") or ""), reverse=True)])
    st.dataframe(_rep, hide_index=True, use_container_width=True)
    st.caption("Entorno **SANDBOX** (paper). Compra/Venta = precio de ejecución por contrato · "
               "P&L = ganancia/pérdida total de la operación · Objetivo = Umbral de ROI configurado.")

with st.expander("🔍 Audit log (últimos 50 eventos)"):
    import pandas as pd
    audit = store.recent_audit(50)
    if audit:
        st.dataframe(pd.DataFrame(audit), use_container_width=True)

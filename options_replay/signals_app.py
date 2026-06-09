"""Página 'Alertas' — Historial de Señales con filas EXPANDIBLES.

Cada fila se expande para mostrar los Criterios de la estrategia (✓/✗) + la Gráfica
de la señal, y permite editar Estado / Ganancia. Importación: pegar JSON · subir .eml
· poller IMAP.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import external_signals as xs  # noqa: E402
import signals_db as db        # noqa: E402

# ── Backtest de señales (cada señal = 1 iteración) ───────────────────────────
import time as _bt_time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402
from datetime import time as _time_cls  # noqa: E402

from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402
from engine import NoMatchError, run_next_iteration, validate_0dte_session  # noqa: E402

_BT_DATA_DIR = HERE / "data"
_BT_TICKER_INFO = (json.loads((HERE / "ticker_info.json").read_text(encoding="utf-8"))
                   if (HERE / "ticker_info.json").exists() else {})
_BT_REASON = {
    "100%_threshold": "Umbral de profit",
    "stop_loss": "Stop loss",
    "session_end": "Cierre (sin trigger)",
    "overnight_1dte": "Overnight 1DTE",
}


def _bt_api_key() -> str:
    try:
        import config  # raíz del proyecto (gitignored)
        return getattr(config, "POLYGON_API_KEY", "")
    except Exception:
        return ""


def _bt_to_ts(date_iso: str, t):
    return pd.Timestamp(f"{date_iso} {t.hour:02d}:{t.minute:02d}", tz="America/New_York")


def _bt_premium_range(ticker: str):
    """Rango de prima (óptimo) por ticker desde ticker_info.json (÷100). Fallback 0.30–0.50."""
    info = _BT_TICKER_INFO.get((ticker or "").upper(), {}) or {}
    lo = (info.get("min") / 100.0) if info.get("min") is not None else 0.30
    hi = (info.get("max") / 100.0) if info.get("max") is not None else 0.50
    if hi <= lo:
        hi = lo + 0.05
    return float(lo), float(hi)


def _bt_run_signal(dl, sig: dict, inversion: float, spread_cfg):
    """Corre 1 iteración para UNA señal (NO usa st.*; seguro en hilos). Mapea:
    symbol→ticker, fecha→fecha, hora→entrada, Tipo CALL→Sólo CALL / PUT→Sólo PUT.
    Inversión 100% a esa pierna, umbral 1000%, stop −100%, Opción 1 (spread), mismo día."""
    symbol = str(sig.get("symbol") or "").upper().strip()
    fecha = str(sig.get("fecha") or "").strip()
    hora_s = str(sig.get("hora") or "").strip()
    tipo = str(sig.get("tipo") or "").upper().strip()
    base = {"symbol": symbol, "fecha": fecha, "hora": hora_s, "tipo": tipo}
    if tipo not in ("CALL", "PUT") or not symbol or not fecha:
        return {**base, "status": "error", "iteration": None,
                "error": "señal incompleta (symbol/fecha/Tipo)"}
    try:
        hh, mm = hora_s.split(":")[:2]
        entry = _time_cls(int(hh), int(mm))
    except Exception:
        return {**base, "status": "error", "iteration": None, "error": f"hora inválida '{hora_s}'"}
    mode = "call_only" if tipo == "CALL" else "put_only"
    inv_call = float(inversion) if mode == "call_only" else 0.0
    inv_put = float(inversion) if mode == "put_only" else 0.0
    p_lo, p_hi = _bt_premium_range(symbol)
    order_ts = _bt_to_ts(fecha, entry)
    day_end_ts = _bt_to_ts(fecha, _time_cls(16, 0)) - pd.Timedelta(minutes=1)
    try:
        validate_0dte_session(dl, symbol, fecha)
        it = run_next_iteration(
            dl, symbol, fecha, p_lo, p_hi, inv_call, inv_put, order_ts, day_end_ts,
            exit_threshold_pct=10.0,    # Umbral de ROI 1000%
            exit_metric="total", stop_loss_pct=-1.0,   # Stop loss −100%
            iteration_idx=1, mode=mode, ext_min=p_lo, ext_max=p_hi,
            selection_criterion="spread", dte=0, spread_cfg=spread_cfg,
        )
        return {**base, "status": "ok", "iteration": it, "error": None}
    except NoMatchError as e:
        return {**base, "status": "error", "iteration": None,
                "error": f"Sin contrato (rango/spread): {e}"}
    except Exception as e:
        return {**base, "status": "error", "iteration": None, "error": str(e)}


def _bt_render_results(results: list, elapsed: float, partial: bool = False):
    """Tabla de resultados (ordenada por fecha/hora) + totales. results: lista de dicts."""
    rows, tot_gain, n_ok, n_win = [], 0.0, 0, 0
    for r in sorted(results, key=lambda x: (x.get("fecha") or "", x.get("hora") or "",
                                            x.get("symbol") or "")):
        it = r.get("iteration")
        if it is not None:
            roi = (it.gain_total / it.invest_total) if it.invest_total else 0.0
            tot_gain += it.gain_total
            n_ok += 1
            n_win += 1 if it.gain_total > 0 else 0
            _call = r.get("tipo") == "CALL"
            rows.append({
                "Acción": r["symbol"], "Fecha": r["fecha"], "Hora": r["hora"], "Tipo": r["tipo"],
                "Strike": (it.call_strike if _call else it.put_strike),
                "Prima ent.": (it.call_entry_premium if _call else it.put_entry_premium),
                "Prima sal.": (it.call_exit_premium if _call else it.put_exit_premium),
                "Ganancia": it.gain_total, "ROI %": roi * 100.0,
                "Razón": _BT_REASON.get(it.exit_reason, it.exit_reason),
            })
        else:
            rows.append({
                "Acción": r.get("symbol", "?"), "Fecha": r.get("fecha", ""),
                "Hora": r.get("hora", ""), "Tipo": r.get("tipo", ""),
                "Strike": None, "Prima ent.": None, "Prima sal.": None,
                "Ganancia": None, "ROI %": None, "Razón": f"⚠ {r.get('error', 'error')}",
            })
    if not partial:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Señales backtesteadas", len(results))
        m2.metric("Con resultado", n_ok)
        m3.metric("💲 Ganancia total", f"${tot_gain:,.0f}")
        m4.metric("Ganadoras", f"{n_win}/{n_ok}" if n_ok else "0/0")
        st.caption(f"⏱️ Completado en {elapsed:0.1f}s")
    st.dataframe(
        pd.DataFrame(rows), use_container_width=True, hide_index=True,
        column_config={
            "Strike": st.column_config.NumberColumn("Strike", format="%.0f"),
            "Prima ent.": st.column_config.NumberColumn("Prima ent.", format="$%.2f"),
            "Prima sal.": st.column_config.NumberColumn("Prima sal.", format="$%.2f"),
            "Ganancia": st.column_config.NumberColumn("Ganancia", format="$%.0f"),
            "ROI %": st.column_config.NumberColumn("ROI %", format="%.0f%%"),
        },
    )


try:
    st.set_page_config(page_title="Alertas", layout="wide")
except Exception:
    pass

st.markdown("<style>.block-container{padding-top:2rem !important;}</style>",
            unsafe_allow_html=True)
st.title("📡 Historial de Señales")
st.caption("Alertas de investepacademyia (Trend Reversal) — importadas a tu app")

_now = lambda: datetime.now().isoformat(timespec="seconds")

# ── Importar ─────────────────────────────────────────────────────────────────
with st.expander("📥 Importar señales", expanded=False):
    t_json, t_eml, t_mail = st.tabs(["Pegar JSON", "Subir email (.eml)", "Revisar correo (auto)"])
    with t_json:
        _txt = st.text_area("JSON del Historial", height=140, label_visibility="collapsed",
                            placeholder='{ "items": [ { "symbol": "AAPL", ... } ] }')
        if st.button("Importar JSON"):
            try:
                n = xs.import_payload(_txt, now_iso=_now())
                st.success(f"Importadas {n} señales nuevas.") if n else st.info("Sin señales nuevas.")
            except Exception as e:
                st.error(f"No se pudo parsear el JSON: {e}")
    with t_eml:
        _files = st.file_uploader("Emails (.eml)", type=["eml"], accept_multiple_files=True,
                                  label_visibility="collapsed")
        if _files and st.button("Importar email(s)"):
            tot = sum(xs.import_email(f.read(), now_iso=_now()) for f in _files)
            st.success(f"Importadas {tot} señales nuevas de {len(_files)} archivo(s).")
    with t_mail:
        st.caption("Lee tu Gmail por IMAP (requiere signals_secrets.py con un App Password).")
        if st.button("📧 Revisar correo ahora"):
            try:
                st.success(f"Importadas {xs.fetch_from_email(now_iso=_now())} señales nuevas.")
            except xs.ScraperNotConfigured as e:
                st.warning(str(e))
            except Exception as e:
                st.error(f"Error IMAP: {e}")

df = xs.load_signals()
if df.empty:
    st.info("Todavía no hay señales. Importá pegando el JSON, subiendo un .eml, o por correo.")
    st.stop()

# ── Filtros ──────────────────────────────────────────────────────────────────
f1, f2, f3, f4 = st.columns(4)
sel_estr = f1.selectbox("Estrategia", ["(todas)"] + sorted(df["estrategia"].dropna().unique().tolist()))
sel_sym = f2.selectbox("Acción", ["(todas)"] + sorted(df["symbol"].dropna().unique().tolist()))
sel_est = f3.selectbox("Estado", ["(todos)"] + xs.ESTADOS)
sel_tipo = f4.selectbox("Tipo", ["(todos)", "CALL", "PUT"])
_fechas = pd.to_datetime(df["fecha"], errors="coerce").dropna()
g1, g2, g3 = st.columns([1, 1, 2])
d_desde = g1.date_input("Desde", value=(_fechas.min().date() if len(_fechas) else datetime.now().date()))
d_hasta = g2.date_input("Hasta", value=(_fechas.max().date() if len(_fechas) else datetime.now().date()))
_page = g3.selectbox("Filas por página", [10, 25, 50, 100, "Todas"], index=4)

fdf = df.copy()
if sel_estr != "(todas)": fdf = fdf[fdf["estrategia"] == sel_estr]
if sel_sym != "(todas)": fdf = fdf[fdf["symbol"] == sel_sym]
if sel_est != "(todos)": fdf = fdf[fdf["estado"] == sel_est]
if sel_tipo != "(todos)": fdf = fdf[fdf["tipo"] == sel_tipo]
fdf = fdf[(fdf["fecha"] >= d_desde.isoformat()) & (fdf["fecha"] <= d_hasta.isoformat())]
if _page != "Todas": fdf = fdf.head(int(_page))

# ── Métricas ─────────────────────────────────────────────────────────────────
m1, m2, m3, m4 = st.columns(4)
m1.metric("Señales en lista", len(fdf))
m2.metric("💲 Dinero ganado con señales en lista", f"${fdf['ganancia'].sum():,.2f}")
m3.metric("CALL / PUT", f"{int((fdf['tipo'] == 'CALL').sum())} / {int((fdf['tipo'] == 'PUT').sum())}")
m4.metric("Aprovechadas", int((fdf["estado"] == "Aprovechada").sum()))

st.divider()


def _render_detalle(r):
    d1, d2 = st.columns([1, 1.3])
    with d1:
        st.markdown("**Criterios de la estrategia:**")
        try:
            crits = json.loads(r["criterios_json"]) if r.get("criterios_json") else []
        except Exception:
            crits = []
        if crits:
            for c in crits:
                st.markdown(("✅ " if c.get("ok") else "❌ ") + str(c.get("nombre", "")))
        else:
            st.caption(f"Sin detalle de criterios ({r.get('criterios') or '—'}).")
    with d2:
        st.markdown("**Gráfica de la señal:**")
        if r.get("chart_url"):
            st.image(r["chart_url"], use_container_width=True)
        else:
            st.caption("Sin gráfica.")
    e1, e2, e3 = st.columns([1, 1, 1])
    _i = xs.ESTADOS.index(r["estado"]) if r["estado"] in xs.ESTADOS else 0
    ne = e1.selectbox("Estado", xs.ESTADOS, index=_i, key=f"est_{r['id']}")
    ng = e2.number_input("Ganancia ($)", value=float(r["ganancia"] or 0), step=10.0,
                         key=f"gan_{r['id']}")
    e3.markdown("<br>", unsafe_allow_html=True)
    if e3.button("💾 Guardar", key=f"save_{r['id']}", use_container_width=True):
        db.update_user_fields(r["id"], estado=ne, ganancia=float(ng))
        st.toast("Guardado"); st.rerun()


# Tabla ORDENABLE (clic en encabezados, redimensionar, buscar) — misma flexibilidad
# que el backtesting. El detalle (criterios + gráfica + editar) se abre al SELECCIONAR
# una fila.
fdf = fdf.reset_index(drop=True)
_show = pd.DataFrame({
    "Acción": fdf["symbol"].values,
    "Hora": fdf["hora"].values,
    "Fecha": fdf["fecha"].values,
    "Estrategia": fdf["estrategia"].values,
    "% Cumpl.": pd.to_numeric(fdf["probabilidad"], errors="coerce").values,
    "Tipo": fdf["tipo"].values,
    "Criterios": fdf["criterios"].values,
    "Estado": fdf["estado"].values,
    "Ganancia": pd.to_numeric(fdf["ganancia"], errors="coerce").fillna(0.0).values,
    "Gráfica": fdf["chart_url"].values,
})
_sel = st.dataframe(
    _show, use_container_width=True, hide_index=True,
    on_select="rerun", selection_mode="multi-row",
    column_config={
        "% Cumpl.": st.column_config.NumberColumn("% Cumpl.", format="%.0f%%"),
        "Ganancia": st.column_config.NumberColumn("Ganancia", format="$%.0f"),
        "Gráfica": st.column_config.LinkColumn("Gráfica", display_text="📈 Ver"),
    },
)
st.caption("Clic en los **encabezados** para ordenar · marcá las **casillas** de la "
           "izquierda para seleccionar señales (1 fila → ver detalle · varias → backtest).")
_rows = _sel.selection.rows if (_sel and getattr(_sel, "selection", None)) else []

# Detalle (criterios + gráfica + editar) solo cuando hay EXACTAMENTE 1 seleccionada.
if len(_rows) == 1:
    st.divider()
    _render_detalle(fdf.iloc[_rows[0]])

# ── Backtest de señales seleccionadas ────────────────────────────────────────
st.divider()
st.subheader("🔬 Backtest de señales seleccionadas")
st.caption(
    "Cada señal seleccionada = 1 iteración: **ticker** = Acción · **fecha/hora** = "
    "Fecha/Hora · **Sólo CALL/PUT** según Tipo · Inversión 100% a esa pierna · Umbral "
    "1000% · Stop −100% · **Opción 1 (menor spread)** · mismo día (sale 16:00)."
)
if not _rows:
    st.info("Marcá una o más filas (casilla a la izquierda de la tabla) para backtestearlas.")
else:
    _bc1, _bc2, _bc3 = st.columns([1, 1, 1.4])
    _bt_inv = float(_bc1.number_input("Inversión ($)", min_value=1.0, value=1000.0,
                                      step=100.0, key="bt_inv"))
    _bt_spmax = float(_bc2.number_input(
        "Spread máx ($) — 0 = auto", min_value=0.0, value=0.0, step=0.01, format="%.2f",
        key="bt_spmax",
        help="0 = filtro de spread por precio (config). Un valor (p. ej. 0.08) lo afloja "
             "si varias señales no encuentran contrato por la compuerta de spread."))
    _bc3.markdown("<br>", unsafe_allow_html=True)
    if _bc3.button(f"▶ Backtestear {len(_rows)} señal(es)", type="primary",
                   use_container_width=True, key="bt_go"):
        _scfg = ({"enable_spread_filter": True, "_max_spread_override": _bt_spmax}
                 if _bt_spmax > 0 else None)
        _sigs = [fdf.iloc[i].to_dict() for i in _rows]
        _dl = Downloader(PolygonAdapter(_bt_api_key()), _BT_DATA_DIR)
        _n = len(_sigs)
        _workers = max(1, min(8, _n))
        _prog = st.progress(0.0, text="Corriendo backtest de señales…")
        _live = st.empty()
        _t0 = _bt_time.perf_counter()
        _res = []
        with ThreadPoolExecutor(max_workers=_workers) as _ex:
            _futs = [_ex.submit(_bt_run_signal, _dl, s, _bt_inv, _scfg) for s in _sigs]
            _done = 0
            for _f in as_completed(_futs):
                try:
                    _res.append(_f.result())
                except Exception as _e:
                    _res.append({"symbol": "?", "status": "error", "iteration": None,
                                 "error": str(_e)})
                _done += 1
                _el = _bt_time.perf_counter() - _t0
                _prog.progress(_done / _n, text=(f"⏱️ {_el:0.1f}s · {_done}/{_n} señales "
                                                 f"({_workers} en paralelo)"))
                with _live.container():
                    _bt_render_results(_res, _el, partial=True)
        _prog.empty()
        _live.empty()
        st.session_state["signals_bt"] = {"results": _res,
                                          "elapsed": _bt_time.perf_counter() - _t0}
        st.rerun()

# Resultados persistidos (sobreviven a re-selecciones hasta que se limpian).
_btres = st.session_state.get("signals_bt")
if _btres:
    st.markdown("#### Resultados del último backtest")
    if st.button("🧹 Limpiar resultados"):
        st.session_state.pop("signals_bt", None)
        st.rerun()
    _bt_render_results(_btres["results"], _btres.get("elapsed", 0.0), partial=False)

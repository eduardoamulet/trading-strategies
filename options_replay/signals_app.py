"""Página 'Alertas' — Historial de Señales con filas EXPANDIBLES.

Cada fila se expande para mostrar los Criterios de la estrategia (✓/✗) + la Gráfica
de la señal, y permite editar Estado / Ganancia. Importación: subir .eml · poller IMAP.
Selección MULTI-fila → "Backtestear señales" redirige a la página
Backtesting con esas señales cargadas como iteraciones.
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

try:
    st.set_page_config(page_title="Alertas", layout="wide")
except Exception:
    pass

st.title("📡 Historial de Señales")
st.caption("Alertas de investepacademyia (Trend Reversal) — importadas a tu app")

_now = lambda: datetime.now().isoformat(timespec="seconds")

_CHAIN_DIR = HERE / "data" / "chain"


@st.cache_data(ttl=300, show_spinner=False)
def _es_0dte(ticker: str, fecha: str) -> bool:
    """¿La señal cayó en un día con 0DTE para ese ticker? El downloader solo cachea chains
    NO vacías → si existe data/chain/{ticker}_{fecha}.parquet, ese día tuvo 0DTE."""
    t, f = str(ticker or "").strip(), str(fecha or "").strip()
    return bool(t and f and (_CHAIN_DIR / f"{t}_{f}.parquet").exists())


@st.cache_data(ttl=600, show_spinner=False)
def _ticker_universe() -> list[str]:
    """Universo completo de activos = las MISMAS claves que la página Activos
    (ticker_info.json). Se usa para poblar el dropdown 'Acción' aunque un ticker
    todavía no tenga señales."""
    try:
        _ti = json.loads((HERE / "ticker_info.json").read_text(encoding="utf-8"))
        return sorted(_ti.keys())
    except Exception:  # noqa: BLE001
        return []


@st.cache_resource(show_spinner=False)
def _polygon():
    """PolygonAdapter cacheado (1 instancia). None si no se pudo crear (sin config/clave)."""
    try:
        import config
        from adapter_polygon import PolygonAdapter
        return PolygonAdapter(config.POLYGON_API_KEY)
    except Exception:
        return None


@st.cache_data(ttl=86400, show_spinner=False)
def _es_optionable(ticker: str):
    """¿El ticker es optionable HOY? (tiene contratos vigentes en Polygon). True/False, o
    None si no se pudo consultar. Cacheado 24h (la optionability casi no cambia)."""
    t = str(ticker or "").strip().upper()
    _ad = _polygon()
    if not t or _ad is None:
        return None
    try:
        return _ad.is_optionable(t)
    except Exception:
        return None


def _opt_icon(v) -> str:
    return "✅" if v is True else ("❌" if v is False else "—")


# ── Importar ─────────────────────────────────────────────────────────────────
with st.expander("📥 Importar señales", expanded=False):
    t_eml, t_mail, t_api = st.tabs(["Subir email (.eml)", "Revisar correo (auto)", "📡 API directa"])
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
    with t_api:
        st.caption("Baja directo de la API de Investep (signals/history). En `signals_secrets.py` poné "
                   "`INVESTEP_USER` + `INVESTEP_PASSWORD` (recomendado: se loguea solo y refresca el "
                   "token), o un `INVESTEP_TOKEN` temporal (Bearer, vence ~1h).")
        _api_days = st.number_input("Días hacia atrás", min_value=1, max_value=90, value=7,
                                    step=1, key="api_days_back")
        if st.button("📡 Bajar de la API ahora"):
            try:
                _n = xs.fetch_from_api(days_back=int(_api_days), now_iso=_now())
                st.success(f"Importadas {_n} señales nuevas de la API.")
            except xs.ScraperNotConfigured as e:
                st.warning(str(e))
            except Exception as e:
                st.error(f"Error API: {e}")

    st.divider()
    if st.button(f"🧹 Limpiar duplicados existentes ({db.count()} señales)",
                 help="Borra señales repetidas por identidad (Acción·Estrategia·Fecha·Hora·Tipo) "
                      "que hayan quedado de antes, conservando 1 por grupo (prioriza las que "
                      "tengan estado/ganancia editados). Un CALL y un PUT iguales en lo demás NO "
                      "son duplicados."):
        _rm = db.dedupe_existing()
        if _rm:
            st.success(f"🧹 Eliminadas {_rm} duplicada(s). Quedan {db.count()}.")
            st.rerun()
        else:
            st.info("No había duplicados. 👍")

df = xs.load_signals()
if df.empty:
    st.info("Todavía no hay señales. Importá subiendo un .eml, o por correo (IMAP).")
    st.stop()

# ── Filtros ──────────────────────────────────────────────────────────────────
f1, f2, f3, f4 = st.columns(4)
sel_estr = f1.selectbox("Estrategia", ["(todas)"] + sorted(df["estrategia"].dropna().unique().tolist()))
# Opciones del dropdown "Acción" = universo completo de activos (página Activos) ∪ los
# símbolos que ya tienen señales (por si alguno no está en ticker_info). Así META/NVDA/
# GOOG aparecen aunque todavía no tengan señales importadas.
_sym_opts = sorted(set(_ticker_universe()) | set(df["symbol"].dropna().astype(str).tolist()))
# Preselección por defecto de "Acción" según el DÍA DE LA SEMANA (config §4 «Tickers que vencen ese
# mismo día», centralizada en ticker_prefs): si la tabla está filtrada a UNA fecha (filtro de tabla ≠
# «(todas las fechas)») o Desde==Hasta, se prepopula con los tickers que vencen 0DTE ese día; si no,
# la lista por defecto. Las fechas se leen del session_state (esos widgets se crean más abajo).
import ticker_prefs as _tp_pref
_TODAS = "(todas las fechas)"                       # (también lo usa la navegación por fecha, abajo)
_acc_target = None
_dp_prev = st.session_state.get("sig_date_pick")
if _dp_prev and _dp_prev != _TODAS:                 # condición 2: filtro de tabla en una fecha puntual
    try:
        _acc_target = pd.to_datetime(_dp_prev).date()
    except Exception:
        _acc_target = None
if _acc_target is None:                             # condición 1: Desde == Hasta
    _de, _ha = st.session_state.get("sig_desde"), st.session_state.get("sig_hasta")
    if _de is not None and _de == _ha:
        _acc_target = _de
try:
    _DEFAULT_SYMS = (_tp_pref.tickers_for_weekday(_acc_target.weekday())
                     if _acc_target is not None else _tp_pref.default_tickers())
except Exception:
    _DEFAULT_SYMS = ["QQQ", "SPY", "IWM", "NVDA", "TSLA", "PLTR", "AMZN", "META", "MSFT", "GOOG", "AAPL"]
# Re-siembra SOLO cuando la lista por defecto cambia (cambió el día/fecha) → no pisa tu filtro manual.
_valid_pref = [s for s in _DEFAULT_SYMS if s in _sym_opts]
if st.session_state.get("_pref_sig_acc") != tuple(_valid_pref):
    st.session_state["_pref_sig_acc"] = tuple(_valid_pref)
    st.session_state["sig_accion_ms"] = _valid_pref
sel_sym = f2.multiselect("Acción", _sym_opts, key="sig_accion_ms", placeholder="(todas)")
sel_est = f3.selectbox("Estado", ["(todos)"] + xs.ESTADOS)
sel_tipo = f4.selectbox("Tipo", ["(todos)", "CALL", "PUT"])
_fechas = pd.to_datetime(df["fecha"], errors="coerce").dropna()
g1, g2, g3, g4 = st.columns(4)
d_desde = g1.date_input("Desde", value=(_fechas.min().date() if len(_fechas) else datetime.now().date()),
                        key="sig_desde")
d_hasta = g2.date_input("Hasta", value=(_fechas.max().date() if len(_fechas) else datetime.now().date()),
                        key="sig_hasta")
pmin = g3.slider("% Cumplimiento mínimo", 0, 100, 90, step=5,
                 help="Muestra solo señales con % de cumplimiento ≥ este valor (0 = todas).")
g4.markdown("<div style='height:1.6rem'></div>", unsafe_allow_html=True)  # alinea con los date_input
_solo_0dte = g4.checkbox("Sólo 0 DTE", value=True, key="sig_solo_0dte",
                         help="Muestra solo señales cuyo ticker tenía opción 0DTE ese día "
                              "(según la cache de cadenas en data/chain/).")
_solo_opt = g4.checkbox("Sólo optionable", value=False, key="sig_solo_opt",
                        help="Muestra solo señales cuyo ticker tiene opciones vigentes hoy "
                             "(consulta Polygon, cacheada 24h).")

fdf = df.copy()
if sel_estr != "(todas)": fdf = fdf[fdf["estrategia"] == sel_estr]
if sel_sym: fdf = fdf[fdf["symbol"].isin(sel_sym)]
if sel_est != "(todos)": fdf = fdf[fdf["estado"] == sel_est]
if sel_tipo != "(todos)": fdf = fdf[fdf["tipo"] == sel_tipo]
if pmin > 0: fdf = fdf[pd.to_numeric(fdf["probabilidad"], errors="coerce") >= pmin]
fdf = fdf[(fdf["fecha"] >= d_desde.isoformat()) & (fdf["fecha"] <= d_hasta.isoformat())]
if _solo_0dte and not fdf.empty:
    fdf = fdf[[_es_0dte(s, f) for s, f in zip(fdf["symbol"].astype(str), fdf["fecha"].astype(str))]]
if _solo_opt and not fdf.empty:
    fdf = fdf[[_es_optionable(s) is True for s in fdf["symbol"].astype(str)]]

# ── Métricas ─────────────────────────────────────────────────────────────────
m1, m2, m3, m4 = st.columns(4)
m1.metric("Señales en lista", len(fdf))
m2.metric("💲 Dinero ganado con señales en lista", f"${fdf['ganancia'].sum():,.2f}")
m3.metric("CALL / PUT", f"{int((fdf['tipo'] == 'CALL').sum())} / {int((fdf['tipo'] == 'PUT').sum())}")
m4.metric("Aprovechadas", int((fdf["estado"] == "Aprovechada").sum()))

# Divisor pegado a las métricas: el st.divider por defecto deja mucho margen arriba.
st.markdown("<hr style='margin:-0.6rem 0 0.6rem 0'>", unsafe_allow_html=True)


@st.dialog("📊 Detalle de la señal", width="large")
def _render_detalle(r):
    # Modal compacto. Toda la INFO va en la columna izquierda (debajo del título); la
    # gráfica (derecha, 2/3) sube hasta el tope del modal (junto al título) y baja hasta
    # abajo. object-fit:contain → no deforma. Scope: solo dentro del modal.
    st.markdown(
        "<style>"
        "div[role='dialog']{max-width:1040px !important;}"   # modal más chico/ajustado
        # margin-top negativo grande → sube la imagen hacia el título; height:auto → la
        # imagen toma su alto natural (sin barra/espacio en blanco debajo).
        "div[role='dialog'] [data-testid='stImage']{width:100% !important; margin-top:-5.5rem !important;}"
        "div[role='dialog'] [data-testid='stImage'] img{"
        "height:auto !important; width:100% !important; object-fit:contain;}"
        "</style>",
        unsafe_allow_html=True,
    )
    # Info y controles a la IZQUIERDA (1/3) · Gráfica a la DERECHA (2/3), pegada de arriba
    # (al título) a abajo del modal (alto en el CSS de arriba).
    _dc1, _dc2 = st.columns([1, 2])
    with _dc1:
        st.markdown(
            f"**{r.get('symbol', '')} · {r.get('tipo', '')} · "
            f"{r.get('fecha', '')} {r.get('hora', '')}**"
        )
        st.markdown(f"**Estrategia:** — {r.get('estrategia', '')}")
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
        st.divider()
        # Estado y Ganancia en la MISMA fila.
        _e1, _e2 = st.columns(2)
        _i = xs.ESTADOS.index(r["estado"]) if r["estado"] in xs.ESTADOS else 0
        ne = _e1.selectbox("Estado", xs.ESTADOS, index=_i, key=f"est_{r['id']}")
        ng = _e2.number_input("Ganancia ($)", value=float(r["ganancia"] or 0), step=10.0,
                              key=f"gan_{r['id']}")
        # Borrar + Confirmar (arriba) · Guardar (debajo).
        _d1, _d2 = st.columns([1, 1], vertical_alignment="center")
        _del_ok = _d2.checkbox("Confirmar", key=f"delok_{r['id']}",
                               help="Borrado irreversible de esta señal.")
        if _d1.button("🗑 Borrar", key=f"del_{r['id']}", disabled=not _del_ok,
                      use_container_width=True):
            db.delete_signals([str(r["id"])])
            st.session_state.pop("bt_selected_ids", None)
            st.session_state["_sig_ed_v"] = st.session_state.get("_sig_ed_v", 0) + 1
            st.toast("🗑 Señal borrada")
            st.rerun()
        if st.button("💾 Guardar", key=f"save_{r['id']}", use_container_width=True,
                     type="primary"):
            db.update_user_fields(r["id"], estado=ne, ganancia=float(ng))
            st.toast("Guardado")
            st.rerun()   # cierra el modal y refresca
    with _dc2:
        if r.get("chart_url"):
            st.image(r["chart_url"], use_container_width=True)
        else:
            st.caption("Sin gráfica.")


@st.dialog("🗑 Eliminar señales")
def _confirm_delete(ids):
    """Confirmación antes de borrar (irreversible). `ids` = ids a eliminar."""
    st.warning(f"¿Eliminar {len(ids)} señal(es)? Esta acción **no se puede deshacer**.")
    _cc, _ce = st.columns(2)
    if _cc.button("Cancelar", use_container_width=True):
        st.rerun()   # cierra el modal sin borrar
    if _ce.button("🗑 Sí, eliminar", type="primary", use_container_width=True):
        _n = db.delete_signals(list(ids))
        st.session_state.pop("bt_selected_ids", None)
        st.session_state["_sig_ed_v"] = st.session_state.get("_sig_ed_v", 0) + 1
        st.toast(f"🗑 Borradas {_n} señal(es)")
        st.rerun()


# Tabla ordenable con SELECCIÓN de filas NATIVA (multi-row): click + shift+click marca un
# rango. Ver/Eliminar aparecen ARRIBA (junto al orden); Backtestear/Operar, debajo.
fdf = fdf.reset_index(drop=True)
if fdf.empty:
    st.info("No hay señales que cumplan los filtros seleccionados. "
            "Ajustá los filtros (por ej., bajá el **% Cumpl. mínimo**).")
    st.stop()
# Acciones ARRIBA (Ver / Eliminar). Este contenedor se llena DESPUÉS de la tabla, cuando
# ya sabemos qué filas están seleccionadas.
_acts_ph = st.container()
# Orden FIJO: por fecha, las más recientes primero (se quitó el selector "Ordenar por").
fdf = fdf.sort_values(["fecha", "hora"], ascending=False).reset_index(drop=True)

# ── Navegación por FECHA ──────────────────────────────────────────────────────
# El historial se mueve POR FECHA: se elige un día (dropdown o ◀/▶) y la tabla muestra
# TODAS las alertas de ese día. 'view' = alertas del día elegido; la selección y los
# botones (Ver/Eliminar/Backtest) operan sobre 'view'.
_dates = sorted(fdf["fecha"].astype(str).unique().tolist(), reverse=True)   # recientes primero
_ndays = len(_dates)
_opts = [_TODAS] + _dates                           # _TODAS ya definido arriba (filtros de "Acción")
st.session_state["_sig_dates"] = _dates   # para los callbacks de ◀/▶
# Selección del dropdown; default = "(todas las fechas)" → la tabla muestra TODAS las
# alertas (de todos los días). Si quedó inválida (cambió un filtro), se resetea a ese valor.
if st.session_state.get("sig_date_pick") not in _opts:
    st.session_state["sig_date_pick"] = _TODAS
_cur = st.session_state["sig_date_pick"]
_is_todas = (_cur == _TODAS)
_didx = _dates.index(_cur) if not _is_todas else -1
if st.session_state.get("_sig_date_prev") != _cur:   # cambió fecha/modo → limpiar selección
    st.session_state["_sig_date_prev"] = _cur
    st.session_state["_sig_ed_v"] = st.session_state.get("_sig_ed_v", 0) + 1
view = (fdf if _is_todas else fdf[fdf["fecha"].astype(str) == _cur]).reset_index(drop=True)


def _step_date(_delta):   # ◀ = día más antiguo (+1) · ▶ = día más reciente (-1)
    _ds = st.session_state.get("_sig_dates", [])
    _p = st.session_state.get("sig_date_pick")
    if _p in _ds and 0 <= _ds.index(_p) + _delta < len(_ds):
        st.session_state["sig_date_pick"] = _ds[_ds.index(_p) + _delta]


_dn1, _dn2, _dn3, _dn4 = st.columns([0.5, 2.5, 0.5, 4], vertical_alignment="center")
_dn1.button("◀", disabled=(_is_todas or _didx >= _ndays - 1), use_container_width=True,
            key="sig_date_older", help="Día anterior (más antiguo)", on_click=_step_date, args=(1,))
_dn2.selectbox("Fecha", _opts, key="sig_date_pick", label_visibility="collapsed")
_dn3.button("▶", disabled=(_is_todas or _didx <= 0), use_container_width=True,
            key="sig_date_newer", help="Día siguiente (más reciente)", on_click=_step_date, args=(-1,))
if _is_todas:
    _dn4.markdown(f"**{len(view)}** alerta(s) · **todas** las fechas ({_ndays} día(s))")
else:
    _dn4.markdown(f"**{len(view)}** alerta(s) el **{_cur}**  ·  día {_didx + 1} de {_ndays}")

# Tabla SOLO LECTURA con selección multi-fila NATIVA: NO lleva casillas; la selección
# (shift+click = rango) la maneja Streamlit. Sólo columnas de datos + bandas por fecha.
_show = pd.DataFrame({
    "Acción": view["symbol"].values,
    "Hora": view["hora"].values,
    "Fecha": view["fecha"].values,
    "Estrategia": view["estrategia"].values,
    "Criterios": view["criterios"].values,
    "% Cumpl.": pd.to_numeric(view["probabilidad"], errors="coerce").values,
    "Tipo": [("📈 CALL" if str(t).upper() == "CALL"
              else "📉 PUT" if str(t).upper() == "PUT" else str(t))
             for t in view["tipo"].values],
    "0 DTE": ["✅" if _es_0dte(s, f) else "❌"
              for s, f in zip(view["symbol"].astype(str), view["fecha"].astype(str))],
    "Optionable": [_opt_icon(_es_optionable(s)) for s in view["symbol"].astype(str)],
})

# Filas TODAS blancas (sin bandas por fecha). Solo se colorea el texto de Tipo y se centra
# el contenido de las columnas (menos "Estrategia"). st.dataframe respeta text-align de las
# CELDAS vía Styler (los headers no se pueden centrar: limitación del grid).
_center_cols = [c for c in _show.columns if c != "Estrategia"]
_styled = (_show.style
           .map(lambda _v: "color:#16a34a; font-weight:700" if "CALL" in str(_v)
                else ("color:#ef4444; font-weight:700" if "PUT" in str(_v) else ""),
                subset=["Tipo"])
           .set_properties(subset=_center_cols, **{"text-align": "center"}))

# Selección multi-fila NATIVA (shift+click = rango). La key versionada se resetea al
# cambiar el orden / borrar / limpiar (bump de _sig_ed_v) → la selección queda alineada.
_tkey = f"sig_table_{st.session_state.get('_sig_ed_v', 0)}"
# use_container_width=True → la tabla ocupa todo el ancho de la página. Con anchos por
# columna definidos; si aun así no entran, el grid muestra scroll horizontal.
_event = st.dataframe(
    _styled, use_container_width=True, hide_index=True, key=_tkey,
    on_select="rerun", selection_mode="multi-row",
    column_config={
        "Acción": st.column_config.TextColumn("Acción", width="small"),
        "Hora": st.column_config.TextColumn("Hora", width="small"),
        "Fecha": st.column_config.TextColumn("Fecha", width="medium"),
        "Estrategia": st.column_config.TextColumn("Estrategia", width="large"),
        "% Cumpl.": st.column_config.NumberColumn("% Cumpl.", format="%.0f%%", width="small"),
        "Tipo": st.column_config.TextColumn("Tipo", width="small"),
        "0 DTE": st.column_config.TextColumn("0 DTE", width="small",
                                             help="✅ = el ticker tenía opción 0DTE ese día"),
        "Optionable": st.column_config.TextColumn("Optionable", width="small",
                                                  help="✅ = el ticker tiene opciones vigentes hoy (Polygon) · ❌ = no · — = no verificado"),
        "Criterios": st.column_config.TextColumn("Criterios", width="small"),
    },
)
st.caption("Tocá una fila para seleccionarla · **shift+click** en otra marca el **rango** · "
           "**Ctrl/Cmd+click** suma sueltas. Con filas seleccionadas aparecen **Ver/Eliminar** "
           "arriba y **Backtestear/Operar** abajo.")

# Posiciones seleccionadas (en el orden actual) → ids.
_sel_rows = sorted(_event.selection.rows) if (_event and _event.selection) else []
_sel_ids = [str(view.iloc[i]["id"]) for i in _sel_rows]
st.session_state["bt_selected_ids"] = set(_sel_ids)

# Acciones de ARRIBA (a la derecha del filtro de orden): Ver (sólo con 1 fila) · Eliminar
# (con 1+). El contenedor _acts_ph se creó arriba; lo llenamos ahora con la selección lista.
with _acts_ph:
    if _sel_rows:
        # Botones chicos, del mismo tamaño y pegados a la izquierda: Eliminar · Ver.
        _bd, _bv, _sp = st.columns([1, 1, 6], vertical_alignment="center")
        if _bd.button("🗑 Eliminar", use_container_width=True,
                      key="sig_del_top", help=f"Eliminar las {len(_sel_rows)} fila(s) seleccionada(s)."):
            _confirm_delete(_sel_ids)
        if len(_sel_rows) == 1:
            if _bv.button("🔍 Ver", use_container_width=True, key="sig_ver_top",
                          help="Ver el detalle de la fila seleccionada."):
                _render_detalle(view.iloc[_sel_rows[0]])

# ── Backtest / operar las filas SELECCIONADAS ────────────────────────────────
st.divider()
st.subheader("🔬 Backtest de señales")
if not _sel_rows:
    st.info("Seleccioná una o más alertas en la tabla de arriba "
            "(click · **shift+click** para un rango · **Ctrl/Cmd+click** para sueltas).")
else:
    _sel_df = view.iloc[_sel_rows]
    st.markdown(f"🧺 **{len(_sel_rows)} seleccionada(s):**  " + "  ·  ".join(
        f"{r['symbol']} {r['tipo']} ({r['fecha']} {r['hora']})" for _, r in _sel_df.iterrows()))
    _b1, _b2, _b3 = st.columns([2, 2, 1])
    if _b1.button(f"▶ Backtestear {len(_sel_rows)}  →  Backtesting", type="primary",
                  use_container_width=True):
        st.session_state["bt_signals_handoff"] = [
            {"symbol": str(r["symbol"]), "fecha": str(r["fecha"]), "hora": str(r["hora"]),
             "tipo": str(r["tipo"]), "prob": r.get("probabilidad"),
             "estrategia": str(r.get("estrategia") or "")} for _, r in _sel_df.iterrows()]
        st.session_state["_sig_ed_v"] = st.session_state.get("_sig_ed_v", 0) + 1
        st.session_state["bt_sidebar_collapse"] = True   # llegar a Backtesting con la sidebar contraída
        st.switch_page("options_replay/app.py")
    if _b2.button(f"🟢 Operar {len(_sel_rows)} (paper)  →  Live", use_container_width=True,
                  help="Abre 1 posición por alerta en el sandbox (paper, NO dinero real). El "
                       "daemon monitorea y vende al Umbral de ROI. Requiere mercado abierto."):
        st.session_state["live_alerts_handoff"] = [
            {"id": str(r["id"]), "symbol": str(r["symbol"]), "tipo": str(r["tipo"])}
            for _, r in _sel_df.iterrows()]
        st.switch_page("live_trader/ui/app.py")
    if _b3.button("🧹 Limpiar", use_container_width=True, help="Vaciar la selección actual."):
        st.session_state["_sig_ed_v"] = st.session_state.get("_sig_ed_v", 0) + 1
        st.rerun()

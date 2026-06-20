"""Página 'Trading view' (submenú de Alertas).

Backtesting de Señales de TradingView: el usuario sube un CSV con las señales del indicador
(Trend Reversal Up & Down BB 15m, etc.) exportadas desde TradingView, y se backtestean con el
MISMO motor (signals_backtest.run_one) y las MISMAS métricas (analytics.backtest_risk_metrics)
que la sección de Backtesting.

Importador FLEXIBLE: detecta las columnas por nombre (sin distinguir mayúsculas/acentos, con
sinónimos) y tolera columnas extra de TradingView sin romper. Formato mínimo esperado:
  Accion · Fecha · Hora · Nombre de estrategia · Tipo de señal (CALL/PUT)
"""
from __future__ import annotations

import io
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import analytics  # noqa: E402
import signals_backtest as sbt  # noqa: E402

try:
    st.set_page_config(page_title="Backtesting Señales TradingView", layout="wide")
except Exception:  # noqa: BLE001
    pass

# ─────────────────────────── Helpers de parsing flexible ───────────────────────────

def _norm(s: str) -> str:
    """Normaliza un header: minúsculas, sin acentos, sin espacios extra."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


# Sinónimos por campo lógico → set de headers normalizados que matchean.
_SYN = {
    "ticker":     {"accion", "ticker", "symbol", "simbolo", "activo", "subyacente"},
    "fecha":      {"fecha", "date", "dia", "day"},
    "hora":       {"hora", "time", "time_et", "hora_et", "horario"},
    "estrategia": {"nombre de estrategia", "estrategia", "strategy", "nombre_estrategia",
                   "nombre estrategia", "setup"},
    "tipo":       {"tipo de senal", "tipo de signal", "tipo", "signal", "direccion", "side",
                   "tipo_senal", "tipo senal", "call/put", "callput"},
}


def _map_columns(cols) -> dict:
    """Devuelve {campo_lógico: nombre_real_de_columna} para los que encuentre."""
    found = {}
    norm2real = {_norm(c): c for c in cols}
    for field, syns in _SYN.items():
        for nc, real in norm2real.items():
            if nc in syns:
                found[field] = real
                break
    return found


# CALL/PUT del CSV → texto canónico. Acepta variantes (call, c, compra, alza, ▲ / put, p, baja…).
def _canon_tipo(v) -> str | None:
    t = _norm(v)
    if t in {"call", "c", "compra", "alza", "up", "buy", "long", "verde", "green", "u"}:
        return "CALL"
    if t in {"put", "p", "venta", "baja", "down", "sell", "short", "rojo", "red", "d", "dw"}:
        return "PUT"
    if "call" in t or "long" in t:
        return "CALL"
    if "put" in t or "short" in t:
        return "PUT"
    return None


# Valores típicos de la columna "Type" del "List of Trades" de TradingView (para filtrar entradas).
_ENTRY_EXIT = {"entry long", "exit long", "entry short", "exit short", "entry", "exit",
               "entrada", "salida"}
# Headers de una columna fecha+hora combinada (TradingView XLSX exporta "Date and time";
# el "Export chart data"/CSV usa "Date/Time"). Normalizados (sin acentos, minúsculas).
_DT_NAMES = {"date and time", "date/time", "datetime", "date time", "fecha y hora",
             "fecha/hora", "fecha hora", "timestamp"}


def parse_signals_csv(df: pd.DataFrame, default_ticker: str = "") -> tuple[pd.DataFrame, list[str]]:
    """CSV crudo → DataFrame normalizado [ticker, fecha, hora, estrategia, tipo, _extra...].
    Devuelve (df_norm, warnings). Tolera columnas faltantes/extra."""
    warns: list[str] = []
    df = df.reset_index(drop=True).copy()

    # (A) Formato "List of Trades" de TradingView: una columna trae Entry/Exit por trade
    #     (2 filas por operación). La detectamos POR VALOR y nos quedamos solo con las entradas.
    for c in df.columns:
        v = df[c].astype(str).map(_norm)
        if v.isin(_ENTRY_EXIT).mean() > 0.6:
            keep = v.str.contains("entry") | v.str.contains("entrada")
            drop = int((~keep).sum())
            df = df[keep].reset_index(drop=True)
            if drop:
                warns.append(f"Detecté formato «List of Trades» de TradingView → me quedé con "
                             f"{int(keep.sum())} entradas (descarté {drop} fila(s) de salida).")
            break

    m = _map_columns(df.columns)
    dt_col = next((c for c in df.columns if _norm(c) in _DT_NAMES), None)  # "Date/Time" combinada

    have_date = ("fecha" in m) or (dt_col is not None)
    have_time = ("hora" in m) or (dt_col is not None) or ("fecha" in m)
    if not have_date or not have_time or "tipo" not in m:
        raise ValueError("Faltan columnas obligatorias (fecha, hora y tipo de señal). "
                         f"Detecté: {df.columns.tolist()}")

    out = pd.DataFrame(index=df.index)   # mismo índice que el CSV → los scalars se propagan a todas las filas
    # Ticker (opcional → fallback al default de la UI)
    if "ticker" in m:
        out["ticker"] = df[m["ticker"]].astype(str).str.strip().str.upper()
    else:
        if not default_ticker:
            raise ValueError("El CSV no trae columna de Acción/Ticker y no elegiste un ticker por defecto.")
        out["ticker"] = default_ticker.strip().upper()
        warns.append(f"Sin columna de ticker → uso «{default_ticker.upper()}» para todas las filas.")

    # Fecha + Hora — de columnas separadas (formato limpio) o de un datetime combinado (TradingView).
    _src = pd.to_datetime(df[m["fecha"]] if "fecha" in m else df[dt_col], errors="coerce")
    out["fecha"] = _src.dt.strftime("%Y-%m-%d")
    if "hora" in m:
        _ht = pd.to_datetime(df[m["hora"]].astype(str).str.strip(), errors="coerce", format="mixed")
        out["hora"] = _ht.dt.strftime("%H:%M")
        out.loc[out["hora"].isna(), "hora"] = _src.dt.strftime("%H:%M")[out["hora"].isna()]
    else:  # sin columna de hora → de la fecha/datetime combinado
        out["hora"] = (pd.to_datetime(df[dt_col], errors="coerce") if dt_col else _src).dt.strftime("%H:%M")

    out["estrategia"] = (df[m["estrategia"]].astype(str).str.strip() if "estrategia" in m else "")
    out["tipo_senal"] = df[m["tipo"]].map(_canon_tipo)

    # Columnas extra (las que no mapeamos) → se preservan con prefijo, sin afectar el motor.
    mapped_reals = set(m.values()) | ({dt_col} if dt_col else set())
    for c in df.columns:
        if c not in mapped_reals:
            out[f"extra::{c}"] = df[c].values

    bad = out["fecha"].isna() | out["hora"].isna() | out["tipo_senal"].isna()
    if bad.any():
        warns.append(f"Descarté {int(bad.sum())} fila(s) sin fecha/hora/tipo válidos.")
    out = out[~bad].reset_index(drop=True)
    return out, warns


# ─────────────────────────── Backtest (reusa el motor) ───────────────────────────

# (nbbo_entrada_salida, timeline_fase2) — entrada y salida usan el mismo flag en los 3 modos.
_FILL_FLAGS = {
    "Precio de barra (rápido)":           (False, False),
    "NBBO entrada/salida (Fase 1)":       (True, False),
    "NBBO por barra (Fase 2)":            (True, True),
}
_CRIT_KEY = {"Opción 1 — Menor spread": "spread",
             "Opción 2 — Primer contrato cerca de ITM": "itm_first"}


@st.cache_resource(show_spinner=False)
def _downloader():
    import config
    from adapter_polygon import PolygonAdapter
    from downloader import Downloader
    return Downloader(PolygonAdapter(config.POLYGON_API_KEY), HERE / "data")


def _op_tipo(signal: str, modo: str) -> str:
    """Mapea (señal CALL/PUT, modo de operación elegido) → 'tipo' que entiende run_one."""
    if modo == "Direccional según la señal":
        return "CALL" if signal == "CALL" else "PUT"
    if modo == "Straddle (CALL y PUT)":
        return "CALL y PUT"
    if modo == "Straddle + Refuerzo":
        return "CALL y PUT (Refuerzo)"
    return "CALL" if signal == "CALL" else "PUT"


def run_backtest(iters_df: pd.DataFrame, cfg: dict) -> dict:
    """Corre run_one por cada fila SELECCIONADA (✓), respetando Tipo/Criterio/Fills POR FILA
    (igual que el panel de Backtesting). En paralelo. Devuelve {results, skipped}."""
    dl = _downloader()
    dl.resolution = cfg["resolution"]
    sel = iters_df[iters_df["✓"] == True] if "✓" in iters_df.columns else iters_df  # noqa: E712

    specs = []
    for _, r in sel.iterrows():
        _fl = str(r.get("Fills", "(default)"))
        _fl = _fl if _fl in _FILL_FLAGS else cfg["fills_default"]      # "(default)" → global
        f1, f2 = _FILL_FLAGS[_fl]
        crit = _CRIT_KEY.get(str(r.get("Criterio", "")), "spread")
        specs.append({"ticker": str(r["Ticker"]).upper(), "fecha": str(r["Fecha"]),
                      "hora": str(r["Hora"]), "tipo": str(r["Tipo"]),
                      "estrategia": str(r.get("Estrategia", "")), "f1": f1, "f2": f2, "crit": crit})

    def _run(idx_spec):
        i, s = idx_spec
        try:
            r = sbt.run_one(dl, {"ticker": s["ticker"], "fecha": s["fecha"], "hora": s["hora"],
                                 "tipo": s["tipo"]},
                            inversion=cfg["inv"], umbral_pct=cfg["umbral"], stop_pct=cfg["stop"],
                            iteration_idx=i + 1, entry_at_ask=s["f1"], exit_at_bid=s["f1"],
                            selection_criterion=s["crit"], refuerzo_loss_pct=cfg["ref_loss"],
                            refuerzo_max=cfg["ref_max"], call_pct=cfg["call_pct"],
                            nbbo_timeline=s["f2"], search_window_min=cfg["search"])
            r["_spec"] = s
            return r
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": str(e), "_spec": s,
                    "ticker": s["ticker"], "fecha": s["fecha"], "hora": s["hora"]}

    results, skipped = [], []
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        for r in ex.map(_run, list(enumerate(specs))):
            (results if r.get("status") == "ok" else skipped).append(r)
    return {"results": results, "skipped": skipped}


def _result_rows(results: list) -> list[dict]:
    """Filas para la tabla + métricas, desde los IterationResult."""
    rows = []
    for r in results:
        it = r["iteration"]
        inv = it.invest_total
        s = r["_spec"]
        rows.append({
            "Fecha": s["fecha"], "Hora": str(getattr(it, "start_dt", ""))[11:16],
            "Ticker": s["ticker"], "Tipo": s["tipo"], "Estrategia": s.get("estrategia", ""),
            "Ganancia $": round(it.gain_total, 2), "Inversión $": round(inv, 2),
            "ROI %": round((it.gain_total / inv * 100) if inv else 0.0, 1),
            "Salida": getattr(it, "exit_reason", ""),
        })
    return rows


def _metric_rows(results: list) -> list[dict]:
    out = []
    for r in results:
        it = r["iteration"]
        inv = it.invest_total
        out.append({"fecha": r["_spec"]["fecha"], "gain": it.gain_total, "invest": inv,
                    "roi": (it.gain_total / inv) if inv else 0.0})
    return out


# ──────────────────────────────────── UI ────────────────────────────────────

st.title("📈 Backtesting de Señales de TradingView")
st.caption("Subí el CSV de señales exportado desde TradingView y backtesteá con el mismo motor "
           "y métricas que la sección de Backtesting.")

with st.expander("ⓘ ¿Cómo exporto el CSV desde TradingView? · formato esperado", expanded=False):
    st.markdown(
        "**Formato mínimo de columnas** (los nombres son flexibles — acepta sinónimos y columnas extra):\n\n"
        "| Accion | Fecha | Hora | Nombre de estrategia | Tipo de señal |\n"
        "|---|---|---|---|---|\n"
        "| QQQ | 2026-06-18 | 09:30 | Cambio de Tendencia en 15m | CALL |\n"
        "| SPY | 2026-06-18 | 09:30 | Cambio de Tendencia en 15m | PUT |\n\n"
        "- **Fecha + Hora** = momento exacto de la alerta (▲ verde = CALL · ▼ rojo = PUT).\n"
        "- Columnas adicionales de TradingView se **conservan** y no rompen la importación.\n\n"
        "**Para generar el CSV** (indicador *Trend Reversal Up & Down BB 15m*): el indicador hoy "
        "dibuja las señales con `plotshape` + `alertcondition`, que **no** se exportan con «Export "
        "chart data». Agregale estas 2 líneas para que la señal salga como columna exportable:\n")
    st.code('plot(breakoutUp ? 1 : na, "Signal CALL", display=display.data_window)\n'
            'plot(breakoutDn ? 1 : na, "Signal PUT",  display=display.data_window)', language="text")
    st.markdown(
        "Después, en TradingView: timeframe **15m** → cargá el rango de fechas (scrolleá hacia atrás) → "
        "menú **⋮ del panel / clic derecho en el chart → «Export chart data…»** → CSV. Filtrá las filas "
        "donde *Signal CALL* o *Signal PUT* = 1 y armá las columnas de arriba (o subí el export tal cual "
        "y elegí el ticker abajo).")

up = st.file_uploader("📤 Subí el CSV o XLSX de señales (export de TradingView)",
                      type=["csv", "xlsx"], key="tv_csv")
default_ticker = st.text_input("Ticker por defecto (si el CSV no trae columna Acción)", value="QQQ",
                               help="Se usa solo si el CSV no tiene columna de ticker/acción.").strip()

if up is not None:
    _name, _data = up.name.lower(), up.getvalue()
    try:
        if _name.endswith(".xlsx"):
            # Export de TradingView: workbook con varias hojas → buscar la de operaciones.
            _sheets = pd.read_excel(io.BytesIO(_data), sheet_name=None)
            raw, _src = None, None
            for _sn in sorted(_sheets, key=lambda n: 0 if any(
                    k in n.lower() for k in ("trade", "operac", "lista")) else 1):
                try:
                    if not parse_signals_csv(_sheets[_sn], default_ticker)[0].empty:
                        raw, _src = _sheets[_sn], _sn
                        break
                except Exception:  # noqa: BLE001 — hoja sin señales (Performance, etc.) → siguiente
                    continue
            if raw is None:
                st.error(f"No encontré una hoja con operaciones en el XLSX. Hojas: {list(_sheets)}. "
                         "Probá abrir el Excel, ir a la hoja de trades y guardarla como CSV.")
                st.stop()
            st.caption(f"📄 Leído de la hoja «{_src}» del XLSX.")
        else:
            raw = pd.read_csv(io.BytesIO(_data), sep=None, engine="python")
    except Exception as e:  # noqa: BLE001
        st.error(f"No pude leer el archivo: {e}")
        st.stop()
    try:
        sig_df, warns = parse_signals_csv(raw, default_ticker)
    except ValueError as e:
        st.error(f"⚠ {e}")
        st.stop()
    for w in warns:
        st.warning(w)
    if sig_df.empty:
        st.error("No quedaron señales válidas tras el parseo.")
        st.stop()

    st.success(f"✅ {len(sig_df)} señales importadas "
               f"({(sig_df['tipo_senal'] == 'CALL').sum()} CALL · {(sig_df['tipo_senal'] == 'PUT').sum()} PUT).")
    # Sembrar la tabla de iteraciones con el MISMO formato que el panel de Backtesting.
    # Solo al subir un archivo NUEVO (para no pisar ediciones del usuario en cada rerun).
    _fid = f"{up.name}:{len(_data)}"
    if st.session_state.get("tv_fid") != _fid:
        st.session_state["tv_fid"] = _fid
        st.session_state["tv_iters"] = pd.DataFrame({
            "✓": True, "Ticker": sig_df["ticker"], "Fecha": sig_df["fecha"], "Hora": sig_df["hora"],
            "Tipo": sig_df["tipo_senal"], "Criterio": "Opción 1 — Menor spread", "Fills": "(default)",
            "% Cumpl.": [float("nan")] * len(sig_df), "Estrategia": sig_df["estrategia"].astype(str),
        }).reset_index(drop=True)
        st.session_state.pop("tv_iters_editor", None)

# ───────── Tabla de iteraciones (MISMO formato que Backtesting) + parámetros globales ─────────
if "tv_iters" in st.session_state:
    st.divider()
    st.subheader("🔎 Señales a backtestear")
    st.caption("Mismo formato que el panel de Backtesting. Editá **Tipo / Criterio / Fills** por fila; "
               "destildá ✓ para excluir una señal.")
    _TIPOS = ["CALL", "PUT", "CALL y PUT", "CALL y PUT (Refuerzo)", "CALL y PUT (plus)",
              "CALL o PUT", "CALL o PUT (plus)"]
    edited = st.data_editor(
        st.session_state["tv_iters"], key="tv_iters_editor", use_container_width=True, height=340,
        column_config={
            "✓": st.column_config.CheckboxColumn("✓", default=True),
            "Ticker": st.column_config.TextColumn("Ticker"),
            "Fecha": st.column_config.TextColumn("Fecha"),
            "Hora": st.column_config.TextColumn("Hora"),
            "Tipo": st.column_config.SelectboxColumn("Tipo", options=_TIPOS, required=True),
            "Criterio": st.column_config.SelectboxColumn("Criterio", options=list(_CRIT_KEY.keys()),
                                                         required=True),
            "Fills": st.column_config.SelectboxColumn("Fills", options=["(default)"] + list(_FILL_FLAGS.keys()),
                                                      required=True),
            "% Cumpl.": st.column_config.NumberColumn("% Cumpl.", disabled=True, format="%.0f"),
            "Estrategia": st.column_config.TextColumn("Estrategia", disabled=True),
        })

    st.subheader("⚙️ Parámetros globales")
    g1, g2, g3 = st.columns(3)
    inv = g1.number_input("Inversión ($)", min_value=100.0, value=1000.0, step=100.0)
    call_pct = g1.number_input("Inversión CALL (%)", min_value=0.0, max_value=100.0, value=50.0, step=5.0)
    umbral = g2.number_input("Umbral ROI (%)", value=10.0, step=5.0)
    stop = g2.number_input("Stop loss (%)", value=-100.0, step=10.0)
    fills_default = g3.selectbox("Modelo de fills (por defecto · para filas en «(default)»)",
                                 list(_FILL_FLAGS.keys()), index=2)
    search = g3.number_input("Ventana de búsqueda (min)", min_value=0.0, value=4.0, step=1.0)
    r1, r2, r3 = st.columns(3)
    ref_loss = r1.number_input("Umbral pérdida refuerzo (%)", value=50.0, step=10.0) / 100.0
    ref_max = int(r2.number_input("Nº de veces a reforzar", min_value=0, value=4, step=1))
    resolution = {"1 min": "1min", "30 seg": "30s", "15 seg": "15s"}[
        r3.selectbox("Resolución de barras", ["1 min", "30 seg", "15 seg"])]

    if st.button("▶ Correr backtest", type="primary"):
        cfg = {"inv": inv, "call_pct": call_pct, "umbral": umbral, "stop": stop, "search": search,
               "fills_default": fills_default, "ref_loss": ref_loss, "ref_max": ref_max,
               "resolution": resolution, "workers": 8}
        with st.spinner("Backtesteando señales…"):
            st.session_state["tv_replay"] = run_backtest(edited, cfg)

# ──────────────────────────────── Resultados ────────────────────────────────
if "tv_replay" in st.session_state:
    rep = st.session_state["tv_replay"]
    results, skipped = rep["results"], rep["skipped"]
    st.divider()
    st.subheader("📊 Resultados")
    if skipped:
        st.caption(f"⏭ {len(skipped)} señal(es) sin resultado (sin 0DTE ese día o error).")
    if not results:
        st.warning("Ninguna señal produjo un backtest válido.")
        st.stop()

    rows = _metric_rows(results)
    m = analytics.backtest_risk_metrics(rows)
    tot_inv = sum(r["invest"] for r in rows) or 1.0
    wr = sum(1 for r in rows if r["gain"] > 0) / m["n"] * 100.0

    k = st.columns(4)
    k[0].metric("Señales", m["n"])
    k[1].metric("Ganancia total", f"${m['total_gain']:,.0f}", f"{m['total_gain']/tot_inv*100:+.1f}%")
    k[2].metric("Profit Factor", "∞" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}")
    k[3].metric("Win rate", f"{wr:.0f}%")
    k2 = st.columns(4)
    k2[0].metric("Max drawdown", f"${m['max_drawdown']:,.0f}")
    k2[1].metric("Peor día", f"{m['worst_roi']*100:.0f}%", m["worst_fecha"])
    k2[2].metric("Sortino", "∞" if m["sortino"] == float("inf") else f"{m['sortino']:.2f}")
    k2[3].metric("Días ≤ −90%", m["n_catastrophic"])

    if m.get("equity_curve"):
        st.caption("Curva de equity (ganancia acumulada por señal, orden cronológico)")
        st.line_chart(pd.DataFrame({"equity": m["equity_curve"]}))

    st.dataframe(pd.DataFrame(_result_rows(results)), use_container_width=True, height=320)

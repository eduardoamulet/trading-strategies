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
# Bolsas que TradingView pone en el nombre del export, justo antes del ticker.
_EXCHANGES = {"amex", "nasdaq", "nyse", "bats", "arca", "cboe", "nysearca", "nyseamerican",
              "otc", "bmv", "tsx", "lse"}


def _ticker_from_filename(name: str) -> str | None:
    """Extrae el ticker del nombre de un export de TradingView, ej.
    'TR-UD-15m-EXP_AMEX_IWM_2026-06-21_93e77.xlsx' → 'IWM'. None si no lo reconoce."""
    stem = str(name).rsplit(".", 1)[0]
    parts = [p for p in stem.split("_") if p]

    def _ok(c):
        return c.isalpha() and 1 <= len(c) <= 6

    def _is_date(s):
        return len(s) == 10 and s[4:5] == "-" and s[7:8] == "-" and s.replace("-", "").isdigit()
    # 1) el segmento JUSTO DESPUÉS de la bolsa (AMEX_IWM → IWM)
    for i, p in enumerate(parts[:-1]):
        if p.lower() in _EXCHANGES and _ok(parts[i + 1]):
            return parts[i + 1].upper()
    # 2) el segmento JUSTO ANTES de una fecha YYYY-MM-DD
    for i, p in enumerate(parts):
        if _is_date(p) and i >= 1 and _ok(parts[i - 1]):
            return parts[i - 1].upper()
    return None


# Patrón en el nombre del archivo → estrategia (los exports de TradingView no traen la estrategia
# en la hoja; la derivamos del nombre). Agregá pares acá para mapear más indicadores.
_STRATEGY_BY_FILENAME = [
    ("tr-ud-15", "Cambio de Tendencia en 15m"),
]


def _strategy_from_filename(name: str) -> str | None:
    """Estrategia inferida del nombre del archivo (ej. 'TR-UD-15…' → 'Cambio de Tendencia en 15m')."""
    n = str(name).lower()
    for pat, label in _STRATEGY_BY_FILENAME:
        if pat in n:
            return label
    return None


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
    "NBBO por barra · triggers sobre el bid (Fase 2)": (True, True),
}
_CRIT_KEY = {"Opción 1 — Menor spread": "spread",
             "Opción 2 — Primer contrato cerca de ITM": "itm_first"}


# NOTA: el backtest de estas señales NO corre acá. El botón «▶ Backtestear → Backtesting» las manda
# (vía bt_signals_handoff) a la sección Backtesting, que usa el ÚNICO motor (signals_backtest.run_one)
# y la ÚNICA UI de resultados — la misma que las alertas de Investep y el backtest manual. Antes había
# un run_backtest/_result_rows/_metric_rows/_downloader DUPLICADOS acá; se eliminaron para no tener un
# segundo engine ni una segunda interfaz de resultados.


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
        "donde *Signal CALL* o *Signal PUT* = 1 y armá las columnas de arriba (o subí el export tal cual: "
        "detecto el ticker del nombre del archivo).")

ups = st.file_uploader("📤 Subí uno o varios CSV/XLSX de señales (export de TradingView)",
                       type=["csv", "xlsx"], key="tv_csv", accept_multiple_files=True)

if ups:
    _frames, _fid_parts = [], []
    for _up in ups:
        _name, _data = _up.name.lower(), _up.getvalue()
        _fid_parts.append(f"{_up.name}:{len(_data)}")
        # Ticker del NOMBRE del archivo (..._AMEX_IWM_2026-…). Si no, la columna Acción del archivo.
        _fn_tk = _ticker_from_filename(_up.name)
        _eff_ticker = _fn_tk or ""
        try:
            if _name.endswith(".xlsx"):
                # Workbook de TradingView con varias hojas → buscar la de operaciones.
                _sheets = pd.read_excel(io.BytesIO(_data), sheet_name=None)
                raw, _srclbl = None, None
                for _sn in sorted(_sheets, key=lambda n: 0 if any(
                        k in n.lower() for k in ("trade", "operac", "lista")) else 1):
                    try:
                        if not parse_signals_csv(_sheets[_sn], _eff_ticker)[0].empty:
                            raw, _srclbl = _sheets[_sn], f"hoja «{_sn}»"
                            break
                    except Exception:  # noqa: BLE001 — hoja sin señales (Performance, etc.) → siguiente
                        continue
                if raw is None:
                    st.error(f"⚠ **{_up.name}**: no encontré una hoja con operaciones (hojas: {list(_sheets)}).")
                    continue
            else:
                raw, _srclbl = pd.read_csv(io.BytesIO(_data), sep=None, engine="python"), "CSV"
            sig_df, warns = parse_signals_csv(raw, _eff_ticker)
        except Exception as e:  # noqa: BLE001
            st.error(f"⚠ **{_up.name}**: {e}")
            continue
        if sig_df.empty:
            st.warning(f"**{_up.name}**: sin señales válidas — lo salteo.")
            continue
        # Estrategia derivada del NOMBRE del archivo (los exports de TradingView no la traen en la
        # hoja). Ej.: "TR-UD-15…" → «Cambio de Tendencia en 15m». Solo rellena las que vengan vacías.
        _fn_strat = _strategy_from_filename(_up.name)
        if _fn_strat:
            _blank = sig_df["estrategia"].astype(str).str.strip() == ""
            sig_df.loc[_blank, "estrategia"] = _fn_strat
        _tklbl = _fn_tk or "(ticker del archivo)"
        st.success(f"✅ **{_up.name}** ({_srclbl}) · **{_tklbl}** → {len(sig_df)} señales "
                   f"({(sig_df['tipo_senal'] == 'CALL').sum()} CALL · {(sig_df['tipo_senal'] == 'PUT').sum()} PUT).")
        for w in warns:
            st.caption(f"　↳ {w}")
        _frames.append(sig_df)

    if _frames:
        _combined = pd.concat(_frames, ignore_index=True)
        if len(_frames) > 1:
            st.success(f"📦 **Total combinado: {len(_combined)} señales** de {len(_frames)} archivos · "
                       f"{', '.join(sorted(_combined['ticker'].unique()))}.")
        # Sembrar la tabla SOLO cuando cambia el conjunto de archivos (no pisar ediciones en cada rerun).
        _fid = "|".join(sorted(_fid_parts))
        if st.session_state.get("tv_fid") != _fid:
            st.session_state["tv_fid"] = _fid
            st.session_state["tv_iters"] = pd.DataFrame({
                "✓": True, "Ticker": _combined["ticker"], "Fecha": _combined["fecha"],
                "Hora": _combined["hora"], "Tipo": _combined["tipo_senal"],
                "Criterio": "Opción 1 — Menor spread",
                "Fills": "NBBO por barra · triggers sobre el bid (Fase 2)",
                "% Cumpl.": [float("nan")] * len(_combined),
                "Estrategia": _combined["estrategia"].astype(str),
            }).reset_index(drop=True)
            st.session_state.pop("tv_iters_editor", None)

# ───────── Tabla de iteraciones (MISMO formato que Backtesting) + parámetros globales ─────────
if "tv_iters" in st.session_state:
    st.divider()
    st.subheader("🔎 Señales a backtestear")
    st.caption("Mismo formato que el panel de Backtesting. Editá **Tipo / Criterio / Fills** por fila; "
               "destildá ✓ para excluir una señal.")
    # --- 🔎 Filtros: por acción + rango de fechas (desde / hasta). Acota qué señales se ven
    #     en la tabla y, por ende, cuáles se backtestean. Vacío = todas. NO toca `tv_iters`. ---
    _tv_all = st.session_state["tv_iters"]
    _tv_tks = sorted({str(t).strip() for t in _tv_all["Ticker"] if str(t).strip()})
    _tv_dts = pd.to_datetime(_tv_all["Fecha"], errors="coerce")
    _tvmin, _tvmax = _tv_dts.min(), _tv_dts.max()
    _tv_date_opts = ["(todas)"] + sorted({d.strftime("%Y-%m-%d") for d in _tv_dts.dropna()},
                                         reverse=True)   # más reciente → más antigua
    # Modo de búsqueda por fecha: EXCLUYENTE — o rango (desde/hasta) o fecha exacta, nunca ambos.
    _mode = st.radio("Buscar por fecha", ["Rango de fechas", "Fecha exacta"],
                     key="tv_flt_mode", horizontal=True,
                     help="Elegí UN modo: rango (desde/hasta) o una fecha exacta puntual. No se combinan.")
    _fc1, _fc2, _fc3 = st.columns([2, 1.3, 1.3])
    _ftk = _fc1.multiselect("🔎 Filtrar por acción", _tv_tks, default=[], key="tv_flt_tks",
                            help="Vacío = todas las acciones. Elegí una o más para acotar el backtest.")
    _ffrom = _fto = None
    _fexact = "(todas)"
    if _mode == "Rango de fechas":
        _ffrom = _fc2.date_input("Fecha desde", value=None, key="tv_flt_from",
                                 format="YYYY-MM-DD", help="Vacío = sin límite inferior.")
        _fto = _fc3.date_input("Fecha hasta", value=None, key="tv_flt_to",
                               format="YYYY-MM-DD", help="Vacío = sin límite superior.")
    else:
        _fexact = _fc2.selectbox(
            "📅 Fecha exacta", _tv_date_opts, index=0, key="tv_flt_exact",
            help="Buscá UNA fecha específica con señales (escribí para filtrar la lista). "
                 "«(todas)» = no filtra por fecha.")
    _m = pd.Series(True, index=_tv_all.index)
    if _ftk:
        _m &= _tv_all["Ticker"].astype(str).str.strip().isin(_ftk)
    if _mode == "Fecha exacta":
        if _fexact != "(todas)":
            _m &= _tv_dts.dt.strftime("%Y-%m-%d") == _fexact
    else:
        if _ffrom is not None:
            _m &= _tv_dts >= pd.Timestamp(_ffrom)
        if _fto is not None:
            _m &= _tv_dts <= pd.Timestamp(_fto)
    _tv_view = _tv_all[_m].reset_index(drop=True)
    # Re-siembra la tabla al cambiar el filtro (descarta el estado viejo del data_editor).
    _tv_flt_sig = (tuple(_ftk), _mode, str(_ffrom), str(_fto), _fexact)
    if st.session_state.get("_tv_flt_last") != _tv_flt_sig:
        st.session_state["_tv_flt_last"] = _tv_flt_sig
        st.session_state.pop("tv_iters_editor", None)
    if len(_tv_view) != len(_tv_all):
        _rng = (f" · disponible {_tvmin.date()} → {_tvmax.date()}"
                if pd.notna(_tvmin) and pd.notna(_tvmax) else "")
        st.caption(f"🔎 Filtro activo: **{len(_tv_view)}** de {len(_tv_all)} señales{_rng}.")
    if _tv_view.empty:
        st.warning("Ninguna señal cumple el filtro. Ajustá la acción, el rango o la fecha exacta.")
    _TIPOS = ["CALL", "PUT", "CALL y PUT", "CALL y PUT (Refuerzo)", "CALL y PUT (plus)",
              "CALL o PUT", "CALL o PUT (plus)"]
    edited = st.data_editor(
        _tv_view, key="tv_iters_editor", use_container_width=True, height=340,
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

    st.divider()
    # Handoff a la sección Backtesting: las señales seleccionadas se cargan en su panel
    # «Backtest de señales / iteraciones», donde se eligen los parámetros y se ven los resultados
    # — LA MISMA interfaz rica que usan las alertas de Investep y el backtest manual (origen único).
    _sel = edited[edited["✓"] == True] if "✓" in edited.columns else edited  # noqa: E712
    _nsel = len(_sel)
    if st.button(f"▶ Backtestear {_nsel} señal(es)  →  Backtesting", type="primary",
                 disabled=_nsel == 0, use_container_width=True):
        st.session_state["bt_signals_handoff"] = [
            {"symbol": str(r["Ticker"]), "fecha": str(r["Fecha"]), "hora": str(r["Hora"]),
             "tipo": str(r["Tipo"]),
             "prob": (None if pd.isna(r.get("% Cumpl.")) else r.get("% Cumpl.")),
             "estrategia": str(r.get("Estrategia") or ""),
             "criterio": str(r.get("Criterio") or ""), "fills": str(r.get("Fills") or ""),
             # El anti-lookahead del panel SOLO aplica a señales de Trading view (la Hora del
             # export TR-UD es la APERTURA de la vela) — este marcador lo habilita allá.
             "origen": "tradingview"}
            for _, r in _sel.iterrows()]
        st.session_state["bt_sidebar_collapse"] = True   # llegar a Backtesting con la sidebar contraída
        st.switch_page("options_replay/app.py")
    st.caption("Las señales se cargan en **Backtesting → «Backtest de señales / iteraciones»**: ahí "
               "elegís los parámetros (inversión, ROI, stop, DTE, horarios, fills, ventana) y ves los "
               "resultados — la **misma interfaz** que las alertas de Investep y el backtest manual.")

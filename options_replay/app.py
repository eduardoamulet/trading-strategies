"""Streamlit UI — generic intraday options replay."""
from __future__ import annotations

import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls, time as time_cls, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402
from engine import (  # noqa: E402
    IterationResult,
    NoMatchError,
    _robust_quote,
    run_next_iteration,
    validate_0dte_session,
)
import signals_backtest as sbt  # noqa: E402
from portfolio_exit import apply_collective_exit  # noqa: E402
from ui_charts import _LWC_TF, _render_lwc_chart  # noqa: E402  (gráfico compartido con Simulación)

DATA_DIR = HERE / "data"
TICKER_INFO_PATH = HERE / "ticker_info.json"
MARKET_HOURS_PATH = HERE / "market_hours.json"

# Comisión por contrato (USD). Usada en el reporte de "Operaciones" por iteración.
COMMISSION_PER_CONTRACT = 1.0

# Etiquetas/íconos de la razón de salida (definidos acá arriba para que también los
# use la tabla "en vivo" del backtest paralelo, que corre antes del render final).
_REASON_LABELS = {
    "100%_threshold": "Exit por umbral de profit",
    "stop_loss": "Exit por STOP LOSS",
    "session_end": "Sin trigger — corre hasta cierre",
    "overnight_1dte": "Venta overnight (1 DTE, día hábil siguiente)",
    "wrong_direction": "Señal en sentido del movimiento equivocado",
    "weak_confirmation": "Confirmación débil (vela doji, sin convicción)",
    "collective_roi": "Cierre por ROI colectivo (cartera)",
    "collective_stop": "Cierre por Stop loss colectivo (cartera)",
}


def _max_leg_roi_vals(it) -> tuple:
    """(máx ROI CALL, máx ROI PUT) en % — el ROI MÁXIMO que alcanzó cada pierna (precio de la
    pierna vs su prima de entrada) durante la vida de la posición; it.df ya viene truncado al
    cierre real (colectivo incluido), así que es el máx MIENTRAS se tuvo la posición. ROI de
    precio puro (sin tranches de refuerzo). None si la pierna no existe."""
    _call = _put = None
    try:
        _df = getattr(it, "df", None)
        if _df is not None and len(_df):
            _ce = getattr(it, "call_entry_premium", None)
            if _ce and "call_px" in _df.columns:
                _call = float((_df["call_px"].max() - _ce) / _ce) * 100.0
            _pe = getattr(it, "put_entry_premium", None)
            if _pe and "put_px" in _df.columns:
                _put = float((_df["put_px"].max() - _pe) / _pe) * 100.0
    except Exception:  # noqa: BLE001 — un timeline raro nunca debe romper el render
        pass
    return _call, _put


def _max_leg_rois(it) -> str:
    """«· máx CALL +x% / PUT +y%» para el título de la iteración (ver _max_leg_roi_vals)."""
    _call, _put = _max_leg_roi_vals(it)
    _parts = ([f"CALL {_call / 100:+.1%}"] if _call is not None else []) \
        + ([f"PUT {_put / 100:+.1%}"] if _put is not None else [])
    return ("  ·  máx " + " / ".join(_parts)) if _parts else ""


def _op_dur(it) -> str:
    """Duración de la operación (entrada → salida): '2h 15m' / '45m' / '30s'."""
    try:
        _s = max(0, int((it.end_dt - it.start_dt).total_seconds()))
    except Exception:
        return "—"
    _h, _r = divmod(_s, 3600)
    _m, _sec = divmod(_r, 60)
    return f"{_h}h {_m:02d}m" if _h else (f"{_m}m" if _m else f"{_sec}s")


_REASON_ICONS = {
    "100%_threshold": "🎯",
    "stop_loss": "🛑",
    "session_end": "🕓",
    "overnight_1dte": "🌙",
    "wrong_direction": "🧭",
    "weak_confirmation": "〰️",
    "collective_roi": "🟰",
    "collective_stop": "🟥",
}


@st.cache_data
def load_market_hours() -> dict:
    """Config de horario de mercado: {default:{open,close}, overrides:{TICKER:{open,close}}}."""
    if not MARKET_HOURS_PATH.exists():
        return {"default": {"open": "09:30", "close": "16:00"}, "overrides": {}}
    import json
    try:
        with MARKET_HOURS_PATH.open(encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("default", {"open": "09:30", "close": "16:00"})
        cfg.setdefault("overrides", {})
        return cfg
    except Exception:
        return {"default": {"open": "09:30", "close": "16:00"}, "overrides": {}}


def get_market_hours(ticker: str) -> tuple[time_cls, time_cls]:
    """(open, close) como objetos time para el ticker. Usa override si existe,
    sino el default. Ya NO se ingresa por UI — viene de market_hours.json."""
    cfg = load_market_hours()
    hrs = (cfg.get("overrides", {}) or {}).get(ticker.upper().strip()) or cfg.get("default", {})
    def _parse(s: str, fallback: time_cls) -> time_cls:
        try:
            hh, mm = str(s).split(":")
            return time_cls(int(hh), int(mm))
        except Exception:
            return fallback
    return (_parse(hrs.get("open", "09:30"), time_cls(9, 30)),
            _parse(hrs.get("close", "16:00"), time_cls(16, 0)))


@st.cache_data
def _load_ticker_info_cached(_mtime: float) -> dict:
    if not TICKER_INFO_PATH.exists():
        return {}
    import json
    with TICKER_INFO_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_ticker_info() -> dict:
    # mtime en la key del cache → al editar el JSON (sección Configuración) se re-lee solo.
    try:
        _mt = TICKER_INFO_PATH.stat().st_mtime
    except OSError:
        _mt = 0.0
    return _load_ticker_info_cached(_mt)


def load_api_key() -> str:
    try:
        import config  # type: ignore
        return getattr(config, "POLYGON_API_KEY", "")
    except Exception:
        return ""


@st.cache_resource
def get_downloader(api_key: str) -> Downloader:
    return Downloader(PolygonAdapter(api_key), DATA_DIR)


# --- Modelo de fills (cómo se valúan entrada y salida) — compartido por ambos paneles ---
_FILL_MODE_BAR = "Precio de barra (rápido)"
_FILL_MODE_F1 = "NBBO entrada/salida (Fase 1)"
_FILL_MODE_F2 = "NBBO por barra · triggers sobre el bid (Fase 2)"
_FILL_MODES = [_FILL_MODE_BAR, _FILL_MODE_F1, _FILL_MODE_F2]
_FILL_MODE_HELP = (
    "Cómo se valúan la entrada y la salida:\n\n"
    "• **Precio de barra**: usa el precio de la barra (último trade). Más rápido y optimista.\n\n"
    "• **NBBO entrada/salida (Fase 1)**: pagás el ASK al entrar y cobrás el BID al salir (parche solo "
    "en la salida). Aproximación barata del costo del spread.\n\n"
    "• **NBBO por barra (Fase 2)**: la valuación y TODOS los triggers usan el BID por minuto (lo que "
    "realmente cobrás en cada barra) y la entrada el ASK. El más realista → baja el P&L. La 1ª corrida "
    "por contrato/día baja la línea de quotes (~2 s) y queda cacheada.")


def _fill_flags(mode: str) -> tuple[bool, bool]:
    """(fase1, fase2) según el modo de fills. fase1 = NBBO entrada/salida (parche última fila);
    fase2 = NBBO por barra (serie de bid → valuación y triggers sobre el bid)."""
    return (mode == _FILL_MODE_F1, mode == _FILL_MODE_F2)


TZ = "America/New_York"

# Feriados bursátiles US conocidos (NYSE/CBOE). Cuando se computa el default
# de fecha, si "ayer" cae en weekend o en cualquiera de estas fechas, se
# retrocede al último día hábil con mercado abierto.
_US_MARKET_HOLIDAYS = frozenset({
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03",  # 4 de julio cae sábado, observado viernes
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})


def _last_open_market_day(d: date_cls) -> date_cls:
    """Devuelve `d` si es día hábil de mercado US (no weekend, no feriado).
    Sino retrocede día a día hasta encontrar uno hábil. Útil para defaults
    de fecha cuando hoy/ayer cae fin de semana o feriado."""
    cur = d
    for _ in range(30):  # safety cap (~mes de feriados consecutivos imposibles)
        if cur.weekday() < 5 and cur.isoformat() not in _US_MARKET_HOLIDAYS:
            return cur
        cur = cur - timedelta(days=1)
    return d  # fallback al original si algo extraño pasa


def _non_trading_reason(date_str: str):
    """Si `date_str` (YYYY-MM-DD) NO es día hábil de mercado US, devuelve el motivo
    ('fin de semana' / 'feriado'); si es operable, None. Para AVISAR antes de correr el backtest
    (esas señales se saltean por no tener sesión / 0DTE ese día)."""
    try:
        _d = pd.Timestamp(date_str).date()
    except Exception:
        return None
    if _d.weekday() >= 5:
        return "fin de semana"
    if _d.isoformat() in _US_MARKET_HOLIDAYS:
        return "feriado"
    return None


def _to_ts(date_str: str, t: time_cls) -> pd.Timestamp:
    return pd.Timestamp.combine(pd.Timestamp(date_str).date(), t).tz_localize(TZ)


def _is_session_exhausted(replay: dict) -> bool:
    # Solo el modo single tiene loop de "Próxima iteración". En range/signals (o sin
    # sesión) NO hay próxima iteración → se considera "agotada" (deshabilita ese botón).
    if not replay or replay.get("mode") != "single":
        return True
    if not replay.get("iterations"):
        return False
    last = replay["iterations"][-1]
    if last.exit_reason == "session_end":
        return True
    next_start = last.end_dt + pd.Timedelta(minutes=1)
    return next_start >= replay["day_end_ts"]


def _next_start_ts(replay: dict) -> pd.Timestamp:
    if not replay or not replay.get("iterations"):
        return replay["day_start_ts"]
    return replay["iterations"][-1].end_dt + pd.Timedelta(minutes=1)


def _snap_to_orden_grid(
    ts: pd.Timestamp,
    t_start_in: time_cls,
    t_end_in: time_cls,
    step: int,
) -> tuple[int, int]:
    """Snap un Timestamp al (hora, minuto) válido más cercano FORWARD del
    sidebar 'Hora de orden', respetando step y la ventana [t_start, t_end].

    Snapping forward (>= ts) — nunca antes del start mínimo permitido para
    la próxima iteración.
    """
    h = ts.hour
    m = ts.minute
    valid_hours = list(range(t_start_in.hour, t_end_in.hour + 1))
    if not valid_hours:
        return t_start_in.hour, t_start_in.minute

    # Mover h adelante hacia el rango válido
    if h < valid_hours[0]:
        h, m = valid_hours[0], 0
    elif h > valid_hours[-1]:
        h, m = valid_hours[-1], 59

    def _minute_bounds(hour: int) -> tuple[int, int]:
        if hour == t_start_in.hour and hour == t_end_in.hour:
            return t_start_in.minute, t_end_in.minute
        if hour == t_start_in.hour:
            return t_start_in.minute, 59
        if hour == t_end_in.hour:
            return 0, t_end_in.minute
        return 0, 59

    def _opts_for(hour: int) -> list[int]:
        lo, hi = _minute_bounds(hour)
        opts = [mm for mm in range(0, 60, max(1, step)) if lo <= mm <= hi]
        return opts or [lo]

    # Snap UP dentro de la hora actual
    opts = _opts_for(h)
    higher = [mm for mm in opts if mm >= m]
    if higher:
        return h, higher[0]

    # Sin minuto >= m en esta hora → saltar a la siguiente hora válida
    idx = valid_hours.index(h)
    if idx + 1 <= len(valid_hours) - 1:
        h_next = valid_hours[idx + 1]
        return h_next, _opts_for(h_next)[0]

    # Última hora, sin más opciones → cap al último minuto disponible
    return h, opts[-1]


def _schedule_hora_orden_sync(
    replay_state: dict | None,
    t_start_in: time_cls,
    t_end_in: time_cls,
) -> None:
    """Agenda actualización de Hora/Minuto + reset de Inversión%/$ para el
    próximo rerun, tras completarse una iteración:
    - Hora/Minuto = next_ts EXACTO (forzando step=1 para que el minuto pueda
      ser cualquier valor, ej. 10:34 que no encajaría en step=5).
    - CALL%/PUT% = 50/50 (o 100/0 / 0/100 según el modo single-leg activo)."""
    if not replay_state or replay_state.get("mode") != "single":
        return
    try:
        if _is_session_exhausted(replay_state):
            return
        next_ts = _next_start_ts(replay_state)
        if next_ts >= replay_state["day_end_ts"]:
            return
    except Exception:
        return
    # Tupla (hour, minute, step) — step=1 para que el minuto exacto sea seleccionable.
    st.session_state["_pending_hora_sync"] = (int(next_ts.hour), int(next_ts.minute), 1)
    # Flag para resetear los % de inversión en el próximo render.
    st.session_state["_pending_invest_reset"] = True


def _generate_times(t_start: time_cls, t_end: time_cls, step_minutes: int = 1) -> list[time_cls]:
    """Lista de `time` desde t_start a t_end (inclusive) con paso step_minutes."""
    start_min = t_start.hour * 60 + t_start.minute
    end_min = t_end.hour * 60 + t_end.minute
    if end_min < start_min:
        return [t_start]
    return [time_cls(m // 60, m % 60) for m in range(start_min, end_min + 1, step_minutes)]


def build_chart(res) -> go.Figure:
    df = res.df
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["spot"], name="Subyacente",
        line=dict(width=1.5, color="#1f77b4"), yaxis="y1",
    ))
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["total"], name="Call + Put (total)",
        line=dict(width=2, color="#2ca02c"), yaxis="y2",
    ))
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["pnl_acum"], name="PnL acumulado",
        line=dict(width=1, dash="dot", color="#d62728"), yaxis="y2",
    ))

    # Marcador único: primera vez que la métrica seleccionada >= umbral de salida
    threshold = getattr(res, "exit_threshold_pct", 1.0)
    metric = getattr(res, "exit_metric", "total")
    pct_call = (df["call_px"] - res.call_entry_premium) / res.call_entry_premium if res.call_entry_premium else pd.Series(0.0, index=df.index)
    pct_put = (df["put_px"] - res.put_entry_premium) / res.put_entry_premium if res.put_entry_premium else pd.Series(0.0, index=df.index)
    # ROI real sobre la inversión total.
    _total_inv = getattr(res, "invest_call", 0) + getattr(res, "invest_put", 0)
    if _total_inv > 0:
        pct_total = (getattr(res, "invest_call", 0) * pct_call + getattr(res, "invest_put", 0) * pct_put) / _total_inv
    else:
        pct_total = pct_call + pct_put
    if metric == "call":
        metric_series = pct_call
        over_mask = pct_call >= threshold
    elif metric == "put":
        metric_series = pct_put
        over_mask = pct_put >= threshold
    else:
        metric_series = pct_total
        over_mask = pct_total >= threshold
    if over_mask.any():
        first_idx = over_mask.idxmax()
        fig.add_trace(go.Scatter(
            x=[df.loc[first_idx, "timestamp"]],
            y=[df.loc[first_idx, "total"]],
            mode="markers",
            name=f"Primera vez ≥ {threshold:.0%}",
            marker=dict(
                symbol="star",
                size=18,
                color="#2e7d32",
                line=dict(color="white", width=2),
            ),
            yaxis="y2",
            hovertemplate=(
                f"<b>Primera vez ≥ {threshold:.0%}</b><br>"
                "%{x|%H:%M}<br>"
                "Total prima: $%{y:.2f}<br>"
                "% combinado: %{customdata:+.1%}<extra></extra>"
            ),
            customdata=[metric_series.loc[first_idx]],
        ))

    # Estrella roja: si la iteración terminó por stop loss
    if getattr(res, "exit_reason", "") == "stop_loss":
        last_idx = df.index[-1]
        fig.add_trace(go.Scatter(
            x=[df.loc[last_idx, "timestamp"].to_pydatetime() if hasattr(df.loc[last_idx, "timestamp"], "to_pydatetime") else df.loc[last_idx, "timestamp"]],
            y=[df.loc[last_idx, "total"]],
            mode="markers",
            name=f"Stop loss ≤ {getattr(res, 'stop_loss_pct', -1.0):.0%}",
            marker=dict(
                symbol="star",
                size=18,
                color="#b71c1c",
                line=dict(color="white", width=2),
            ),
            yaxis="y2",
            hovertemplate=(
                f"<b>Stop loss ≤ {getattr(res, 'stop_loss_pct', -1.0):.0%}</b><br>"
                "%{x|%H:%M}<br>"
                "Total prima: $%{y:.2f}<br>"
                "Total %: %{customdata:+.1%}<extra></extra>"
            ),
            customdata=[pct_total.loc[last_idx]],
        ))

    fig.add_hrect(y0=res.premium_min, y1=res.premium_max,
                  fillcolor="lightyellow", opacity=0.20, line_width=0, layer="below",
                  yref="y2")
    # Plotly bug: add_vline with annotation_text fails on datetime/Timestamp x via internal sum().
    # Workaround: draw the line as a shape and add the label as a separate annotation.
    max_x = res.max_total_dt.to_pydatetime() if hasattr(res.max_total_dt, "to_pydatetime") else res.max_total_dt
    min_x = res.min_total_dt.to_pydatetime() if hasattr(res.min_total_dt, "to_pydatetime") else res.min_total_dt
    fig.add_shape(type="line", x0=max_x, x1=max_x, y0=0, y1=1,
                  xref="x", yref="paper",
                  line=dict(color="green", dash="dot"))
    fig.add_shape(type="line", x0=min_x, x1=min_x, y0=0, y1=1,
                  xref="x", yref="paper",
                  line=dict(color="red", dash="dot"))
    fig.add_annotation(x=max_x, y=1.0, xref="x", yref="paper",
                       text="Max", showarrow=False, yanchor="bottom",
                       font=dict(color="green"))
    fig.add_annotation(x=min_x, y=0.0, xref="x", yref="paper",
                       text="Min", showarrow=False, yanchor="top",
                       font=dict(color="red"))
    fig.add_annotation(x=0, y=1.0, xref="paper", yref="paper",
                       text=f"Rango premium objetivo ${res.premium_min:.2f}–${res.premium_max:.2f}",
                       showarrow=False, xanchor="left", yanchor="bottom",
                       font=dict(color="#888"))
    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", y=1.05),
        xaxis=dict(title=""),
        yaxis=dict(title="Spot ($)"),
        yaxis2=dict(title="Prima total / PnL ($)", overlaying="y", side="right"),
    )
    return fig


def render_temporal_distribution(iterations: list, entrada, chart_key: str = "temporal_dist_chart") -> None:
    """Distribución temporal de operaciones EXITOSAS: tiempo desde la apertura hasta
    que se cumplió la condición de salida de la estrategia (exit_reason ==
    "100%_threshold"), agrupado por "Espaciado en minutos" (n) y graficado en barras.
    `iterations` = lista de IterationResult (los None se ignoran). Las que salieron por
    stop_loss o por fin de ventana (timeout) se EXCLUYEN. Sirve para single y rango."""
    _wins = [it for it in iterations
             if it is not None and getattr(it, "exit_reason", "") == "100%_threshold"]
    with st.expander(
        f"⏱️ Distribución temporal de operaciones exitosas ({len(_wins)})",
        expanded=False,
    ):
        if not _wins:
            st.caption(
                "No hubo operaciones que cumplieran la condición de salida de la "
                "estrategia (todas salieron por stop loss o por fin de ventana)."
            )
            return
        _esp = int(st.number_input(
            "Espaciado en minutos", min_value=1, max_value=240, value=15, step=5,
            key="temporal_spacing_min",
            help="Tamaño n de cada intervalo para agrupar el tiempo desde la apertura "
                 "hasta que se cumplió la condición de salida. La gráfica se actualiza "
                 "al cambiarlo.",
        ))
        # Minutos desde el Horario de ENTRADA hasta el cierre de cada operación
        # (hora de cierre - entrada). En rango todas entran a la misma hora, así que
        # esto es el tiempo a salida; en single refleja la hora real del cierre.
        _ent_min = entrada.hour * 60 + entrada.minute
        _mins = [max(0, (it.end_dt.hour * 60 + it.end_dt.minute) - _ent_min)
                 for it in _wins]
        # Bins de tamaño n alineados a la entrada → etiquetas con RANGO DE HORA real.
        def _bink(m):
            return 1 if m <= 0 else (m + _esp - 1) // _esp
        def _clock(total_min):
            h, mm = divmod(int(total_min), 60)
            return f"{h:02d}:{mm:02d}"
        _maxk = max(_bink(m) for m in _mins)
        _labels = [f"{_clock(_ent_min + (k - 1) * _esp)} - {_clock(_ent_min + k * _esp)}"
                   for k in range(1, _maxk + 1)]
        _counts = [0] * _maxk
        for m in _mins:
            _counts[_bink(m) - 1] += 1
        import plotly.graph_objects as _go
        _fig = _go.Figure(_go.Bar(
            x=_labels, y=_counts, marker_color="#2e7d32",
            text=_counts, textposition="outside",
        ))
        _fig.update_layout(
            xaxis=dict(title="Rango de hora de cierre", categoryorder="array",
                       categoryarray=_labels),
            yaxis=dict(title="Operaciones exitosas"),
            height=360, margin=dict(l=10, r=10, t=40, b=10),
            title=f"Distribución temporal — bloques de {_esp} min · {len(_wins)} exitosas",
        )
        st.plotly_chart(_fig, use_container_width=True, key=chart_key)
        _avg = sum(_mins) / len(_mins)
        st.caption(
            f"Cierre promedio: {_clock(_ent_min + round(_avg))} · "
            f"más temprano: {_clock(_ent_min + min(_mins))} · "
            f"más tarde: {_clock(_ent_min + max(_mins))}  "
            f"(desde la entrada {_clock(_ent_min)}; solo operaciones exitosas)"
        )


def render_batch_totals(
    day_runs: list,
    total_days: int,
    title: str = "💼 Totales del backtest",
    show_risk: bool = False,
) -> None:
    """Renderiza el panel de 5 métricas + caption de razones de salida.
    Pensado para ser llamado tanto en vivo durante el batch (con day_runs
    parcial) como en el render final (con day_runs completo). Definida aquí
    arriba porque el batch loop la llama antes del bloque de render.
    `show_risk`=True añade el panel de riesgo/cola + charts (solo en el render
    final; en vivo queda False para no recalcular/parpadear en cada update)."""
    successful = [r for r in day_runs if r.get("iteration") is not None]
    # "Ganancia total" = SUMATORIA de los ROI ($) de todas las iteraciones — es decir,
    # el valor FINAL de la columna "ROI ($) acumulado" de la tabla de días (cum_gain).
    total_invested = sum(r["iteration"].invest_total for r in successful)
    total_gain = sum(r["iteration"].gain_total for r in successful)
    final_capital = total_invested + total_gain
    roi = total_gain / total_invested if total_invested else 0.0
    # Conteo de TODOS los motivos de salida (antes solo contaba 3 → las salidas por ROI colectivo,
    # confirmación o flip "desaparecían" del resumen y el total no cerraba con la cantidad de señales).
    _reason_cnt: dict = {}
    for r in successful:
        _er = r["iteration"].exit_reason
        _reason_cnt[_er] = _reason_cnt.get(_er, 0) + 1
    n_winning = sum(1 for r in successful if r["iteration"].gain_total > 0)
    n_losing = sum(1 for r in successful if r["iteration"].gain_total < 0)
    # Win rate = ganadores / (ganadores + perdedores). Excluye días con
    # ganancia exactamente 0 (neutrales) del denominador.
    _decisive = n_winning + n_losing
    win_rate = (n_winning / _decisive) if _decisive else 0.0

    # En el render FINAL (show_risk=True, nivel superior) los Totales van en un expander
    # EXPANDIDO. En la vista preliminar en vivo (show_risk=False) NO, porque corre dentro
    # del expander "🔬 Backtest de señales / iteraciones" y Streamlit no anida expanders.
    _box = st.expander(title, expanded=True) if show_risk else st
    if not show_risk:
        _box.markdown(f"### {title}")
    tc = _box.columns(6)
    tc[0].metric("Días procesados", f"{len(successful)} / {total_days}")
    tc[1].metric("Inversión total", f"${total_invested:,.2f}")

    if total_gain > 0:
        gain_bg, gain_delta_color, gain_arrow = "rgba(33, 195, 84, 0.1)", "#2e7d32", "▲"
    elif total_gain < 0:
        gain_bg, gain_delta_color, gain_arrow = "#ffcdd2", "#b71c1c", "▼"
    else:
        gain_bg, gain_delta_color, gain_arrow = "#f0f2f6", "#555", "–"
    gain_delta_txt = f"{gain_arrow} {abs(roi):.1%}" if total_invested else ""
    tc[2].markdown(
        f"<div style='border:1px solid rgba(49,51,63,0.2); border-radius:0.5rem; "
        f"padding:0.85rem 1rem; background-color:{gain_bg};'>"
        f"<div style='font-size:0.85rem; font-weight:bold; color:rgba(49,51,63,0.65); "
        f"margin-bottom:0.35rem;'>Ganancia total</div>"
        f"<div style='font-size:1.75rem; font-weight:600; line-height:1.15;'>${total_gain:+,.2f}</div>"
        f"<div style='font-size:0.85rem; color:{gain_delta_color}; margin-top:0.25rem;'>{gain_delta_txt}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    tc[3].metric("Capital final", f"${final_capital:,.2f}")
    tc[4].metric("Días ganadores / perdedores", f"{n_winning} / {n_losing}")
    tc[5].metric(
        "Win rate",
        f"{win_rate:.1%}",
        help=(
            "Días ganadores / (ganadores + perdedores). "
            "Los días con ganancia exactamente 0 se excluyen del denominador."
        ),
    )

    # Base (siempre visible, aunque sea 0) + cualquier otro motivo con conteo > 0 (ROI colectivo,
    # confirmación, flip, overnight). Así el resumen siempre suma la cantidad total de señales.
    _RS_SHORT = {"100%_threshold": "umbral", "stop_loss": "stop loss", "session_end": "cierre de sesión",
                 "collective_roi": "ROI colectivo", "collective_stop": "stop colectivo",
                 "wrong_direction": "dirección equivocada",
                 "weak_confirmation": "confirmación débil", "overnight_1dte": "overnight"}
    _rs_base = ["100%_threshold", "stop_loss", "session_end"]
    _rs_parts = [f"**{_reason_cnt.get(_k, 0)}** {_RS_SHORT[_k]}" for _k in _rs_base]
    _rs_parts += [f"**{_v}** {_RS_SHORT.get(_k, _k)}"
                  for _k, _v in _reason_cnt.items() if _k not in _rs_base and _v > 0]
    _box.caption(
        f"📊 Razones de salida: {' · '.join(_rs_parts)}  ·  "
        f"💹 **Ganancia total** = suma de los ROI ($) de todas las iteraciones"
    )

    if show_risk and len(successful) >= 2:
        _render_risk_panel(successful, key_prefix="risk_batch")


def render_grouped_totals(successful: list, group_of, group_col: str) -> None:
    """Tabla de totales RECALCULADOS por grupo. `group_of(run)` → clave del grupo. Mismas
    métricas que el panel general (días procesados, inversión, ganancia, capital final,
    ganadores/perdedores, win rate), una fila por grupo + una fila TOTAL. Cada grupo refleja
    SOLO sus runs (período o ticker(s) de ese grupo)."""
    _groups: dict = {}
    for _r in successful:
        _groups.setdefault(group_of(_r), []).append(_r)
    _rows = []
    for _k in sorted(_groups, key=str):
        _rs = _groups[_k]
        _inv = sum(x["iteration"].invest_total for x in _rs)
        _gain = sum(x["iteration"].gain_total for x in _rs)
        _nw = sum(1 for x in _rs if x["iteration"].gain_total > 0)
        _nl = sum(1 for x in _rs if x["iteration"].gain_total < 0)
        _dec = _nw + _nl
        _rows.append({
            group_col: _k, "Días proc.": len(_rs),
            "Inversión": _inv, "Ganancia": _gain, "Capital final": _inv + _gain,
            "Ganadores": _nw, "Perdedores": _nl,
            "Win rate %": (_nw / _dec * 100.0) if _dec else 0.0,
        })
    if not _rows:
        return
    # Fila TOTAL (agregado de todos los grupos). Se calcula sobre las filas de grupo.
    _ti = sum(r["Inversión"] for r in _rows)
    _tg = sum(r["Ganancia"] for r in _rows)
    _tw = sum(r["Ganadores"] for r in _rows)
    _tl = sum(r["Perdedores"] for r in _rows)
    _tdp = sum(r["Días proc."] for r in _rows)
    _td = _tw + _tl
    _rows.append({group_col: "— TODOS —", "Días proc.": _tdp,
                  "Inversión": _ti, "Ganancia": _tg, "Capital final": _ti + _tg,
                  "Ganadores": _tw, "Perdedores": _tl,
                  "Win rate %": (_tw / _td * 100.0) if _td else 0.0})
    _df = pd.DataFrame(_rows)

    def _gcol(v):
        if not isinstance(v, (int, float)) or pd.isna(v):
            return ""
        return ("background-color: #c8e6c9" if v > 0
                else ("background-color: #ffcdd2" if v < 0 else ""))

    _styled = (_df.style
               .map(_gcol, subset=["Ganancia"])
               .format({"Inversión": "${:,.0f}", "Ganancia": "${:+,.0f}",
                        "Capital final": "${:,.0f}", "Win rate %": "{:.0f}%"}))
    st.dataframe(_styled, use_container_width=True, hide_index=True,
                 height=min(460, 40 + 35 * max(1, len(_rows))))


def _fecha_of(it) -> str:
    """Fecha (YYYY-MM-DD) de una iteración para ordenar/mostrar en el panel de riesgo."""
    sd = getattr(it, "start_dt", None)
    try:
        return sd.strftime("%Y-%m-%d")
    except Exception:
        return str(sd or "")


def _render_risk_panel(successful: list, key_prefix: str = "risk") -> None:
    """Panel de RIESGO/COLA: lo que el win rate esconde (drawdown, peor día, días a −100%,
    profit factor, Sortino) + curva de equity e histograma de ROI diario. Crítico para
    martingalas. `successful` = day_runs con iteración (no None). `key_prefix` evita colisión
    de keys de plotly cuando el panel de rango y el de señales conviven en un mismo rerun."""
    import numpy as np
    import analytics as _an
    rows = [{"fecha": _fecha_of(r["iteration"]),
             "gain": float(r["iteration"].gain_total),
             "invest": float(r["iteration"].invest_total),
             "roi": (float(r["iteration"].gain_total) / float(r["iteration"].invest_total)
                     if r["iteration"].invest_total else 0.0)}
            for r in successful]
    m = _an.backtest_risk_metrics(rows)
    if m.get("n", 0) < 2:
        return

    _box2 = st.expander("⚠️ Riesgo y cola — lo que el win rate esconde", expanded=False)
    rc = _box2.columns(6)
    _pf = m["profit_factor"]
    rc[0].metric("Profit factor", "∞" if _pf == float("inf") else f"{_pf:.2f}",
                 help="Σ ganancias / Σ pérdidas ($). >1 rentable; <1.3 es frágil. No depende del win rate.")
    rc[1].metric("Max drawdown", f"−${m['max_drawdown']:,.0f}",
                 help=f"Mayor caída pico-a-valle de la P&L acumulada = {m['max_drawdown_x']:.1f}× "
                      f"una apuesta promedio (${m['avg_invest']:,.0f}/día).")
    rc[2].metric("Peor día", f"{m['worst_roi']:+.0%}",
                 help=f"{m['worst_fecha']} · ${m['worst_gain']:+,.0f}. En martingala, el día que se "
                      f"come muchas ganancias chicas.")
    rc[3].metric("Días ≤ −90%", f"{m['n_catastrophic']}",
                 help="Días de (cuasi) ruina. Invisibles en el win rate.")
    rc[4].metric("Racha perdedora", f"{m['max_losing_streak']}",
                 help="Máximo de días perdedores consecutivos.")
    _so = m["sortino"]
    rc[5].metric("Sortino (diario)", "∞" if _so == float("inf") else f"{_so:.2f}",
                 help="Retorno medio / desviación a la baja. Penaliza solo la volatilidad mala. Mayor = mejor.")

    p = m["pcts"]
    # OJO: st.caption usa markdown → los `$` SIN escapar se interpretan como LaTeX y
    # mezclan el texto entre dos signos. Escapamos cada `$` como `\$` (literal).
    _box2.caption(
        f"Expectativa **\\${m['expectancy']:+,.0f}/día** · ROI diario: p5 **{p[5]:+.0%}** · "
        f"mediana **{p[50]:+.0%}** · p95 **{p[95]:+.0%}**  ·  "
        f"Σ ganancias **\\${m['wins_sum']:,.0f}** / Σ pérdidas **\\${m['losses_sum']:,.0f}** · "
        f"Sharpe diario **{m['sharpe']:.2f}**"
    )

    g1, g2 = _box2.columns(2)
    with g1:
        _eq = np.array(m["equity_curve"], dtype=float)
        _peak = np.maximum.accumulate(_eq)
        _x = m["fechas"] if all(m["fechas"]) else list(range(1, m["n"] + 1))
        _fig = go.Figure()
        _fig.add_trace(go.Scatter(x=_x, y=_peak, line=dict(width=0), hoverinfo="skip",
                                  showlegend=False))
        _fig.add_trace(go.Scatter(x=_x, y=_eq, fill="tonexty", name="P&L acum.",
                                  fillcolor="rgba(183,28,28,0.10)",
                                  line=dict(color="#1f77b4", width=2)))
        _fig.add_hline(y=0, line_color="#aaa", line_dash="dot")
        _fig.update_layout(title="Curva de equity (P&L acumulada $) · sombra = drawdown",
                           height=270, margin=dict(l=10, r=10, t=40, b=10), showlegend=False,
                           yaxis_title="P&L $")
        st.plotly_chart(_fig, use_container_width=True, key=f"{key_prefix}_equity")
    with g2:
        _fig2 = go.Figure(go.Histogram(x=[r * 100 for r in m["rois"]], nbinsx=30,
                                       marker_color="#1f77b4"))
        _fig2.add_vline(x=0, line_color="#888", line_dash="dash")
        _fig2.update_layout(title="Distribución de ROI diario (%)", height=270,
                            margin=dict(l=10, r=10, t=40, b=10), bargap=0.05,
                            xaxis_title="ROI %", yaxis_title="días")
        st.plotly_chart(_fig2, use_container_width=True, key=f"{key_prefix}_hist")


# ============================================================================
# App
# ============================================================================
# try/except: standalone funciona normal; dentro del trading_suite (st.navigation)
# set_page_config ya se llamó en el entry → ignoramos el error de doble llamada.
try:
    st.set_page_config(
        page_title="Options Replay",
        layout="wide",
        initial_sidebar_state="expanded",
    )
except Exception:
    pass
st.markdown(
    """
    <style>
    /* En Backtesting el panel izquierdo es algo más ancho que el menú (300px → 400px),
       solo cuando está expandida. Al volver al menú, este CSS no se aplica y la regla
       global (300px) restaura el ancho inicial. */
    section[data-testid="stSidebar"][aria-expanded="true"] {
        min-width: 400px !important;
        max-width: 400px !important;
    }

    /* Pegar el contenido al tope de la sidebar (incluyendo el botón << y el
       header 'Parámetros'). Eliminamos padding-top de los wrappers internos. */
    section[data-testid="stSidebar"] > div:first-child,
    section[data-testid="stSidebar"] [data-testid="stSidebarContent"],
    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"],
    section[data-testid="stSidebar"] .block-container {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }

    /* El header de la sidebar (donde está el botón <<) se aplasta para no
       reservar altura extra. */
    section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
        padding: 0 !important;
        min-height: 0 !important;
        height: auto !important;
    }

    /* En Backtesting esconder el MENÚ de navegación: el sidebar muestra SOLO los
       'Parámetros' (arriba). Se navega de vuelta con el breadcrumb del contenido. */
    section[data-testid="stSidebar"] [data-testid="stSidebarNav"] {
        display: none !important;
    }


    /* Pegar el título 'Options Replay' al extremo superior
       sin solaparlo con el header de Streamlit (que contiene Deploy / menú). */
    .main .block-container,
    [data-testid="stMainBlockContainer"],
    [data-testid="stAppViewContainer"] > .main > .block-container {
        padding-top: 1.5rem !important;
    }
    /* Tamaño del título lo fija el tema global (ui_theme); acá solo lo pegamos al tope. */
    .main h1:first-child {
        margin-top: 0 !important;
        padding-top: 0 !important;
    }


    /* Reducir margenes alrededor de los divisores (---) en la sidebar */
    section[data-testid="stSidebar"] hr {
        margin-top: 0.4rem !important;
        margin-bottom: 0.4rem !important;
    }

    /* Number input: separar +/- a los lados del input.
       Layout final: [-] [ input ] [+]
       Por default Streamlit los stackea juntos a la derecha del input.
       Como el botón "-" está dentro de un wrapper (no es hijo directo del
       contenedor), `order` por sí solo no funciona — usamos position:absolute
       para extraerlo del flow y anclarlo al borde izquierdo del contenedor. */

    div[data-testid="stNumberInputContainer"],
    div[data-testid="stNumberInput"] > div:not([data-testid="stWidgetLabel"]) {
        position: relative !important;
    }

    /* Botón "-" → absolute al borde izquierdo */
    div[data-testid="stNumberInput"] button[data-testid="stNumberInputStepDown"],
    div[data-testid="stNumberInput"] button[kind="stepDown"],
    div[data-testid="stNumberInput"] button[aria-label*="Decrement"] {
        position: absolute !important;
        left: 0 !important;
        top: 0 !important;
        bottom: 0 !important;
        z-index: 10 !important;
        margin: 0 !important;
        border-top-right-radius: 0 !important;
        border-bottom-right-radius: 0 !important;
        border-top-left-radius: 0.25rem !important;
        border-bottom-left-radius: 0.25rem !important;
    }

    /* Botón "+" → queda en su posición default (derecha) */
    div[data-testid="stNumberInput"] button[data-testid="stNumberInputStepUp"],
    div[data-testid="stNumberInput"] button[kind="stepUp"],
    div[data-testid="stNumberInput"] button[aria-label*="Increment"] {
        border-top-left-radius: 0 !important;
        border-bottom-left-radius: 0 !important;
        border-top-right-radius: 0.25rem !important;
        border-bottom-right-radius: 0.25rem !important;
    }

    /* Input: padding izquierdo extra para no superponerse con el "-"
       absolute-positioned, y texto centrado.
       Múltiples selectores porque Streamlit cambia DOM entre versiones. */
    div[data-testid="stNumberInput"] input,
    div[data-testid="stNumberInputContainer"] input,
    div[data-testid="stNumberInput"] [data-baseweb="input"] input,
    div[data-testid="stNumberInput"] [data-baseweb="base-input"] input {
        padding-left: 42px !important;
        text-align: center !important;
        border-radius: 0 !important;
    }

    /* Headers h5 (##### ...) en la sidebar: centrados y con separación
       respecto al label que viene inmediatamente debajo. */
    section[data-testid="stSidebar"] h5 {
        margin-top: 0.3rem !important;
        margin-bottom: 1rem !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
        text-align: center !important;
    }

    /* Reducir el espacio del header principal "Parametros" */
    section[data-testid="stSidebar"] h2 {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("Options Replay")
st.caption(
    "Reproducción minuto a minuto de un Call + Put 0 DTE cuyo premium de apertura "
    "cae dentro del rango definido por el usuario, y entre esos el más cercano a ATM."
)

api_key = load_api_key()
if not api_key:
    st.error(
        "Falta `POLYGON_API_KEY` en `..\\config.py`.  \n"
        "Agregá la línea:  `POLYGON_API_KEY = \"tu_key\"` y refrescá la página."
    )
    st.stop()

# ============================================================================
# 🔬 Backtest de SEÑALES / iteraciones — cada fila = 1 iteración (una señal).
# Se llega acá desde "Alertas → Backtestear señales" (redirige a esta página) o
# cargando iteraciones a mano. Independiente del backtest single/rango de abajo.
# ============================================================================
def _render_sig_results(results, elapsed, partial=False):
    rows, tot, nok, nwin = [], 0.0, 0, 0
    for r in sorted(results, key=lambda x: (x.get("fecha") or "", x.get("hora") or "",
                                            x.get("ticker") or "")):
        it = r.get("iteration")
        if it is not None:
            roi = (it.gain_total / it.invest_total) if it.invest_total else 0.0
            tot += it.gain_total
            nok += 1
            nwin += 1 if it.gain_total > 0 else 0
            _c = r.get("tipo") == "CALL"
            rows.append({
                "Ticker": r["ticker"], "Fecha": r["fecha"], "Hora": r["hora"], "Tipo": r["tipo"],
                "Strike": (it.call_strike if _c else it.put_strike),
                "Prima ent.": (it.call_entry_premium if _c else it.put_entry_premium),
                "Prima sal.": (it.call_exit_premium if _c else it.put_exit_premium),
                "Ganancia": it.gain_total, "ROI %": roi * 100.0,
                "Razón": sbt.REASON.get(it.exit_reason, it.exit_reason),
            })
        else:
            rows.append({
                "Ticker": r.get("ticker", "?"), "Fecha": r.get("fecha", ""),
                "Hora": r.get("hora", ""), "Tipo": r.get("tipo", ""),
                "Strike": None, "Prima ent.": None, "Prima sal.": None,
                "Ganancia": None, "ROI %": None, "Razón": f"⚠ {r.get('error', 'error')}",
            })
    if partial:
        # Totales preliminares EN VIVO — mismo helper que el render final.
        _succ_live = [r for r in results if r.get("iteration") is not None]
        render_batch_totals(_succ_live, total_days=len(results),
                            title="💼 Totales del backtest (preliminar)", show_risk=False)
        if st.session_state.get("sig_coll_exit"):
            st.caption("⏳ _El corte por **ROI colectivo** se aplica al TERMINAR la corrida (necesita "
                       "el timeline completo del día) → recién en el resultado FINAL vas a ver "
                       "«Cierre por ROI colectivo» en los días donde la cartera cruzó el umbral._")
    if not partial:
        _m1, _m2, _m3, _m4 = st.columns(4)
        _m1.metric("Iteraciones", len(results))
        _m2.metric("Con resultado", nok)
        _m3.metric("💲 Ganancia total", f"${tot:,.0f}")
        _m4.metric("Ganadoras", f"{nwin}/{nok}" if nok else "0/0")
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
    if not partial:
        _succ = [r for r in results if r.get("iteration") is not None]
        if len(_succ) >= 2:
            _render_risk_panel(_succ, key_prefix="risk_sig")


# ── Persistencia ANTI-RERUN-TEMPRANO (whitelist) ─────────────────────────────
# El panel de señales renderiza ANTES que los parámetros de sesión de la sidebar; un
# st.rerun() disparado dentro del panel (correr backtest, sembrar config por día, cargar
# plan) aborta el script SIN instanciar los widgets de la sidebar → Streamlit descarta su
# estado y vuelven a defaults (síntoma: «Rango de fechas» perdía las fechas al correr).
# Re-asertar el valor ANTES de cualquier widget lo marca como seteado programáticamente y
# sobrevive al rerun. SOLO claves de la sidebar (WHITELIST): un barrido ciego rompe los
# widgets write-disallowed (botones/uploaders/editores) — el error salta al INSTANCIAR el
# widget, no en la asignación, así que un try/except acá no protege.
_PERSIST_KEYS = ("date_mode_radio", "sel_fecha_unica", "sel_fecha_inicial", "sel_fecha_final",
                 "tickers_select", "straddle_mode_radio", "bt_mode_radio", "horario_entrada",
                 "premium_min_input", "premium_max_input", "exit_plus_pct")
for _pk in _PERSIST_KEYS:
    if _pk in st.session_state:
        st.session_state[_pk] = st.session_state[_pk]

# Señales handed-off desde Alertas (una sola vez): siembran el editor y lo abren.
_handoff = st.session_state.pop("bt_signals_handoff", None)
if _handoff:
    # Llegando desde Alertas → arrancar LIMPIO: borrar los resultados de cualquier backtest
    # previo (manual o de señales) para que la derecha no muestre nada renderizado antes.
    st.session_state.pop("replay", None)
    st.session_state.pop("_confirm_new_sim", None)

    def _norm_hora(h):
        # Alertas de antes de las 09:00 (pre-market) → entrada por defecto a 09:30
        # (apertura), así el backtest 0DTE tiene datos. Las demás quedan igual.
        s = str(h or "").strip()
        try:
            _hh, _mm = s.split(":")[:2]
            if (int(_hh), int(_mm)) < (9, 0):
                return "09:30"
        except Exception:
            pass
        return s
    st.session_state["bt_iters"] = [
        {"Ticker": str(s.get("symbol") or s.get("ticker") or "").upper(),
         "Fecha": str(s.get("fecha") or ""), "Hora": _norm_hora(s.get("hora")),
         "Tipo": str(s.get("tipo") or "").upper(),
         "% Cumpl.": s.get("prob"),
         "Estrategia": str(s.get("estrategia") or ""),
         # Criterio/Fills opcionales (los manda Trading view; Investep no → defaults del panel).
         **({"Criterio": s["criterio"]} if s.get("criterio") else {}),
         **({"Fills": s["fills"]} if s.get("fills") else {})} for s in _handoff]
    st.session_state.pop("bt_iters_editor", None)   # forzar re-seed del data_editor
    st.session_state.pop("_iters_cfg_por_dia", None)  # handoff nuevo → config por día afuera
    # ¿Las señales vienen de TRADING VIEW? (marcador del handoff; heurística TR-UD para
    # exports viejos). Habilita el anti-lookahead del panel — con otro origen se oculta.
    st.session_state["_iters_es_tv"] = any(
        str(s.get("origen") or "") == "tradingview"
        or str(s.get("estrategia") or "").upper().startswith("TR-UD") for s in _handoff)
    st.session_state["_iters_sel_seed"] = True       # nuevo handoff → todas seleccionadas
    st.session_state["bt_iters_open"] = True

_iters_seed = st.session_state.get("bt_iters")   # None / [] si no hay iteraciones cargadas
# El expander se QUEDA ABIERTO mientras haya iteraciones cargadas. Antes "bt_iters_open" era un
# flag de un solo uso (se .pop()-eaba) → en cada rerun (p.ej. al tocar un dropdown) se cerraba solo.
_one_shot = bool(st.session_state.pop("bt_iters_open", False))
_iters_open = (bool(_iters_seed) or _one_shot or bool(st.session_state.get("sig_bt"))
               or bool(st.session_state.get("plan_rango_on")))   # generador rango+criterios abierto


def _has_0dte_on(dl, ticker: str, date: str) -> bool:
    """¿El ticker tiene opción 0DTE (chain que vence ESE día)? El downloader solo cachea
    chains NO vacías → si el archivo existe, hubo 0DTE. Si no está cacheado, consulta (y
    cachea). Ante error, NO saltea (deja que el motor decida)."""
    try:
        if (dl.data_dir / "chain" / f"{ticker}_{date}.parquet").exists():
            return True
        return not dl.chain(ticker, date).empty
    except Exception:  # noqa: BLE001
        return True


_WD_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _venc_label(dl, ticker: str, fecha: str) -> str:
    """Etiqueta del vencimiento del 0DTE/contrato más cercano para (ticker, fecha):
    «mismo día» (0DTE, el contrato vence ESE día), «Vie (+3)» (vence el viernes, +3 días corridos
    desde la señal), «—» si no se puede determinar. Usa nearest_expiry (memoizado en el downloader)."""
    _tk, _fc = str(ticker or "").strip().upper(), str(fecha or "").strip()
    if not _tk or not _fc:
        return ""
    try:
        _exp = dl.nearest_expiry(_tk, _fc)
        _d0 = pd.to_datetime(_fc).normalize()
        _d1 = pd.to_datetime(_exp).normalize()
    except Exception:  # noqa: BLE001
        return "—"
    if _exp is None or pd.isna(_d1) or pd.isna(_d0):
        return "—"
    _n = int((_d1 - _d0).days)
    return "mismo día" if _n <= 0 else f"{_WD_ES[_d1.weekday()]} (+{_n})"


def _venc_label_cached(dl, ticker: str, fecha: str) -> str:
    """Como `_venc_label` pero SIN RED: solo mira la cache local (chain 0DTE en disco → «mismo
    día»; sin dato → «?»). Para seeds GRANDES (p. ej. el puente con un rango largo): nearest_expiry
    por fila = 1 llamada a Polygon por (ticker, fecha) sin cache → la página quedaba «Running…»
    eternamente. El runner resuelve el vencimiento real al correr."""
    _tk, _fc = str(ticker or "").strip().upper(), str(fecha or "").strip()
    if not _tk or not _fc:
        return ""
    try:
        if (dl.data_dir / "chain" / f"{_tk}_{_fc}.parquet").exists():
            return "mismo día"
    except Exception:  # noqa: BLE001
        pass
    return "?"


def _section_rule(text: str) -> None:
    """Encabezado de sección: texto a la izquierda + línea divisoria a la derecha (regla inline).

    Centraliza el HTML que antes se repetía en cada sub-header (DATOS DE ENTRADA / SALIDA)."""
    st.markdown(
        "<div style='display:flex; align-items:center; gap:0.6rem; margin:0.8rem 0 0.3rem 0;'>"
        f"<span style='font-weight:700; white-space:nowrap;'>{text}</span>"
        "<hr style='flex:1; border:none; border-top:1px solid rgba(128,128,128,0.35); margin:0;'>"
        "</div>",
        unsafe_allow_html=True)


def _shift_hhmm(hhmm: str, mins: int) -> str:
    """Suma `mins` a un horario «HH:MM», topeado a las 16:00 (cierre de mercado).

    La señal de la TR-UD-15m se CONFIRMA al CIERRE de la vela de 15m, pero la «Hora» de cada
    fila es la APERTURA de esa vela (Pine/TradingView timestampea las barras por su apertura).
    Entrar en la apertura usaría datos del futuro (el cierre de esa misma vela) → lookahead.
    Desplazar la entrada al cierre (Hora + timeframe) la hace realista."""
    try:
        _h, _m = str(hhmm).strip().split(":")[:2]
        _tot = min(int(_h) * 60 + int(_m) + int(mins), 16 * 60)
        return f"{_tot // 60:02d}:{_tot % 60:02d}"
    except Exception:
        return hhmm


# Tipos de operación válidos POR FILA en el panel de señales (editor + generador por rango).
_TIPO_OPTS = ["CALL", "PUT", "CALL y PUT", "CALL y PUT (Refuerzo)",
              "CALL y PUT (Refuerzo) (End of Day)", "CALL y PUT (plus)",
              "CALL o PUT", "CALL o PUT (plus)", "CALL o PUT (End of Day)",
              "CALL o PUT (Until reach ROI(%))",
              "Sólo CALL (End of Day)", "Sólo PUT (End of Day)"]


@st.cache_data(ttl=600)
def _plan_scenario_configs() -> dict:
    """Condiciones por ID de escenario de la COMBINACIÓN ACTIVA (combinations.db) — la misma
    fuente que usan el batch y el playbook; el template estático ya no participa (quedó migrado
    como la combinación «tpl480»). {} si no hay combinación activa."""
    try:
        import combinations as _cmb
        _cmb.ensure_legacy()
        _act = _cmb.active_combination()
        return _cmb.scenario_configs(_act) if _act else {}
    except Exception:  # noqa: BLE001
        return {}


def _render_iters_panel(_iters_seed):
    # ════════════ 🔁 DATOS DE ITERACIÓN (señales · filtros · tabla editable) ════════════
    with st.container(border=True):
        st.markdown("<h5 style='text-align:center;'>🔁 DATOS DE ITERACIÓN</h5>", unsafe_allow_html=True)
        st.caption(
            "Cada fila = 1 iteración, configurable por fila: **Tipo** = modo (CALL/PUT una pierna · "
            "CALL y PUT · CALL o PUT + variantes 'plus' · Refuerzo; los de dos piernas reparten "
            "50/50) · **Criterio** = selección de contrato (Menor spread en rango óptimo · Primer "
            "contrato cerca de ITM) · **Fills** = modelo de fills ('(default)' usa el selector global de abajo, o "
            "Barra/Fase 1/Fase 2 por fila) · mismo día (sale 16:00). Editá, agregá o borrá filas. "
            "Las señales de **Alertas** llegan acá."
        )
        _seed_df = pd.DataFrame(_iters_seed)
        for _c in ("Ticker", "Fecha", "Hora", "Tipo", "Estrategia"):
            if _c not in _seed_df.columns:
                _seed_df[_c] = ""
        if "% Cumpl." not in _seed_df.columns:
            _seed_df["% Cumpl."] = None
        _seed_df["% Cumpl."] = pd.to_numeric(_seed_df["% Cumpl."], errors="coerce")
        # Tipo = modo del motor. Una pierna = "CALL"/"PUT" (= lo que viene en la alerta, así
        # ese es el DEFAULT). Compat: si quedó "Sólo CALL/PUT" de antes, se mapea a CALL/PUT.
        _TIPO_CI = {t.upper(): t for t in _TIPO_OPTS}   # match case-insensible (el handoff manda en MAYÚS)
        _seed_df["Tipo"] = _seed_df["Tipo"].apply(
            lambda v: _TIPO_CI.get(str(v).strip().upper())
            or {"SÓLO CALL": "CALL", "SOLO CALL": "CALL",
                "SÓLO PUT": "PUT", "SOLO PUT": "PUT"}.get(str(v).strip().upper(), "CALL"))
        # Criterio de selección de contrato POR FILA: Opción 1 (menor spread, default) /
        # Opción 2 (primer contrato cerca de ITM = 1-ITM; ignora spread y rango de prima) /
        # COMPUESTO (Opción 1 completa; si no compra, fallback a la Opción 2 → tier itm_fallback).
        _CRIT_OPTS = ["Menor spread en rango óptimo", "Primer contrato cerca de ITM",
                      "Menor spread en rango óptimo sino Primer contrato cerca de ITM"]
        _CRIT_KEY = {"Menor spread en rango óptimo": "spread",
                     "Primer contrato cerca de ITM": "itm_first",
                     "Menor spread en rango óptimo sino Primer contrato cerca de ITM": "spread_itm_first"}
        if "Criterio" not in _seed_df.columns:
            _seed_df["Criterio"] = _CRIT_OPTS[0]
        _seed_df["Criterio"] = _seed_df["Criterio"].apply(
            lambda v: str(v).strip() if str(v).strip() in _CRIT_OPTS else _CRIT_OPTS[0])
        # Modelo de fills POR FILA. "(default)" = usa el selector global de abajo; o un modo
        # concreto (Barra / Fase 1 / Fase 2) que SOBRESCRIBE el default para esa fila.
        _FILL_ROW_OPTS = ["(default)"] + _FILL_MODES
        if "Fills" not in _seed_df.columns:
            _seed_df["Fills"] = "(default)"
        _seed_df["Fills"] = _seed_df["Fills"].apply(
            lambda v: str(v).strip() if str(v).strip() in _FILL_ROW_OPTS else "(default)")
        # --- 🔎 Filtro de señales: por acción + rango de fechas (desde / hasta). Acota qué
        #     señales se ven en el editor y, por ende, cuáles se backtestean. Vacío = todas. ---
        _all_sig_tks = sorted({str(t).strip() for t in _seed_df["Ticker"] if str(t).strip()})
        _sig_dts_all = pd.to_datetime(_seed_df["Fecha"], errors="coerce")
        _dmin, _dmax = _sig_dts_all.min(), _sig_dts_all.max()
        _hr_uniq = sorted({str(h).strip() for h in _seed_df["Hora"] if str(h).strip()})
        _flt_c1, _flt_c2, _flt_c3, _flt_c4 = st.columns([2, 1, 1, 2])
        _f_tks = _flt_c1.multiselect(
            "🔎 Filtrar por acción", _all_sig_tks, default=[], key="sig_flt_tks",
            help="Vacío = todas las acciones. Elegí una o más para acotar el backtest.")
        _f_from = _flt_c2.date_input("Fecha desde", value=None, key="sig_flt_from",
                                     format="YYYY-MM-DD", help="Vacío = sin límite inferior.")
        _f_to = _flt_c3.date_input("Fecha hasta", value=None, key="sig_flt_to",
                                   format="YYYY-MM-DD", help="Vacío = sin límite superior.")
        # Rango de hora (slider). HH:MM ordena bien como string → comparación directa.
        if len(_hr_uniq) >= 2:
            if st.session_state.get("_sig_flt_hr_opts") != tuple(_hr_uniq):
                st.session_state["_sig_flt_hr_opts"] = tuple(_hr_uniq)
                st.session_state.pop("sig_flt_hr", None)   # re-siembra si cambian las horas disponibles
            _h_lo, _h_hi = _flt_c4.select_slider(
                "🕐 Rango de hora", options=_hr_uniq, value=(_hr_uniq[0], _hr_uniq[-1]),
                key="sig_flt_hr", help="Acota por la hora de la señal (columna Hora).")
        else:
            _flt_c4.caption("🕐 Rango de hora")
            _flt_c4.caption(f"_(única: {_hr_uniq[0]})_" if _hr_uniq else "_(sin horas)_")
            _h_lo = _hr_uniq[0] if _hr_uniq else ""
            _h_hi = _hr_uniq[-1] if _hr_uniq else ""
        _mask = pd.Series(True, index=_seed_df.index)
        if _f_tks:
            _mask &= _seed_df["Ticker"].astype(str).str.strip().isin(_f_tks)
        if _f_from is not None:
            _mask &= _sig_dts_all >= pd.Timestamp(_f_from)
        if _f_to is not None:
            _mask &= _sig_dts_all <= pd.Timestamp(_f_to)
        if _hr_uniq:
            _hora_str = _seed_df["Hora"].astype(str).str.strip()
            _mask &= (_hora_str >= _h_lo) & (_hora_str <= _h_hi)
        _n_total = len(_seed_df)
        _seed_df = _seed_df[_mask].reset_index(drop=True)
        # Orden EN PYTHON (no por header del grid) → la edición de celdas del data_editor sigue
        # funcionando. Ordenar por header + editar NO conviven en st.data_editor (la edición se pierde).
        # Además el orden se basa en los datos ORIGINALES, así que editar una celda no hace saltar la fila.
        _SORT_OPTS = ["(sin ordenar)", "Fecha ↑", "Fecha ↓", "Ticker ↑", "Ticker ↓",
                      "Hora ↑", "Hora ↓", "Tipo ↑", "Tipo ↓"]
        _sort_by = st.selectbox(
            "↕ Ordenar por", _SORT_OPTS, index=0, key="sig_sort_by",
            help="Ordena la tabla al instante, sin romper la edición de celdas. '(sin ordenar)' = orden "
                 "de carga. ↑ ascendente · ↓ descendente.")
        if _sort_by != "(sin ordenar)":
            _scol = _sort_by.rsplit(" ", 1)[0]
            if _scol in _seed_df.columns:
                _seed_df = _seed_df.sort_values(
                    _scol, ascending=_sort_by.endswith("↑"), kind="stable",
                    key=lambda s: s.astype(str)).reset_index(drop=True)
        # Re-siembra el editor cuando cambia el filtro O el orden (descarta el estado viejo del
        # data_editor). NO toca `bt_iters` (no es destructivo).
        _flt_sig = (tuple(_f_tks), str(_f_from), str(_f_to), _h_lo, _h_hi, _sort_by)
        if st.session_state.get("_sig_flt_last") != _flt_sig:
            st.session_state["_sig_flt_last"] = _flt_sig
            st.session_state.pop("bt_iters_editor", None)
        if len(_seed_df) != _n_total:
            _rng = (f" · disponible {_dmin.date()} → {_dmax.date()}"
                    if pd.notna(_dmin) and pd.notna(_dmax) else "")
            st.caption(f"🔎 Filtro activo: **{len(_seed_df)}** de {_n_total} señales{_rng}.")
        if _seed_df.empty:
            st.warning("Ninguna señal cumple el filtro. Ajustá la acción o el rango de fechas.")
        # Columna ✓ (1ª, a la izquierda) para elegir qué filas backtestear. Por defecto TODAS
        # marcadas; los botones marcan/desmarcan todas (re-siembran el editor).
        _bsa, _bsn, _ = st.columns([1.7, 1.7, 5])
        if _bsa.button("☑ Seleccionar todas", use_container_width=True, key="iters_sel_all"):
            st.session_state["_iters_sel_seed"] = True
            st.session_state.pop("bt_iters_editor", None)
            st.rerun()
        if _bsn.button("☐ Quitar todas", use_container_width=True, key="iters_sel_none"):
            st.session_state["_iters_sel_seed"] = False
            st.session_state.pop("bt_iters_editor", None)
            st.rerun()
        _seed_df.insert(0, "✓", bool(st.session_state.get("_iters_sel_seed", True)))
        _seed_df["✓"] = _seed_df["✓"].astype(bool)
        # Columna "Vencimiento" (informativa, no editable): cuándo vence el 0DTE/contrato más cercano de
        # cada fila. Refleja el ticker/fecha del SEED (la alerta), no las ediciones en vivo del editor.
        _dl_venc = get_downloader(api_key)
        # Tope de red: hasta 30 filas se consulta el vencimiento real (memoizado); con más filas
        # (p. ej. puente con rango largo → cientos), SOLO cache local — si no, la página queda
        # «Running…» para siempre resolviendo Polygon fila por fila.
        _VENC_NET_MAX = 30
        if len(_seed_df) <= _VENC_NET_MAX:
            _seed_df["Vencimiento"] = [_venc_label(_dl_venc, _t, _f)
                                       for _t, _f in zip(_seed_df["Ticker"], _seed_df["Fecha"])]
        else:
            _seed_df["Vencimiento"] = [_venc_label_cached(_dl_venc, _t, _f)
                                       for _t, _f in zip(_seed_df["Ticker"], _seed_df["Fecha"])]
            st.caption(f"⚡ **{len(_seed_df)} filas**: el Vencimiento se resolvió solo con la "
                       "cache local (**«?»** = sin dato local, se resuelve al correr) para que el "
                       "panel cargue al instante.")
        # "0 DTE": ✅ si el ticker tenía opción que vence ESE mismo día (derivado del Vencimiento, sin
        # llamada extra). True = «mismo día».
        _seed_df["0 DTE"] = ["✅" if str(_v) == "mismo día" else "" for _v in _seed_df["Vencimiento"]]
        # "dynamic" → la edición de celdas (Tipo/Criterio/Fills) funciona SIEMPRE. El orden se hace en
        # Python con el selector "↕ Ordenar por" de arriba, NO por click en el header: ordenar por header
        # y editar celdas no conviven en st.data_editor (la edición se pierde / la fila salta).
        _num_rows = "dynamic"
        # Centrar los VALORES (text-align en celdas vía Styler; los headers no se pueden
        # centrar — limitación del grid de Glide, igual que en la tabla de resultados).
        # Editor: SOLO Ticker / Fecha / Hora / Tipo (+ el ✓ de selección). Criterio y Fills ya NO se
        # eligen por fila → el run los toma por default: Criterio = «Menor spread en rango óptimo»
        # (_r.get cae a "spread") y Fills = «Modelo de fills (por defecto)» de abajo.
        _ed = st.data_editor(
            _seed_df[["✓", "Ticker", "Fecha", "Hora", "Tipo", "Vencimiento", "0 DTE"]].style.set_properties(
                **{"text-align": "center"}),
            num_rows=_num_rows,
            use_container_width=True, hide_index=True, key="bt_iters_editor",
            column_config={
                "✓": st.column_config.CheckboxColumn(
                    "✓", default=True, help="Marcá las filas a backtestear (todas por defecto)."),
                "Ticker": st.column_config.TextColumn("Ticker", help="Símbolo del subyacente de la señal."),
                "Fecha": st.column_config.TextColumn(
                    "Fecha (YYYY-MM-DD)", help="Día de la señal (formato YYYY-MM-DD)."),
                "Hora": st.column_config.TextColumn(
                    "Hora (HH:MM)", help="Hora de entrada de la señal (HH:MM). Se usa si el «Horario de "
                                         "entrada» es «(hora de la alerta)»."),
                "Tipo": st.column_config.SelectboxColumn(
                    "Tipo de operación", options=_TIPO_OPTS, required=True,
                    help="Estrategia de la operación (CALL, PUT, CALL y PUT, y variantes plus / End of Day / Refuerzo)."),
                "Vencimiento": st.column_config.TextColumn(
                    "Vencimiento", disabled=True,
                    help="Cuándo vence el 0DTE/contrato más cercano del ticker en esa fecha: «mismo día» = 0DTE "
                         "(QQQ/SPY/IWM); «Vie (+3)» = vence el viernes, +3 días corridos (acción sin 0DTE ese "
                         "día). Informativo (no editable); refleja el ticker/fecha de la alerta, no las ediciones."),
                "0 DTE": st.column_config.TextColumn(
                    "0 DTE", disabled=True,
                    help="✅ = el ticker tenía opción 0DTE ese día (vence el mismo día de la señal). "
                         "Vacío = el vencimiento más cercano es posterior (ver «Vencimiento»)."),
            },
        )
        # Info por ticker (debajo de la tabla, colapsada): metadata + rangos + calendario de vencimientos.
        with st.expander("📊 Información de tickers", expanded=False):
            _tks_tbl = sorted({str(_t).strip().upper() for _t in _ed.get("Ticker", []) if str(_t).strip()})
            if not _tks_tbl:
                st.caption("No hay tickers en la tabla.")
            else:
                _tinfo_all = load_ticker_info()
                _exp_by_tk = {}
                try:
                    import ticker_prefs as _tp_info
                    for _, _prr in _tp_info.load().iterrows():
                        _exp_by_tk[str(_prr["ticker"]).strip().upper()] = _prr
                except Exception:
                    pass
                _info_rows = []
                for _tk in _tks_tbl:
                    _v = _tinfo_all.get(_tk, {}) or {}
                    _pr = _exp_by_tk.get(_tk)
                    _info_rows.append({
                        "Ticker": _tk,
                        "Nombre": _v.get("nombre") or "—",
                        "Sector": _v.get("bloque_sector") or "—",
                        "Rango óptimo ($)": _v.get("rango_optimo_text") or "—",
                        "Rango ext ($)": _v.get("min_max_text") or "—",
                        "Lun": (_pr["exp_lun"] if _pr is not None else "—"),
                        "Mar": (_pr["exp_mar"] if _pr is not None else "—"),
                        "Mié": (_pr["exp_mie"] if _pr is not None else "—"),
                        "Jue": (_pr["exp_jue"] if _pr is not None else "—"),
                        "Vie": (_pr["exp_vie"] if _pr is not None else "—"),
                    })
                st.dataframe(pd.DataFrame(_info_rows), hide_index=True, use_container_width=True)
                st.caption("**Rango óptimo / ext** = rangos de prima por contrato (×100) que usa el motor. "
                           "**Lun–Vie** = vencimiento más temprano entrando ese día (base de §3 en Configuración; "
                           "«—» = sin datos / recalculá en §3).")
    # El orden se hace con el selector "↕ Ordenar por" (en Python), para no romper la edición.
    # --- Inversión: total + split por pierna (CALL%/PUT% y CALL$/PUT$, BIDIRECCIONAL) ---
    # El run usa `inversion` (total) + `call_pct`; PUT%, CALL$ y PUT$ son vistas equivalentes
    # del mismo split, sincronizadas entre sí (editás cualquiera y los demás se ajustan).
    st.session_state.setdefault("sig_inv", 1000.0)
    st.session_state.setdefault("sig_call_pct", 50.0)
    st.session_state.setdefault("sig_put_pct", 50.0)
    st.session_state.setdefault("sig_call_dollars", 500.0)
    st.session_state.setdefault("sig_put_dollars", 500.0)

    def _sig_sync_total():
        _t = float(st.session_state["sig_inv"])
        st.session_state["sig_call_dollars"] = (float(st.session_state["sig_call_pct"]) / 100.0) * _t
        st.session_state["sig_put_dollars"] = (float(st.session_state["sig_put_pct"]) / 100.0) * _t

    def _sig_sync_call_pct():
        _t = float(st.session_state["sig_inv"]); _c = float(st.session_state["sig_call_pct"])
        st.session_state["sig_put_pct"] = 100.0 - _c
        st.session_state["sig_call_dollars"] = (_c / 100.0) * _t
        st.session_state["sig_put_dollars"] = ((100.0 - _c) / 100.0) * _t

    def _sig_sync_put_pct():
        _t = float(st.session_state["sig_inv"]); _p = float(st.session_state["sig_put_pct"])
        st.session_state["sig_call_pct"] = 100.0 - _p
        st.session_state["sig_put_dollars"] = (_p / 100.0) * _t
        st.session_state["sig_call_dollars"] = ((100.0 - _p) / 100.0) * _t

    def _sig_sync_call_d():
        _t = float(st.session_state["sig_inv"])
        if _t <= 0:
            return
        _cd = min(float(st.session_state["sig_call_dollars"]), _t)
        st.session_state["sig_call_dollars"] = _cd
        _c = (_cd / _t) * 100.0
        st.session_state["sig_call_pct"] = _c
        st.session_state["sig_put_pct"] = 100.0 - _c
        st.session_state["sig_put_dollars"] = _t - _cd

    def _sig_sync_put_d():
        _t = float(st.session_state["sig_inv"])
        if _t <= 0:
            return
        _pd = min(float(st.session_state["sig_put_dollars"]), _t)
        st.session_state["sig_put_dollars"] = _pd
        _p = (_pd / _t) * 100.0
        st.session_state["sig_put_pct"] = _p
        st.session_state["sig_call_pct"] = 100.0 - _p
        st.session_state["sig_call_dollars"] = _t - _pd

    # ════════════ 📥 DATOS DE ENTRADA (inversión · horario · contrato · fills) ════════════
    with st.container(border=True):
        st.markdown("<h5 style='text-align:center;'>📥 DATOS DE ENTRADA</h5>", unsafe_allow_html=True)
        # ⚙️ Config del análisis: elegir un escenario GANADOR POR DÍA aplica sus CONDICIONES DE
        # SALIDA (leídas del template) a los widgets de abajo — seguro porque esta sección renderiza
        # ANTES que «CONDICIONES DE SALIDA». Solo se aplica al CAMBIAR la selección (después podés
        # retocar a mano sin que se pise).
        _section_rule("Configuración del análisis")
        _cfgs_plan = _plan_scenario_configs()
        # Presets legacy (Lun·Mar·Mié·Jue·Vie del playbook original) — solo si existen en la
        # combinación ACTIVA (otra combinación tiene otros IDs → se ofrecen solo los válidos).
        _CFG_IDS = [i for i in ("C061", "C063", "C041", "C001", "C123") if i in _cfgs_plan]
        _cfg_c1, _cfg_c2 = st.columns([1.2, 2.8])
        _sel_cfg = _cfg_c1.selectbox(
            "Seleccionar configuración",
            ["(manual)", "(playbook automático)"] + _CFG_IDS, key="iters_cfg_sel",
            help="**(playbook automático)**: aplica la config que el PLAYBOOK guardado "
                 "(Configuración → §6) asigna al DÍA DE LA SEMANA de las fechas cargadas — sin "
                 "intervención manual; si el playbook dice NO OPERAR ese día, avisa y no aplica. "
                 "**C0xx**: aplica ese escenario COMPLETO del template (salidas con flags Sí/No + "
                 "refuerzo + sin-lookahead). **«(manual)»** no toca nada. Después de aplicar podés "
                 "retocar cualquier valor a mano.")
        _n_tk_seed = len({str(_r.get("Ticker") or "").strip().upper()
                          for _r in _iters_seed if str(_r.get("Ticker") or "").strip()})
        # Modo AUTOMÁTICO: resolver el escenario por el día de la semana de las filas cargadas.
        # El estado se muestra en CADA rerun (caption/warning persistente); la APLICACIÓN a los
        # widgets solo ocurre al cambiar la firma (selección + fechas del seed).
        _seed_dates = sorted({str(_r.get("Fecha") or "").strip()[:10]
                              for _r in _iters_seed if str(_r.get("Fecha") or "").strip()})
        _auto_entry = None
        _auto_map: dict = {}           # {día: {scenario, cfg}} — días mezclados en modo automático
        if _sel_cfg == "(playbook automático)":
            import playbook_store as _pbs
            _pb_auto = _pbs.load_playbook()
            _wds = set()
            for _f in _seed_dates:
                try:
                    _wds.add(_pbs._WD_ES.get(pd.Timestamp(_f).weekday()))
                except Exception:  # noqa: BLE001
                    pass
            _wds.discard(None)
            if not _pb_auto:
                _cfg_c2.warning("No hay **playbook guardado** — generá uno en **Configuración → "
                                "§6 Playbook** (botón «Reevaluar Playbook»).")
            elif not _seed_dates:
                _cfg_c2.caption("Cargá iteraciones con fecha para que el playbook resuelva el día.")
            elif len(_wds) > 1:
                # Días MEZCLADOS → modo CONFIG POR DÍA: cada fila correrá con las condiciones del
                # escenario de SU día (solo días OPERAR + estado operable; el resto se saltea).
                # ACÁ solo se CALCULA y se muestra; la SIEMBRA en session_state ocurre una única
                # vez por firma (selección+fechas) en el guard de abajo — si no, el botón
                # «✖ Desactivar» del banner no serviría (cada rerun re-sembraría el mapa).
                for _wd_a in sorted(_wds):
                    _e_a = (_pb_auto.get("per_day") or {}).get(_wd_a) or {}
                    if (str(_e_a.get("recommendation") or "").upper() == "OPERAR"
                            and _e_a.get("estado") in (None, "operable") and _e_a.get("config")):
                        _auto_map[_wd_a] = {"scenario": str(_e_a.get("scenario") or ""),
                                            "cfg": _e_a["config"]}
                if _auto_map:
                    # Trazabilidad (clave reservada, NO es un día): de QUÉ combinación salió la
                    # config — viaja con el mapa y se estampa en los resultados de cada corrida.
                    _auto_map["_combo"] = {"combination": _pb_auto.get("combination"),
                                           "nombre": _pbs.display_name(_pb_auto)}
                if not _auto_map:
                    _cfg_c2.warning(f"Las filas mezclan **{len(_wds)} días de semana** "
                                    f"({', '.join(sorted(_wds))}) y NINGUNO está operable en el "
                                    "playbook (o no tiene condiciones) — no se aplicó ninguna "
                                    "config y la corrida usaría las condiciones del panel.")
                elif st.session_state.get("_iters_cfg_por_dia"):
                    _no_op_a = sorted(_wds - set(_auto_map))
                    _cfg_c2.caption("🗓 **Config por día activa** (filas con varios días): "
                                    + " · ".join(f"**{_d}→{_auto_map[_d]['scenario']}**"
                                                 for _d in ("Lun", "Mar", "Mié", "Jue", "Vie")
                                                 if _d in _auto_map)
                                    + (f" — los **{', '.join(_no_op_a)}** son NO OPERAR/no "
                                       "operables: sus filas se SALTEAN al correr." if _no_op_a
                                       else "")
                                    + " Las CONDICIONES DE SALIDA del panel no se usan.")
                else:
                    # El usuario la desactivó (banner «✖ Desactivar») → modo clásico hasta que
                    # la reactive o cambie la firma (selección/fechas).
                    _cfg_c2.caption("🗓 Config por día **desactivada** — TODAS las filas correrán "
                                    "con las CONDICIONES DE SALIDA del panel (incluidos los días "
                                    "no operables del playbook).")
                    if _cfg_c2.button("🗓 Reactivar config por día", key="cfg_dia_on"):
                        st.session_state["_iters_cfg_por_dia"] = _auto_map
                        st.rerun()
            else:
                _auto_entry = _pbs.scenario_for_date(_pb_auto, _seed_dates[0])
                _dia_auto = next(iter(_wds), "?")
                # De QUÉ playbook sale la config: con varias combinaciones conviviendo, el
                # nombre es el desambiguador (el «alcance» de la config puede parecérsele).
                _pb_org = f"🧩 «{_pbs.display_name(_pb_auto)}»"
                if not _auto_entry:
                    _cfg_c2.caption(f"El playbook de {_pb_org} no cubre el día **{_dia_auto}**.")
                elif str(_auto_entry.get("recommendation", "")).upper() != "OPERAR":
                    _cfg_c2.warning(f"📕 Playbook de {_pb_org} ({_pb_auto.get('evaluado_desde')} → "
                                    f"{_pb_auto.get('evaluado_hasta')}): los **{_dia_auto}** son "
                                    f"**NO OPERAR** — no se aplicó ninguna config. "
                                    f"{_auto_entry.get('reason', '')}")
                    _auto_entry = None
                elif _auto_entry.get("estado") not in (None, "operable"):
                    # Máquina de supervivencia: OPERAR pero el estado no es «operable»
                    # (candidato = 1 sola ventana; suspendido = kill-switch/edge decaído) →
                    # el automático NO aplica. Playbooks viejos sin estado siguen aplicando.
                    _est_auto = _auto_entry.get("estado")
                    _cfg_c2.warning(f"📙 Playbook de {_pb_org}: los **{_dia_auto}** pasan el gate "
                                    f"pero el escenario **{_auto_entry.get('scenario')}** está "
                                    f"**{_est_auto}** ({_auto_entry.get('estado_motivo', '')}) — "
                                    "el automático solo aplica estados 🟢 **operable** (gate en "
                                    "las DOS mitades de la ventana). Aplicalo a mano si querés "
                                    "probarlo igual.")
                    _auto_entry = None
                else:
                    _cfg_c2.caption(f"📗 **{_dia_auto} → {_auto_entry.get('scenario')}** · "
                                    f"{_pb_org} (evaluado "
                                    f"{_pb_auto.get('evaluado_desde')} → "
                                    f"{_pb_auto.get('evaluado_hasta')}) · "
                                    f"{_auto_entry.get('config_txt', '')}")
                    if (_pb_auto.get("regimen_vol") or {}).get("alerta"):
                        _cfg_c2.warning("🌊 **Régimen de volatilidad alterado** (vol realizada 5d "
                                        "> 2× la mediana de 60d): la config operable se aplica "
                                        "igual, pero considerá reducir el tamaño hasta que "
                                        "normalice.")
        _cfg_sig = f"{_sel_cfg}|{','.join(_seed_dates)}"
        if _cfg_sig != st.session_state.get("_iters_cfg_applied"):
            st.session_state["_iters_cfg_applied"] = _cfg_sig
            if _sel_cfg == "(playbook automático)":
                if _auto_entry and _auto_entry.get("config"):
                    # Un solo día de semana → aplicación clásica a los widgets (config única).
                    st.session_state.pop("_iters_cfg_por_dia", None)
                    _n_ap = _apply_scenario_config(_auto_entry["config"], _n_tk_seed)
                    st.toast(f"📗 Playbook: {_auto_entry.get('scenario')} aplicado "
                             f"({_n_ap} condición(es))")
                elif _auto_map:
                    # Días mezclados → sembrar el mapa UNA vez por firma (después el usuario
                    # puede desactivarlo con «✖ Desactivar» sin que un rerun lo re-siembre).
                    # El rerun refresca la caption de arriba a «activa» (el guard de firma ya
                    # quedó marcado → no re-entra: sin loop).
                    st.session_state["_iters_cfg_por_dia"] = _auto_map
                    st.rerun()
                else:
                    st.session_state.pop("_iters_cfg_por_dia", None)
            elif _sel_cfg != "(manual)":
                st.session_state.pop("_iters_cfg_por_dia", None)   # C0xx = UNA config por corrida
                _cfg_ap = _cfgs_plan.get(_sel_cfg)
                if not _cfg_ap:
                    _cfg_c2.warning(f"No encontré las condiciones de **{_sel_cfg}**: falta el "
                                    "template en «excels for backtesting».")
                else:
                    _n_ap = _apply_scenario_config(_cfg_ap, _n_tk_seed)
                    st.toast(f"🎛 {_sel_cfg}: {_n_ap} condición(es) aplicadas a "
                             "«CONDICIONES DE SALIDA»")
        _cfg_show = (_cfgs_plan.get(_sel_cfg)
                     if _sel_cfg not in ("(manual)", "(playbook automático)") else None)
        if _cfg_show:
            # Resumen RESPETANDO los flags Sí/No (una condición apagada se ve como «off»).
            import trade_plan as _tplc
            _cfg_c2.caption(f"**{_sel_cfg}** → " + _tplc.scenario_config_summary(_cfg_show))
        _section_rule("Inversión y horario de operación")
        _si1, _si2, _si3, _si4, _si5 = st.columns(5)
        _sig_inv = float(_si1.number_input("Inversión ($)", min_value=1.0, step=100.0,
                                           key="sig_inv", on_change=_sig_sync_total,
                                           help="Monto TOTAL a invertir por iteración. Se reparte entre "
                                                "CALL y PUT según los % de al lado (o 100% a una sola "
                                                "pierna en CALL/PUT solo)."))
        _sig_call_pct = float(_si2.number_input(
            "Inversión en CALL (%)", min_value=0.0, max_value=100.0, step=5.0, key="sig_call_pct",
            on_change=_sig_sync_call_pct,
            help="% de la inversión que va a la pierna CALL; el resto va a la PUT. 50 = 50/50."))
        _si3.number_input("Inversión en PUT (%)", min_value=0.0, max_value=100.0, step=5.0,
                          key="sig_put_pct", on_change=_sig_sync_put_pct,
                          help="% de la inversión a la pierna PUT (= 100 − CALL%).")
        _si4.number_input("Inversión en CALL ($)", min_value=0.0, step=100.0, format="%.2f",
                          key="sig_call_dollars", on_change=_sig_sync_call_d,
                          help="Monto $ a la pierna CALL (autocalculado = Inversión × CALL%).")
        _si5.number_input("Inversión en PUT ($)", min_value=0.0, step=100.0, format="%.2f",
                          key="sig_put_dollars", on_change=_sig_sync_put_d,
                          help="Monto $ a la pierna PUT (autocalculado = Inversión × PUT%).")
        # Horario de operación (entrada / salida).
        _TIMES_SIG = ["09:30", "09:31", "09:32", "09:35", "09:45", "10:00", "10:30", "11:00",
                      "12:00", "13:00", "13:55", "14:00", "15:00", "15:30", "15:45", "16:00"]
        _ENTRY_OPTS = ["(hora de la alerta)"] + _TIMES_SIG
        _se2, _se3 = st.columns(2)
        # Defaults del Data seed del template: entrada 09:30 · salida 13:55.
        _sig_entry_lbl = _se2.selectbox(
            "Horario de entrada", _ENTRY_OPTS, index=_ENTRY_OPTS.index("09:30"), key="sig_entry_lbl",
            help="«(hora de la alerta)» usa la Hora de cada fila. Un horario fijo se aplica a TODAS las señales.")
        _sig_exit_lbl = _se3.selectbox(
            "Horario de salida", _TIMES_SIG, index=_TIMES_SIG.index("13:55"), key="sig_exit_lbl",
            help="Hora de venta (mismo día con DTE=0; día hábil siguiente con DTE=1).")
        _section_rule("Criterio de selección de contratos de opciones")
        # Ventana · Criterio · Modelo de fills — EN UNA SOLA FILA (3 columnas, como Inversión).
        _sc1, _sc2, _sc3 = st.columns(3)
        _sig_search = float(_sc1.number_input(
            "Ventana de búsqueda de contrato (min)", min_value=0.0, max_value=30.0, value=4.0,
            step=1.0, key="sig_search",
            help="Desde el horario de entrada, repregunta «Menor spread en rango óptimo» cada minuto "
                 "hasta encontrar un contrato que pase TODO (espera a que el spread de la subasta de "
                 "09:30 se cierre). Entra en ese momento. 0 = un solo intento."))
        _sig_crit_lbl = _sc2.selectbox(
            "Criterio de selección de contrato", _CRIT_OPTS, index=0, key="sig_crit_global",
            help="Cómo se elige el contrato. «Menor spread en rango óptimo»: el de menor spread en el "
                 "rango (con compuerta de spread). «Primer contrato cerca de ITM»: el primer contrato "
                 "dentro del dinero (1-ITM), ignorando spread y rango de prima. «… sino …» (compuesto): "
                 "intenta el menor spread en rango; si NINGÚN contrato pasa, cae al 1-ITM en vez de no "
                 "comprar (la posición queda marcada como «1-ITM (fallback)»).")
        _sig_fill_default = _sc3.selectbox(
            "Modelo de fills", _FILL_MODES, index=_FILL_MODES.index(_FILL_MODE_F2),
            key="fill_mode_sig", help=_FILL_MODE_HELP)

    with st.container(border=True):
        st.markdown("<h5 style='text-align:center;'>📋 CONDICIONES DE ENTRADA</h5>", unsafe_allow_html=True)
        # 🗓 Config por día activa: refuerzo/DTE/granularidad se OCULTAN y quedan pinneados
        # a las condiciones con las que se EVALUÓ el playbook (refuerzo = el del escenario de
        # cada día vía overrides; DTE = 0 — mismo día; barras de 1 min = 60 s del Data seed).
        # Reaparecen al desactivar el modo o elegir otra config.
        _cfg_dia_ent = bool(st.session_state.get("_iters_cfg_por_dia"))
        if _cfg_dia_ent:
            st.info("🗓 **Config por día activa** — el refuerzo lo define el escenario de cada "
                    "día; **DTE = 0 — mismo día** y **granularidad = 1 min** (las condiciones "
                    "con las que se evaluó el playbook).")
            _sig_apply_ref, _sig_refuerzo, _sig_refuerzo_max = False, 0.50, 2
            _sig_dte, _auto_dte_on = 0, False
        else:
            # ── Refuerzo (martingala) — aplica a CUALQUIER tipo (CALL / PUT / CALL y PUT) si está activo ──
            _sig_apply_ref = st.checkbox(
                "Aplicar refuerzo (martingala) — vale para CALL, PUT o CALL y PUT", value=False,
                key="sig_apply_ref",
                help="Cuando una pierna cae a ≤ −«Umbral de pérdida refuerzo», compra MÁS de ESA misma pierna "
                     "(mismo tipo) con su inversión inicial, hasta «No. de veces a reforzar». Aplica al Tipo de "
                     "CADA fila: CALL refuerza solo el CALL, PUT solo el PUT, CALL y PUT la pierna que más "
                     "pierde. (Las filas «CALL y PUT (Refuerzo)» ya refuerzan siempre, sin este check.)")
            if _sig_apply_ref:
                _sr1, _sr2 = st.columns(2)
                _sig_refuerzo = float(_sr1.number_input(
                    "Umbral pérdida refuerzo (%)", value=50.0, min_value=1.0, max_value=99.0, step=5.0,
                    key="sig_refuerzo",
                    help="Cuando el ROI de una pierna (CALL o PUT) cae a ≤ −este valor, se refuerza esa pierna "
                         "(compra más del mismo tipo con su inversión inicial).")) / 100.0
                _sig_refuerzo_max = int(_sr2.number_input(
                    "No. de veces a reforzar", value=2, min_value=1, max_value=20, step=1, key="sig_refuerzo_max",
                    help="Máximo de refuerzos por iteración (en total, sumando las piernas)."))
            else:
                _sig_refuerzo = 0.50
                _sig_refuerzo_max = 2

            # ── Vencimiento DTE (incluye «Auto-DTE» como 3ª opción del dropdown) ──
            _DTE_AUTO_LBL = ("🗓️ Auto-DTE — si una señal no tiene 0DTE ese día, operar al vencimiento más "
                             "cercano (en vez de saltarla)")
            _DTE_OPTS = ["0 — mismo día", "1 — overnight (D+1)", _DTE_AUTO_LBL]
            _sig_dte_lbl = st.selectbox(
                "Vencimiento DTE", _DTE_OPTS, index=0, key="sig_dte_lbl",
                help="**0 — mismo día**: compra y vende el MISMO día (0DTE). · **1 — overnight (D+1)**: compra "
                     "el día de la señal y vende el día hábil siguiente al Horario de salida. · **🗓️ Auto-DTE**: "
                     "igual que «0 — mismo día», pero las señales SIN opción venciendo ese mismo día (típico en "
                     "acciones fuera de viernes) —que normalmente se SALTEAN— se operan al vencimiento más cercano "
                     "(compra el día de la señal, vende a ese vencimiento). Las que SÍ tienen 0DTE se operan 0DTE "
                     "igual; QQQ/SPY/IWM no cambian (0DTE diario).")
            # Mapeo a las variables que usa el run (mismo comportamiento que el checkbox de antes):
            if _sig_dte_lbl.startswith("1"):
                _sig_dte, _auto_dte_on = 1, False
            elif _sig_dte_lbl == _DTE_AUTO_LBL:
                _sig_dte, _auto_dte_on = 0, True      # = DTE 0 + Auto-DTE ON (como tildar el checkbox)
            else:                                      # "0 — mismo día"
                _sig_dte, _auto_dte_on = 0, False

        # --- Anti-lookahead: la señal de la TR-UD-15m se confirma al CIERRE de la vela de 15m, pero la
        #     «Hora» de cada fila es la APERTURA de esa vela. Entrar en la apertura mira el futuro
        #     (usa el cierre de esa misma vela). ON desplaza la entrada al cierre (Hora + timeframe).
        #     SOLO aplica a señales de TRADING VIEW (el export TR-UD trae la apertura de la vela);
        #     con cualquier otro origen (Investep, plan por rango, manual) la Hora ya es operable →
        #     los widgets se OCULTAN y no participan de la lógica. ---
        if bool(st.session_state.get("_iters_es_tv")):
            _is_alert_hr = (_sig_entry_lbl == "(hora de la alerta)")
            _nl1, _nl2 = st.columns([3, 1])
            _sig_no_lookahead = _nl1.checkbox(
                "⏱️ Entrar al CIERRE de la vela de la señal (sin lookahead)",
                value=False, key="sig_no_lookahead", disabled=not _is_alert_hr,
                help="La señal se CONFIRMA cuando la vela de 15m CIERRA, no en su apertura. La «Hora» de cada "
                     "fila es la APERTURA de esa vela → entrar ahí adelanta la entrada y MIRA EL FUTURO "
                     "(resultados inflados). ON desplaza la entrada al CIERRE de la vela (Hora + timeframe), "
                     "que es lo más temprano que en la realidad podrías operar. Solo con «(hora de la alerta)». "
                     "Visible únicamente con señales de Trading view (TR-UD).")
            _sig_candle_min = int(_nl2.number_input(
                "Timeframe vela (min)", min_value=1, max_value=60, value=15, step=5, key="sig_candle_min",
                disabled=(not _sig_no_lookahead) or (not _is_alert_hr),
                help="Duración de la vela que dispara la señal. TR-UD-15m = 15."))
            _no_lookahead_on = bool(_sig_no_lookahead) and _is_alert_hr
        else:
            _sig_no_lookahead, _sig_candle_min, _no_lookahead_on = False, 15, False

        def _eff_fills(label) -> str:
            _l = str(label or "(default)").strip()
            return _l if _l in _FILL_MODES else _sig_fill_default

        _specs = []
        for _, _r in _ed.iterrows():
            if not bool(_r.get("✓", False)):   # solo las filas MARCADAS
                continue
            _tk = str(_r.get("Ticker") or "").strip()
            if not _tk:
                continue
            _hora_use = (str(_r.get("Hora") or "").strip()
                         if _sig_entry_lbl == "(hora de la alerta)" else _sig_entry_lbl)
            if _no_lookahead_on:   # entrar al CIERRE de la vela de la señal (Hora + timeframe), sin lookahead
                _hora_use = _shift_hhmm(_hora_use, _sig_candle_min)
            _specs.append({"ticker": _tk, "fecha": str(_r.get("Fecha") or "").strip(),
                           "hora": _hora_use,
                           "tipo": str(_r.get("Tipo") or "").upper().strip(),
                           "criterio": _CRIT_KEY.get(_sig_crit_lbl, "spread"),
                           "fills": _eff_fills(_r.get("Fills"))})

        if _cfg_dia_ent:
            _sig_resolution = "1min"          # 60 s del Data seed — lo que respalda el veredicto
        else:
            # Resolución (señales): 30s/15s solo si TODOS los tickers marcados tienen la data fina.
            try:
                import json as _json
                _avail = set(_json.loads((Path(__file__).resolve().parent / "data" /
                             "resolutions_available.json").read_text(encoding="utf-8")).get("tickers", []))
            except Exception:
                _avail = set()
            _sig_res_ok = bool(_specs) and {s["ticker"] for s in _specs}.issubset(_avail)
            _sig_res_opts = ["1 min"] + (["30 seg", "15 seg"] if _sig_res_ok else [])
            if st.session_state.get("sig_res_lbl") not in _sig_res_opts:
                st.session_state.pop("sig_res_lbl", None)
            _sig_res_lbl = st.selectbox(
                "Granularidad temporal de las barras", _sig_res_opts, index=0, key="sig_res_lbl",
                help="30s/15s solo si TODAS las señales marcadas son de tickers con data fina descargada.")
            _sig_resolution = {"1 min": "1min", "30 seg": "30s", "15 seg": "15s"}[_sig_res_lbl]
            if _specs and not _sig_res_ok:
                _noav = sorted({s["ticker"] for s in _specs} - _avail)
                st.caption(f"⏱️ Solo **1 min** disponible — {', '.join(_noav)} sin 30s/15s descargada.")

    # ───────────────────────── CONDICIONES DE SALIDA ─────────────────────────
    with st.container(border=True):
        st.markdown("<h5 style='text-align:center;'>🚪 CONDICIONES DE SALIDA</h5>", unsafe_allow_html=True)
        # 🗓 Config POR DÍA (playbook): cuando está activa, cada fila corre con las condiciones
        # del escenario de SU día de la semana (overrides por fila + colectivo por día) y las
        # condiciones de ESTA sección NO se usan; las filas de días sin config se saltean.
        _cfg_dia_ui = st.session_state.get("_iters_cfg_por_dia") or {}
        if _cfg_dia_ui:
            _bn1, _bn2 = st.columns([5, 1])
            _bn1.info("🗓 **Config por día (playbook) ACTIVA**: "
                      + " · ".join(f"**{_d}→{_cfg_dia_ui[_d].get('scenario', '?')}**"
                                   for _d in ("Lun", "Mar", "Mié", "Jue", "Vie")
                                   if _d in _cfg_dia_ui)
                      + " — cada fila corre con las condiciones (salidas + refuerzo + colectivo) "
                        "del escenario de SU día; **las condiciones de abajo NO se usan** y las "
                        "filas de días no cubiertos se saltean.")
            if _bn2.button("✖ Desactivar", key="cfg_dia_off",
                           help="Vuelve al modo clásico: UNA config (la de abajo) para toda la corrida."):
                st.session_state.pop("_iters_cfg_por_dia", None)
                st.rerun()
            # Pedido UX: con la config POR DÍA activa, los widgets de salida se OCULTAN
            # (no se usan: cada fila corre con los overrides del escenario de SU día y el
            # colectivo va por grupo de día-semana). Estos valores NEUTRALES solo mantienen
            # vivas las referencias del runner (cortocircuitadas por los overrides). Al
            # desactivar —o elegir otra config— los widgets reaparecen con sus defaults.
            _alcance = "Aplicar a tickers y colectivo"
            _tk_on = _col_on = True
            _sig_umb, _sig_stop = 100000.0, -100000.0
            _sig_confirm = _sig_flip = False
            _sig_min_body, _sig_cut_weak = 0.0, True
            _sig_coll = _sig_coll_stop = False
            _sig_coll_thr, _sig_coll_stop_thr = 5.0, -80.0
        else:
            # Default dinámico según el nº de tickers en DATOS DE ITERACIÓN: con 1 solo ticker el
            # colectivo es redundante (la cartera ES ese ticker) → «solo a tickers»; con varios →
            # «a tickers y colectivo». Se re-aplica al cambiar el nº de tickers, sin pisar un cambio
            # manual mientras ese nº no cambie.
            _n_tk = len({s["ticker"] for s in _specs}) if _specs else 0
            _alcance_opts = ["Aplicar a tickers y colectivo", "Aplicar solo a tickers", "Aplicar solo a colectivo"]
            if st.session_state.get("_sig_alcance_ntk") != _n_tk:
                st.session_state["sig_alcance"] = "Aplicar solo a tickers" if _n_tk == 1 else "Aplicar a tickers y colectivo"
                st.session_state["_sig_alcance_ntk"] = _n_tk
            _alcance = st.radio(
                "Alcance de salida", options=_alcance_opts, horizontal=True, key="sig_alcance",
                help="Qué condiciones de salida se APLICAN y se VALIDAN:\n\n"
                     "• **a tickers y colectivo**: ambas secciones activas.\n\n"
                     "• **solo a tickers**: el colectivo se deshabilita y se ignora.\n\n"
                     "• **solo a colectivo**: las condiciones por ticker se deshabilitan y se ignoran.\n\n"
                     "_Default automático: «solo a tickers» con 1 ticker (el colectivo no aplica); "
                     "«a tickers y colectivo» con varios. Podés cambiarlo a mano._")
            _tk_on = _alcance != "Aplicar solo a colectivo"
            _col_on = _alcance != "Aplicar solo a tickers"
            _section_rule("🎯 Aplicar condiciones para tickers")
            # ── Salida POR TICKER (umbral ROI / stop loss) — cada una con su checkbox que la activa ──
            _so1, _so2 = st.columns(2)
            with _so1:
                _sig_apply_umb = st.checkbox(
                    "Cerrar si cumple Umbral ROI (%) del ticker", value=True, key="sig_apply_umb",
                    disabled=not _tk_on,
                    help="Si está activo, cada iteración/pierna cierra en GANANCIA al tocar el Umbral ROI de "
                         "abajo. Si NO, no hay salida por ganancia (corre hasta stop / cierre / otra condición).")
                _sig_umb = float(st.number_input(
                    "Umbral ROI (%) del ticker", value=15.0, step=5.0, key="sig_umb",
                    disabled=not _sig_apply_umb or not _tk_on,
                    help="ROI(%) al que CADA iteración/pierna cierra en GANANCIA (por contrato del ticker, no la "
                         "cartera). Ej: 15 = vende al +15%."))
                if not _sig_apply_umb:
                    _sig_umb = 100000.0   # check OFF → el profit target nunca se alcanza
            with _so2:
                _sig_apply_stop = st.checkbox(
                    "Cerrar si cumple Stop loss (%) del ticker", value=True, key="sig_apply_stop",
                    disabled=not _tk_on,
                    help="Si está activo, cada iteración/pierna CORTA la pérdida al tocar el Stop loss de "
                         "abajo. Si NO, no hay stop (aguanta hasta cierre / otra condición).")
                if float(st.session_state.get("sig_stop", -80.0)) > 0:   # el stop SIEMPRE es ≤ 0
                    st.session_state["sig_stop"] = -abs(float(st.session_state["sig_stop"]))
                _sig_stop = float(st.number_input(
                    "Stop loss (%) del ticker", value=-80.0, step=10.0, key="sig_stop",
                    max_value=0.0, disabled=not _sig_apply_stop or not _tk_on,
                    help="ROI(%) NEGATIVO (≤ 0; el campo no acepta positivos) al que CADA iteración/pierna CORTA "
                         "la pérdida (por contrato del ticker). Ej: −80 = corta al perder 80%; −100 = sin stop efectivo."))
                if not _sig_apply_stop:
                    _sig_stop = -100000.0   # check OFF → el stop nunca se alcanza
            # ── Filtro de confirmación de la 1ª vela (gestión POR TICKER: cierra / da vuelta la pierna) ──
            _cf_left, _cf_right = st.columns(2)
            with _cf_left:
                _sig_conf_mode = st.radio(
                    "Filtro de confirmación de la 1ª vela",
                    options=["No filtrar", "Dar vuelta (flip) si va en contra", "Cerrar si va en contra"],
                    index=1, key="sig_conf_mode", disabled=not _tk_on,
                    help="A los 15 min de la entrada, si la 1ª vela de 15m DESDE la hora de entrada (cualquiera, NO "
                         "solo las 9:30) cerró EN CONTRA de la señal:\n\n"
                         "• **No filtrar**: no hace nada (corre hasta su salida normal / cierre).\n\n"
                         "• **Cerrar si va en contra**: vende ahí (motivo «Señal en sentido del movimiento "
                         "equivocado»).\n\n"
                         "• **Dar vuelta (flip)**: reemplaza la señal por la pierna OPUESTA entrando al cierre de la "
                         "1ª vela (entrada+15m) y la corre hasta el cierre del día (apuesta a que el movimiento "
                         "adverso continúa, ~60%). Validado 4 años: PF 1.62 vs 1.57 de cortar.\n\n"
                         "Solo aplica a iteraciones de UNA pierna (Sólo CALL / Sólo PUT).")
            _sig_confirm = (_sig_conf_mode != "No filtrar")
            _sig_flip = (_sig_conf_mode == "Dar vuelta (flip) si va en contra")
            _sig_min_body = 0.0
            _sig_cut_weak = True
            if _sig_confirm:
                with _cf_right:
                    # ¿TODAS las filas marcadas son modos "End of Day"? Esos corren hasta el cierre por diseño →
                    # la salida por vela doji NO aplica (deshabilitamos el checkbox; el motor igual la exime por fila).
                    _all_eod = bool(_specs) and all(
                        "end of day" in str(_s.get("tipo", "")).lower() for _s in _specs)
                    _sig_cut_weak = st.checkbox(
                        "Cerrar si confirmación débil (vela doji, sin convicción)",
                        value=True, key="sig_cut_weak", disabled=_all_eod,
                        help="Si la 1ª vela es un DOJI (cuerpo dentro de ±anti-doji, sin convicción), corta la "
                             "operación con motivo «Confirmación débil». Destildá para que el doji NO corte (sigue "
                             "hasta su salida normal / cierre del día). Los modos «End of Day» (Sólo CALL/PUT End of "
                             "Day, etc.) la IGNORAN siempre — corren hasta el cierre por diseño.")
                    if _all_eod:
                        st.caption("↳ _Deshabilitado: las operaciones «End of Day» corren hasta el cierre; "
                                   "el doji no las corta._")
                    _sig_min_body = float(st.number_input(
                        "↳ Cuerpo mínimo de la vela de confirmación (%) — anti-doji", min_value=0.0,
                        max_value=1.0, value=0.05, step=0.05, key="sig_min_body",
                        help="Banda muerta de ±este valor. A FAVOR: si el cuerpo no llega al umbral, no confirma "
                             "→ corta al cierre de la 1ª vela (entrada+15m). EN CONTRA: el flip SOLO invierte si el "
                             "movimiento adverso SUPERA el umbral; dentro de ±umbral (ruido tipo −0.01%) corta, no "
                             "invierte. 0 = comportamiento original (flipea con cualquier negativo). Típico ≤0.10; "
                             "subilo para exigir velas adversas más grandes antes de flipear."))
            if not _tk_on:   # «solo colectivo»: las condiciones por ticker NO se aplican ni validan
                _sig_umb, _sig_stop = 100000.0, -100000.0
                _sig_confirm = _sig_flip = False
                _sig_min_body = 0.0
            _section_rule("🌐 Aplicar condiciones para colectivo")
            _cc1, _cc2 = st.columns(2)
            with _cc1:
                _sig_coll = st.checkbox(
                    "Cerrar si cumple Umbral de ROI colectivo (%)", value=True, key="sig_coll_exit",
                    disabled=not _col_on,
                    help="Salida A NIVEL CARTERA: dentro de cada día, cuando el ROI de CARTERA de las posiciones "
                         "abiertas (ganancia $ ÷ invertido $ = el TOTAL en pantalla) alcanza el umbral, vende TODAS "
                         "de golpe (motivo «ROI colectivo»). Las que ya salieron por su umbral/stop no cuentan "
                         "después. Puede dispararse varias veces por día si entran nuevas señales.")
                _sig_coll_thr = float(st.number_input(
                    "Umbral de ROI colectivo (%)", value=5.0, step=1.0, key="sig_coll_thr",
                    disabled=not _sig_coll or not _col_on,
                    help="Cuando el ROI de CARTERA de las posiciones abiertas (ganancia ÷ invertido = el TOTAL) "
                         "≥ este valor, se cierran TODAS en ese minuto, con motivo «ROI colectivo»."))
            with _cc2:
                _sig_coll_stop = st.checkbox(
                    "Cerrar si cumple Stop loss (%) del colectivo", value=False, key="sig_coll_stop",
                    disabled=not _col_on,
                    help="STOP A NIVEL CARTERA — solo con >1 TICKER abierto: cuando el ROI de CARTERA de las "
                         "posiciones abiertas cae a ≤ «Stop loss (%) de colectivo» (la pérdida llega a ese %), "
                         "vende TODAS de golpe (motivo «Stop colectivo»). Con un solo ticker abierto NO aplica "
                         "(manda su stop individual). En la pasada de cartera gana el PRIMER trigger del día (ROI "
                         "colectivo o stop colectivo).")
                if float(st.session_state.get("sig_coll_stop_thr", -80.0)) > 0:   # el stop SIEMPRE es ≤ 0
                    st.session_state["sig_coll_stop_thr"] = -abs(float(st.session_state["sig_coll_stop_thr"]))
                _sig_coll_stop_thr = float(st.number_input(
                    "Stop loss (%) de colectivo", value=-80.0, step=5.0, key="sig_coll_stop_thr",
                    max_value=0.0, disabled=not _sig_coll_stop or not _col_on,
                    help="ROI(%) NEGATIVO de CARTERA (≤ 0; el campo no acepta positivos). Cuando el ROI de las "
                         "abiertas (ganancia ÷ invertido = el TOTAL) ≤ este valor y hay >1 ticker abierto, se "
                         "cierran TODAS (motivo «Stop colectivo»). Ej: −80 = corta si la cartera pierde 80% o más."))

    # Aviso ANTES de correr: señales en días SIN mercado (fin de semana / feriado) → se saltean.
    _nontrading = [(_s.get("ticker", "?"), _s.get("fecha", ""), _r)
                   for _s in _specs if (_r := _non_trading_reason(_s.get("fecha", "")))]
    if _nontrading:
        _nt_txt = " · ".join(f"{_tk} {_fc} ({_rs})" for _tk, _fc, _rs in _nontrading)
        st.warning(f"⚠️ **{len(_nontrading)}** señal(es) caen en un día SIN mercado y se "
                   f"**saltearán** (no hay sesión / 0DTE): {_nt_txt}")
    if st.button(f"▶ Correr backtest de {len(_specs)} iteración(es)", type="primary",
                 disabled=not _specs, key="sig_run"):
        _dl = get_downloader(api_key)
        _dl.resolution = _sig_resolution   # barras a la resolución elegida para esta corrida
        # 🗓 Config por día (playbook): overrides del RUNNER por día de la semana — el espejo puro
        # está en trade_plan.scenario_run_overrides (misma semántica que el batch/map_scenario).
        _cfg_dia_run = st.session_state.get("_iters_cfg_por_dia") or {}
        _ov_by_wd: dict = {}
        _WD_RUN = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

        def _wd_of_fecha(_f):
            try:
                return _WD_RUN[pd.Timestamp(str(_f)).weekday()]
            except Exception:  # noqa: BLE001
                return None
        if _cfg_dia_run:
            import trade_plan as _tplr
            for _wd_r, _e_r in _cfg_dia_run.items():
                if _wd_r == "_combo":
                    continue                       # clave reservada de trazabilidad, no es un día
                try:
                    _ov_by_wd[_wd_r] = {"scenario": str(_e_r.get("scenario") or ""),
                                        **_tplr.scenario_run_overrides(_e_r.get("cfg") or {})}
                except Exception:  # noqa: BLE001 — config ilegible: ese día se saltea
                    pass
        # Solo 0DTE: salteamos las señales SIN 0DTE ese día (no se intentan → no ensucian
        # los resultados con avisos "No 0 DTE option"). Los salteos POR PLAYBOOK (días
        # NO OPERAR / sin config operable) son OTRA categoría: se reportan aparte — antes
        # caían en la misma lista y el panel los mostraba como «sin 0DTE» (falso para
        # tickers con vencimiento diario) con un hint de Auto-DTE que no aplicaba.
        _skipped = []
        _pb_skip = []
        _keep = []
        if _ov_by_wd:
            _pb_skip = [s for s in _specs if _wd_of_fecha(s.get("fecha")) not in _ov_by_wd]
            if _pb_skip:
                st.warning(f"🗓 Config por día: **{len(_pb_skip)}** fila(s) caen en días sin "
                           "config operable del playbook y se **saltean**: "
                           + ", ".join(sorted({f"{s.get('ticker')} {s.get('fecha')}"
                                               for s in _pb_skip})[:12])
                           + ("…" if len(_pb_skip) > 12 else ""))
            _specs = [s for s in _specs if _wd_of_fecha(s.get("fecha")) in _ov_by_wd]
        # Spinner visible: con MUCHAS filas esta pre-pasada consulta chains no cacheadas y sin
        # aviso parecía que la app estaba colgada antes de la barra de progreso.
        with st.spinner(f"Chequeando 0DTE de {len(_specs)} iteración(es)…"):
            for _s in _specs:
                # Con DTE=0 salteamos las señales SIN 0DTE ese día; con DTE=1 o Auto-DTE NO (se
                # opera al vencimiento más cercano, no hace falta 0DTE en la fecha de la señal).
                (_keep if (_sig_dte == 1 or _auto_dte_on
                           or _has_0dte_on(_dl, _s["ticker"], _s["fecha"]))
                 else _skipped).append(_s)
        _specs = _keep
        _n = len(_specs)
        _wk = max(1, min(8, _n)) if _n else 1
        _pr = st.progress(0.0, text="Corriendo iteraciones…")
        _lv = st.empty()
        _t0 = time.perf_counter()
        _res = []
        # Observabilidad del backtest CONCURRENTE (hilos comparten el Downloader): inicio/fin/duración
        # + captura de cualquier excepción de hilo. A options_replay/logs/app.log; consola a WARNING
        # (no ensucia el server de Streamlit). Correlation-id por corrida. NUNCA loguea la api_key.
        import logging as _logging
        from obs_log import get_logger as _get_logger, log_exception as _log_exc, set_correlation_id as _set_cid
        _sig_log = _get_logger("signals", to_file="app.log", level=_logging.WARNING)
        _set_cid(f"sig-{int(time.time())}")
        _sig_log.info("SIGNALS start: %d iteracion(es) · %d hilos · resolución=%s · refuerzo=%s",
                      _n, _wk, _sig_resolution, _sig_refuerzo)
        with ThreadPoolExecutor(max_workers=_wk) as _ex:
            _futs = []
            for _i, s in enumerate(_specs, start=1):
                # flags de fills POR FILA: f1/f2 desde la columna «Fills» (ya resuelta a un
                # modo concreto). entry_at_ask/exit_at_bid = (Fase 1 o Fase 2); nbbo = Fase 2.
                _f1, _f2 = _fill_flags(s.get("fills", _FILL_MODES[0]))
                _ef = _f1 or _f2
                # 🗓 Config por día: las SALIDAS + refuerzo de la fila vienen del escenario de SU
                # día de la semana (el playbook); sin mapa, los valores globales del panel.
                _ov = _ov_by_wd.get(_wd_of_fecha(s.get("fecha"))) if _ov_by_wd else None
                _futs.append(_ex.submit(
                    sbt.run_one, _dl, s, _sig_inv,
                    (_ov["umbral_pct"] if _ov else _sig_umb),
                    (_ov["stop_pct"] if _ov else _sig_stop), None, _i,
                    _ef, _ef, _auto_dte_on, s.get("criterio", "spread"),
                    (_ov["refuerzo_loss_pct"] if _ov else _sig_refuerzo),
                    (_ov["refuerzo_max"] if _ov else _sig_refuerzo_max),
                    call_pct=_sig_call_pct, nbbo_timeline=_f2,
                    search_window_min=_sig_search, dte=_sig_dte, exit_hora=_sig_exit_lbl,
                    confirm_candle=(_ov["confirm_candle"] if _ov else _sig_confirm),
                    confirm_min_body_pct=(_ov["confirm_min_body_pct"] if _ov else _sig_min_body),
                    flip_on_wrong_direction=(_ov["flip_on_wrong_direction"] if _ov else _sig_flip),
                    apply_refuerzo=(_ov["apply_refuerzo"] if _ov else _sig_apply_ref),
                    cut_weak_confirmation=(_ov["cut_weak_confirmation"] if _ov else _sig_cut_weak)))
            _dn = 0
            for _f in as_completed(_futs):
                try:
                    _res.append(_f.result())
                except Exception as _e:
                    _res.append({"ticker": "?", "status": "error", "iteration": None,
                                 "error": str(_e)})
                    _log_exc(_sig_log, "SIGNALS: una iteración lanzó en el hilo (capturada)")
                _dn += 1
                _el = time.perf_counter() - _t0
                _pr.progress(_dn / _n, text=(f"⏱️ {_el:0.1f}s · {_dn}/{_n} iteraciones "
                                             f"({_wk} en paralelo)"))
                with _lv.container():
                    _render_sig_results(_res, _el, partial=True)
        _pr.empty()
        _lv.empty()
        _sig_nerr = sum(1 for _r in _res if _r.get("status") == "error")
        _sig_log.info("SIGNALS end: %d ok · %d error · %.1fs (%d hilos)",
                      len(_res) - _sig_nerr, _sig_nerr, time.perf_counter() - _t0, _wk)
        # Salidas A NIVEL CARTERA (ROI colectivo / Stop colectivo): post-procesan los resultados YA
        # completos en UNA pasada cronológica por día (gana el primer trigger). El STOP colectivo solo
        # dispara con >1 ticker abierto. NO se aplica en los renders parciales (timeline incompleto).
        if _ov_by_wd:
            # 🗓 Config por día: el colectivo de CADA día-semana usa la config de SU escenario
            # (misma pasada cronológica por fecha, solo que en grupos por día de la semana).
            _ncoll_t = 0
            for _wd_c, _ov_c in _ov_by_wd.items():
                _coll_c = _ov_c.get("collective")
                if not _coll_c:
                    continue
                _grp = [r for r in _res if _wd_of_fecha(r.get("fecha")) == _wd_c]
                if not _grp:
                    continue
                try:
                    _ncoll_t += apply_collective_exit(_grp, _coll_c.get("profit_frac"),
                                                      _coll_c.get("stop_frac"),
                                                      stop_require_multi=True)
                except Exception as _ce:  # noqa: BLE001
                    st.warning(f"Salida colectiva ({_wd_c}) no aplicada: {_ce}")
            if _ncoll_t:
                st.toast(f"🟰 Salida colectiva (config por día): {_ncoll_t} posición(es) "
                         "cerradas a nivel cartera")
        else:
            _profit_frac = (_sig_coll_thr / 100.0) if (_sig_coll and _col_on) else None
            _stop_frac = (-abs(_sig_coll_stop_thr) / 100.0) if (_sig_coll_stop and _col_on) else None   # SIEMPRE negativo (guard)
            if _profit_frac is not None or _stop_frac is not None:
                try:
                    _ncoll = apply_collective_exit(_res, _profit_frac, _stop_frac, stop_require_multi=True)
                    if _ncoll:
                        st.toast(f"🟰 Salida colectiva: {_ncoll} posición(es) cerradas a nivel cartera")
                except Exception as _ce:
                    st.warning(f"Salida colectiva no aplicada: {_ce}")
        # Guardar como el "replay" actual (modo señales) → se renderiza RICO más abajo,
        # igual que un backtest manual (Totales + detalle por iteración con render_iteration).
        _combo_run = (_cfg_dia_run or {}).get("_combo") or {}
        st.session_state["replay"] = {
            "mode": "signals", "sig_results": _res, "sig_skipped": _skipped,
            "sig_skipped_playbook": _pb_skip,
            # Trazabilidad: combinación + escenario por día con los que corrió ESTA corrida.
            "sig_pb_combo": ({**_combo_run,
                              "escenarios": {d: (e or {}).get("scenario")
                                             for d, e in _cfg_dia_run.items()
                                             if d != "_combo"}}
                             if _combo_run else None),
            "sig_elapsed": time.perf_counter() - _t0, "sig_workers": _wk,
        }
        st.rerun()


def _apply_scenario_config(cfg: dict, n_tickers: int) -> int:
    """Aplica un ESCENARIO COMPLETO a los widgets del panel vía session_state (seguro: corre
    ANTES de que las secciones instancien sus widgets en este run). El MAPEO es puro y testeado
    en trade_plan.scenario_widget_values — salidas + refuerzo (martingala global: sig_apply_ref/
    sig_refuerzo/sig_refuerzo_max) + sin-lookahead, respetando los flags Sí/No: apagar una
    condición también es aplicar el escenario (C061 apaga umbral y stop del ticker; C063 apaga
    además el ROI colectivo; C123 apaga el refuerzo). Devuelve cuántas claves seteó."""
    import trade_plan as _tplc
    vals = _tplc.scenario_widget_values(cfg, n_tickers)
    for _k, _v in vals.items():
        st.session_state[_k] = _v
    return sum(1 for _k in vals if not _k.startswith("_"))


def _render_range_plan_generator() -> None:
    """📅 Backtest por RANGO dirigido por criterios (Fase 2 — UI). Cuarto PRODUCTOR del contrato
    `bt_iters` (como los handoffs de Alertas/Trading view y el editor manual): arma un TradePlan
    (playbook JSON del análisis / última interpretación de la sesión / matriz manual), lo expande
    con trade_plan.build_iterations sobre el rango elegido y SIEMBRA el panel con el patrón del
    handoff. El runner/render/exports NO cambian: las filas se corren con el MISMO botón."""
    if not st.toggle("📅 Generar iteraciones desde rango + criterios", key="plan_rango_on",
                     help="Arma las filas automáticamente: rango de fechas × veredictos OPERAR/"
                          "NO OPERAR del análisis («Interpretar resultados» → Playbook JSON) o una "
                          "matriz manual por día. Después las corrés con el botón de siempre."):
        return
    import trade_plan as _tpl

    with st.container(border=True):
        _c0, _c1, _c2 = st.columns([1, 1, 2])
        _hoy = date_cls.today()
        # Prefill desde la barra lateral: si ya elegiste rango/tickers en «Parámetros de sesión»,
        # el generador arranca con esos valores (solo el default inicial; después manda el widget).
        _sb_d0 = st.session_state.get("sel_fecha_inicial") or st.session_state.get("sel_fecha_unica")
        _sb_d1 = st.session_state.get("sel_fecha_final") or st.session_state.get("sel_fecha_unica")
        _d0 = _c0.date_input("Desde", value=_sb_d0 or (_hoy - timedelta(days=14)), key="plan_d0")
        _d1 = _c1.date_input("Hasta", value=_sb_d1 or (_hoy - timedelta(days=1)), key="plan_d1")
        _tk_opts = sorted(load_ticker_info().keys()) or ["IWM", "QQQ", "SPY"]
        _sb_tks = [t for t in (st.session_state.get("tickers_select") or []) if t in _tk_opts]
        _tk_def = (_sb_tks or [t for t in ("QQQ", "SPY", "IWM") if t in _tk_opts]
                   or _tk_opts[:1])
        _tks = _c2.multiselect("Tickers", options=_tk_opts, default=_tk_def, key="plan_tks",
                               help="El plan se evalúa por (día, ticker). QQQ/SPY/IWM tienen 0DTE "
                                    "todos los días y data local completa.")

        _what = st.radio("Qué backtestear",
                         ["📅 Plan sintético (1 iteración por día×ticker)",
                          "🔔 Señales históricas de Alertas (filtradas por el criterio)"],
                         horizontal=True, key="plan_what",
                         help="**Plan sintético**: genera una iteración por cada (día hábil × "
                              "ticker) que el criterio habilita, a la hora del plan. **Señales "
                              "históricas**: toma las alertas REALES de la base (página Alertas) "
                              "dentro del rango y deja solo las que el criterio habilita — cada "
                              "señal conserva su hora, tipo y estrategia.")
        _use_sigs = _what.startswith("🔔")
        _pj = None                     # JSON del playbook (None en modo manual)
        _src = st.radio("Fuente del criterio",
                        ["📘 Playbook guardado (incremental)",
                         "📁 Playbook del análisis (JSON)",
                         "🧠 Última interpretación (de esta sesión)", "✍️ Matriz manual"],
                        horizontal=True, key="plan_src",
                        help="**📘 Playbook guardado** = el de Herramientas → Playbook "
                             "(data/playbook.json, se actualiza solo cada mañana): su gate usa la "
                             "máquina de estados — solo días **operables**. Las otras fuentes "
                             "salen de **«Interpretar resultados»** (botón «⬇ Playbook JSON»). "
                             "La matriz manual no requiere análisis previo.")
        if _src.startswith("✍️"):
            _wd_cols = st.columns(5)
            _dias_sel = [_d for _i, _d in enumerate(("Lun", "Mar", "Mié", "Jue", "Vie"))
                         if _wd_cols[_i].checkbox(_d, value=True, key=f"plan_wd_{_i}")]
            _mt1, _mt2 = st.columns(2)
            _tipo = _mt1.selectbox("Tipo de operación", _TIPO_OPTS,
                                   index=_TIPO_OPTS.index("CALL y PUT"), key="plan_tipo")
            _hora = _mt2.text_input("Hora de entrada (HH:MM)", value="09:30", key="plan_hora")
            _plan = _tpl.plan_from_manual(_dias_sel, tipo=_tipo, hora=_hora)
        else:
            _gate = st.checkbox(
                "Usar veredicto día×ticker (gate fino)", value=True, key="plan_gate",
                help="Si el playbook trae `por_ticker`, ese veredicto MANDA sobre el de cartera "
                     "(p. ej. SPY opera el Martes aunque la cartera diga NO OPERAR).")
            if _src.startswith("📘"):
                import playbook_store as _pbs_gen
                _pb_sv = _pbs_gen.load_playbook()
                try:
                    _pj = _tpl.pj_from_saved_playbook(_pb_sv)
                except ValueError as _pe:
                    st.error(str(_pe))
                    return
                _n_oper = sum(1 for _v in _pj.values()
                              if isinstance(_v, dict)
                              and str(_v.get("recommendation")).upper() == "OPERAR")
                _pb_sv_org = _pbs.display_name(_pb_sv)
                st.caption(f"📘 **Playbook guardado** · 🧩 «{_pb_sv_org}» · "
                           f"{_pb_sv.get('modo', 'manual')} · evaluado "
                           f"{_pb_sv.get('evaluado_desde')} → {_pb_sv.get('evaluado_hasta')} · "
                           f"generado {_pb_sv.get('generado_en')} · **{_n_oper}** día(s) operable(s) "
                           "— el gate exige estado 🟢 **operable** (candidato/suspendido quedan "
                           "NO OPERAR, y su gate día×ticker se descarta).")
            elif _src.startswith("📁"):
                _up = st.file_uploader("Playbook JSON del análisis", type=["json"], key="plan_pj_up")
                if _up is None:
                    st.info("Subí el **playbook.json** (o usá la fuente «Última interpretación»).")
                    return
                try:
                    import json as _json
                    _pj = _json.load(_up)
                except Exception as _pe:  # noqa: BLE001
                    st.error(f"El archivo no es un JSON válido: {_pe}")
                    return
            else:
                _pj = st.session_state.get("bta_playbook_json")
                if not _pj:
                    st.info("Todavía no corriste **«Interpretar resultados»** en esta sesión. "
                            "Corré el análisis (arriba, en el modo de la barra lateral) o subí el "
                            "playbook JSON con la otra fuente.")
                    return
            try:
                _plan = _tpl.plan_from_playbook(_pj, use_ticker_gate=_gate)
            except ValueError as _pe:
                st.error(str(_pe))
                return

        try:
            if _use_sigs:
                import signals_db as _sdb
                _sdf = _sdb.load_signals()
                if _sdf is None or _sdf.empty:
                    st.info("La base de señales está vacía — bajalas en **Alertas** («🔄 Bajar "
                            "API») y volvé.")
                    return
                st.caption("Del plan se usa **solo el veredicto** por día(/ticker): cada señal "
                           "conserva su hora (pre-market → 09:30), tipo y estrategia. "
                           "**Tickers vacío = todos.**")
                _res = _tpl.filter_signals(_plan, _sdf.to_dict("records"), _d0, _d1,
                                           tickers=_tks, non_trading_reason=_non_trading_reason)
            else:
                _res = _tpl.build_iterations(_plan, _d0, _d1, _tks,
                                             non_trading_reason=_non_trading_reason)
        except ValueError as _ve:
            st.error(str(_ve))
            return

        def _chip(_d):
            _r = _plan.days[_d]
            _g = (_plan.ticker_gate or {}).get(_d) or {}
            _dif = [f"{_t} {'✅' if _rr.operar else '❌'}" for _t, _rr in sorted(_g.items())
                    if _rr.operar != _r.operar]
            return (f"**{_d}** {'✅' if _r.operar else '❌'}"
                    + (f" _{_r.scenario}_" if _r.scenario else "")
                    + (f" ({', '.join(_dif)})" if _dif else ""))
        st.markdown("🗓️ " + " · ".join(_chip(_d) for _d in ("Lun", "Mar", "Mié", "Jue", "Vie")))

        # ⚙️ Config por día: QUÉ escenario le toca a cada día (el rentable/robusto del análisis),
        # POR QUÉ (métricas) y sus CONDICIONES de salida (si el análisis corrió con template).
        _EN_BY_ES = {"Lun": "Monday", "Mar": "Tuesday", "Mié": "Wednesday",
                     "Jue": "Thursday", "Vie": "Friday"}
        _apply_cfg = None
        _load_cfg_dia = None           # {día: {scenario, cfg}} si el modo «config por día» está ON
        if _pj:
            _cfg_rows, _has_cfg = [], False
            def _cond_cell(_cfg, _fk, _vk):
                """Valor si la condición está ON; «off» si el flag Sí/No la apaga; «—» sin config."""
                if not _cfg:
                    return "—"
                _on = _tpl.flag_on(_cfg.get(_fk), default=True)
                _v = _cfg.get(_vk)
                return _v if (_on and _v is not None) else "off"

            for _d, _en in _EN_BY_ES.items():
                _i = _pj.get(_en) or {}
                if not _i:
                    continue
                _cfg = _i.get("config") or {}
                _has_cfg = _has_cfg or bool(_cfg)
                _cfg_rows.append({
                    "Día": _d, "Escenario": _i.get("scenario"),
                    "Veredicto": _i.get("recommendation"),
                    "WR %": _i.get("win_rate"), "ROI %": _i.get("expected_roi"),
                    "Sharpe": _i.get("sharpe"), "n": _i.get("n"),
                    "Alcance": _cfg.get("alcance", "—"),
                    "Umbral ticker %": _cond_cell(_cfg, "ticker_roi_on", "ticker_roi"),
                    "Stop ticker %": _cond_cell(_cfg, "ticker_stop_on", "ticker_stop"),
                    "ROI colect. %": _cond_cell(_cfg, "col_roi_on", "col_roi"),
                    "Stop colect. %": _cond_cell(_cfg, "col_stop_on", "col_stop"),
                    "Confirmación": _cfg.get("filtro_confirmacion", "—"),
                    "Refuerzo": _cfg.get("refuerzo", "—"),
                })
            if _cfg_rows:
                st.markdown("**⚙️ Config por día** — el escenario que le toca a cada día y sus "
                            "condiciones de salida:")
                st.dataframe(pd.DataFrame(_cfg_rows), use_container_width=True, hide_index=True)
                if not _has_cfg:
                    st.caption("ℹ️ Solo se ve el **ID**: el análisis corrió **sin el template**. "
                               "En «Interpretar resultados» subí también el template y bajá de "
                               "nuevo el Playbook JSON para ver acá las condiciones de cada "
                               "escenario.")
            # 🎛 Config del escenario: por día (Fase 4) o una sola por corrida.
            _scen_cfgs: dict = {}
            _cfg_dia_map: dict = {}
            for _d, _en in _EN_BY_ES.items():
                _i = _pj.get(_en) or {}
                if (str(_i.get("recommendation") or "").upper() == "OPERAR"
                        and _i.get("config") and _i.get("scenario")):
                    _e = _scen_cfgs.setdefault(str(_i["scenario"]), {"cfg": _i["config"], "dias": []})
                    _e["dias"].append(_d)
                    _cfg_dia_map[_d] = {"scenario": str(_i["scenario"]), "cfg": _i["config"]}
            _n_cfgs_dist = len({tuple(sorted((k, str(v)) for k, v in _m["cfg"].items()))
                                for _m in _scen_cfgs.values()})
            if _cfg_dia_map and st.checkbox(
                    "🗓 **Config por día** — cada fila corre con las condiciones del escenario "
                    "de SU día de la semana (una sola corrida)",
                    value=(_n_cfgs_dist > 1), key="plan_cfg_dia",
                    help="Fase 4: al correr, cada fila usa las salidas + refuerzo del escenario "
                         "que el playbook asigna a su día (Mié→C041, Jue→C102, …), incluida la "
                         "salida COLECTIVA por día. Las «CONDICIONES DE SALIDA» del panel se "
                         "ignoran mientras esté activa. Los días sin config operable se saltean. "
                         "Desactivalo para aplicar UNA sola config a toda la corrida (selector "
                         "de abajo)."):
                _load_cfg_dia = _cfg_dia_map
                st.caption("🗓 Al cargar: " + " · ".join(
                    f"**{_d}→{_cfg_dia_map[_d]['scenario']}**"
                    for _d in ("Lun", "Mar", "Mié", "Jue", "Vie") if _d in _cfg_dia_map)
                    + " — las condiciones de salida del panel NO se usarán en esta corrida.")
            elif _scen_cfgs:
                # Escenarios con la MISMA config → una sola opción (sus IDs difieren en columnas
                # que el panel no re-aplica). Con una única config, el checkbox arranca activado.
                _by_cfg: dict = {}
                for _sc, _v in _scen_cfgs.items():
                    _kk = tuple(sorted((_ck, str(_cv)) for _ck, _cv in _v["cfg"].items()))
                    _m = _by_cfg.setdefault(_kk, {"ids": [], "cfg": _v["cfg"], "dias": []})
                    _m["ids"].append(_sc)
                    _m["dias"].extend(_v["dias"])
                _opt_map = {f"{'+'.join(_m['ids'])} ({', '.join(_m['dias'])})": _m["cfg"]
                            for _m in _by_cfg.values()}
                _ap1, _ap2 = st.columns(2)
                _apply_on = _ap1.checkbox(
                    "🎛 Al cargar, aplicar al panel las condiciones del escenario:",
                    value=(len(_opt_map) == 1), key="plan_apply_cfg",
                    help="Aplica el escenario COMPLETO al panel: salidas con sus flags Sí/No "
                         "(Alcance, Umbral/Stop del ticker, ROI/Stop colectivo, confirmación) + "
                         "**refuerzo** (martingala: checkbox/umbral/veces) + sin-lookahead. Las "
                         "condiciones aplican a TODA la corrida (una config por corrida); para "
                         "validar otro escenario, cargá de nuevo con ese elegido.")
                _sel = _ap2.selectbox("Escenario a aplicar", list(_opt_map), key="plan_cfg_scen",
                                      disabled=not _apply_on, label_visibility="collapsed")
                if _apply_on and _sel:
                    _apply_cfg = _opt_map[_sel]

        if pd.Timestamp(_d1) > pd.Timestamp(_hoy):
            st.warning("El rango incluye **fechas futuras** — todavía no hay datos de mercado; "
                       "esos días saldrán como error/salteados al correr.")

        for _w in _res.warnings:
            st.warning(_w)
        _s = _res.summary
        _m0, _m1, _m2, _m3 = st.columns(4)
        _m0.metric("Iteraciones", _s["generadas"])
        _m1.metric("Descartes por criterio", _s["criterio"])
        _m2.metric("Sin sesión", _s["sin_sesion"])
        if _use_sigs:
            _m3.metric("Señales en rango", _s.get("en_rango", 0),
                       help=f"De {_s.get('señales_total', 0)} señal(es) en la base.")
            if _s.get("otros_tickers") or _s.get("invalidas"):
                st.caption(f"ℹ️ No candidatas: **{_s.get('otros_tickers', 0)}** de otros tickers · "
                           f"**{_s.get('invalidas', 0)}** inválida(s) (sin ticker/fecha).")
        else:
            _m3.metric("Días hábiles", _s.get("dias_habiles", 0))
        _t_ok, _t_no = st.tabs([f"✅ Iteraciones ({len(_res.rows)})",
                                f"🗑️ Descartes ({len(_res.discarded)})"])
        with _t_ok:
            if _res.rows:
                st.dataframe(pd.DataFrame(_res.rows), use_container_width=True, hide_index=True,
                             height=min(320, 38 + 35 * len(_res.rows)))
            else:
                st.caption("El criterio no habilita ninguna iteración en ese rango.")
        with _t_no:
            if _res.discarded:
                st.dataframe(pd.DataFrame(_res.discarded), use_container_width=True,
                             hide_index=True, height=min(320, 38 + 35 * len(_res.discarded)))
            else:
                st.caption("Sin descartes.")
        st.caption("El chequeo de **0DTE** lo hace el runner al correr (columna «Vencimiento» del "
                   "panel). **Cargar reemplaza** las filas actuales del panel.")
        if st.button(f"📥 Cargar {len(_res.rows)} iteración(es) al panel", type="primary",
                     disabled=not _res.rows, key="plan_load"):
            # Mismo patrón que el handoff de Alertas: sembrar + re-seed del editor + abrir panel.
            # El aviso va en un flag de sesión (banner tras el rerun): un st.toast acá se PIERDE
            # con el st.rerun() inmediato — por eso parecía que el botón «no hacía nada».
            st.session_state.pop("replay", None)
            _msg = (f"✅ **{len(_res.rows)} iteración(es) del plan cargadas** abajo en "
                    "«🔁 DATOS DE ITERACIÓN» (reemplazaron las filas anteriores).")
            if _load_cfg_dia:
                st.session_state["_iters_cfg_por_dia"] = _load_cfg_dia
                _msg += (" 🗓 **Config por día ACTIVA**: "
                         + " · ".join(f"{_d}→{_load_cfg_dia[_d]['scenario']}"
                                      for _d in ("Lun", "Mar", "Mié", "Jue", "Vie")
                                      if _d in _load_cfg_dia)
                         + " (las CONDICIONES DE SALIDA del panel no se usan en esta corrida).")
            else:
                st.session_state.pop("_iters_cfg_por_dia", None)
                if _apply_cfg:
                    _n_ap = _apply_scenario_config(_apply_cfg, len({r["Ticker"] for r in _res.rows}))
                    _msg += (f" 🎛 {_n_ap} condición(es) del escenario aplicadas a "
                             "«🚪 CONDICIONES DE SALIDA».")
            st.session_state["_plan_loaded_msg"] = _msg
            st.session_state.pop("_iters_es_tv", None)        # el plan por rango ≠ Trading view
            st.session_state["bt_iters"] = _res.rows
            st.session_state.pop("bt_iters_editor", None)
            st.session_state["_iters_sel_seed"] = True
            st.session_state["bt_iters_open"] = True
            st.rerun()


with st.expander("🔬 Backtest de señales / iteraciones", expanded=_iters_open):
    # Banner one-shot tras «📥 Cargar…» del generador (el toast no sobrevive al rerun).
    _plan_msg = st.session_state.pop("_plan_loaded_msg", None)
    if _plan_msg:
        st.success(_plan_msg + " Revisá/ajustá las filas y las condiciones, y tocá "
                   "**▶ Correr backtest** (al final del panel).")
    _render_range_plan_generator()
    if not _iters_seed:
        st.info("No hay iteraciones cargadas. Seleccioná señales en **Alertas** y tocá "
                "**Backtestear** para traerlas acá, o empezá una manualmente abajo.")
        if st.button("➕ Empezar una iteración manual", key="iters_manual_start"):
            st.session_state.pop("_iters_cfg_por_dia", None)
            st.session_state.pop("_iters_es_tv", None)
            st.session_state["bt_iters"] = [{"Ticker": "", "Fecha": "", "Hora": "", "Tipo": "CALL"}]
            st.session_state["_iters_sel_seed"] = True
            st.session_state.pop("bt_iters_editor", None)
            st.rerun()
    else:
        _render_iters_panel(_iters_seed)


# ----- Sidebar form -----
# Breadcrumb EN EL LOGO: "SignalForge \ Backtesting" — se sobreescribe el logo global solo
# en esta página (st.logo, última llamada gana). Se quita el botón "Menú".
_LOGO_BT_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="30">'
    '<text x="0" y="23" font-family="sans-serif" font-size="22" font-weight="800">'
    '<tspan fill="#1f2937">Signal</tspan><tspan fill="#16a34a">Forge</tspan>'
    '<tspan fill="#9ca3af" font-weight="600"> \\ Backtesting</tspan></text></svg>')
st.logo(_LOGO_BT_SVG)

replay_state = st.session_state.get("replay")
has_session = replay_state is not None
_is_range_mode = has_session and replay_state.get("mode") == "range"
# En range mode la "Próxima iteración" no aplica → tratamos la sesión como agotada.
session_exhausted = has_session and (
    _is_range_mode or _is_session_exhausted(replay_state)
)

# Cargar tickers desde ticker_info.json y construir el listado del dropdown.
_all_tickers_info = load_ticker_info()


@st.cache_data(ttl=30)
def _cached_tickers_set() -> set[str]:
    """Set de tickers con al menos un archivo chain cacheado en data/chain/.
    TTL 30s para que se refresque cuando termine un prefetch en background."""
    chain_dir = DATA_DIR / "chain"
    if not chain_dir.exists():
        return set()
    return {p.stem.split("_")[0] for p in chain_dir.glob("*.parquet")}


_cached_set = _cached_tickers_set()

# Curado por tipo de subyacente (estable, cubre tickers sin cache o con cache
# incompleto). Verificado contra la data de Polygon:
#   - ETFs/índices: 0DTE TODOS los días hábiles (Lun-Vie) → "daily".
#   - Mega-caps: 0DTE solo Lun/Mié/Vie (NO Mar/Jue) → "mwf".
#   - El resto: solo weekly (viernes).
# Clasificación verificada contra Polygon (probe sobre 3 martes + 3 miércoles):
#   - DAILY: SPY/QQQ/IWM/SPX (del watchlist) + XSP/NDX/VIX/RUT (índices daily conocidos).
#     OJO: DIA NO es daily (es weekly) — sorpresa confirmada.
#   - MWF (Lun/Mié/Vie): mega-caps AAPL/AMZN/AVGO/META/MSFT/NVDA/TSLA + commodities
#     GLD/SLV/USO. OJO: GOOG/GOOGL NO son mwf (son weekly).
_DAILY_INDEX = {"SPY", "QQQ", "IWM", "SPX", "XSP", "NDX", "VIX", "RUT"}
_MWF_STOCK = {"AAPL", "AMZN", "AVGO", "GLD", "META", "MSFT", "NVDA", "SLV", "TSLA", "USO"}


@st.cache_data(ttl=60)
def _zerodte_tiers() -> dict[str, str]:
    """Mapa {ticker: 'daily'|'mwf'} indicando en qué días vence el mismo día (0DTE).
    Detección data-driven por nombre de archivo de cache (el nombre
    `{tk}_{fecha}.parquet` coincide siempre con el expiry interno: la cache sólo
    guarda chains 0DTE), unida con las listas curadas.
      - 'daily': hay 0DTE en Mar Y Jue (≥3 c/u) → todos los días hábiles.
      - 'mwf'  : hay 0DTE en Lun Y Mié (≥3 c/u) pero NO daily → Lun/Mié/Vie.
    Los Mar:1/Jue:1 sueltos de los stocks son feriados corridos → se filtran con
    el umbral de 3."""
    by_wd: dict[str, dict[int, int]] = {}
    chain_dir = DATA_DIR / "chain"
    if chain_dir.exists():
        for p in chain_dir.glob("*.parquet"):
            parts = p.stem.split("_", 1)
            if len(parts) != 2:
                continue
            tk, date_str = parts
            try:
                wd = pd.Timestamp(date_str).weekday()
            except Exception:
                continue
            if wd > 4:
                continue
            by_wd.setdefault(tk, {}).setdefault(wd, 0)
            by_wd[tk][wd] += 1

    tiers: dict[str, str] = {}
    for tk, wd in by_wd.items():  # 1) data-driven
        tue, thu = wd.get(1, 0), wd.get(3, 0)
        mon, wed = wd.get(0, 0), wd.get(2, 0)
        if tue >= 3 and thu >= 3:
            tiers[tk] = "daily"
        elif mon >= 3 and wed >= 3:
            tiers[tk] = "mwf"
    for tk in _DAILY_INDEX:   # 2) curado (no degrada un 'daily' detectado)
        tiers[tk] = "daily"
    for tk in _MWF_STOCK:
        tiers.setdefault(tk, "mwf")
    return tiers


_zerodte_map = _zerodte_tiers()

# Ordenar tickers: primero los cacheados (alfabético), después los no cacheados
# (alfabético). El usuario ve arriba los que tienen data lista.
TICKER_OPTIONS = (
    sorted(_all_tickers_info.keys(), key=lambda t: (t not in _cached_set, t))
    if _all_tickers_info else []
)


def _defaults_for(t: str) -> tuple[float, float]:
    """Defaults de Rango óptimo Min/Max desde el JSON, dividiendo por 100
    (los valores del archivo están en 'centavos' de premium, e.g. SPY $30-$45
    representa $0.30-$0.45)."""
    info = _all_tickers_info.get(t) or {}
    lo = info.get("rango_optimo_lo")
    hi = info.get("rango_optimo_hi")
    if lo is not None and hi is not None:
        return (float(lo) / 100.0, float(hi) / 100.0)
    return (0.30, 0.50)


# Ticker selector — fuera del form para que cambiar de ticker
# refresque los defaults del rango premium en el mismo render.
# Default: QQQ si está cacheado; sino primer cacheado; sino 0.
if "QQQ" in TICKER_OPTIONS and "QQQ" in _cached_set:
    _default_idx = TICKER_OPTIONS.index("QQQ")
else:
    _default_idx = 0  # primer item (cacheado si hay alguno por el sort)


def _on_ticker_change():
    """Callback que corre cuando el usuario cambia el ticker en el selectbox.
    Actualiza los inputs de Rango óptimo Min/Max con los defaults del nuevo
    ticker desde el JSON."""
    new_ticker = st.session_state.get("ticker_select")
    if new_ticker:
        lo, hi = _defaults_for(new_ticker)
        st.session_state["premium_min_input"] = lo
        st.session_state["premium_max_input"] = hi


def _ticker_label(t: str) -> str:
    """Label del dropdown:
    - 🟢 = 0DTE todos los días (daily)  ·  🟡 = 0DTE solo Lun/Mié/Vie
    - 💾 = cacheado (data local) · ☁️ = sin cache (se baja de Polygon)
    Ej: `🟢 💾 SPY — …` · `🟡 ☁️ MSFT — …` · `⚪ 💾 SOXL — …` (weekly)
    """
    info = _all_tickers_info.get(t) or {}
    nombre = info.get("nombre")
    tier = _zerodte_map.get(t)
    # 🟢 daily · 🟡 Lun/Mié/Vie · ⚪ weekly (solo viernes / el resto).
    mark = "🟢 " if tier == "daily" else ("🟡 " if tier == "mwf" else "⚪ ")
    cache = "💾" if t in _cached_set else "☁️"
    base = f"{mark}{cache} {t}"
    return f"{base} — {nombre}" if nombre else base


_ticker_legend = (
    "🟢 0DTE todos los días&#10;"
    "🟡 0DTE Lun/Mié/Vie&#10;"
    "⚪ 0DTE solo viernes (weekly)&#10;"
    "💾 cacheado&#10;"
    "☁️ sin cache"
)
# El selector "Ticker" + "Granularidad temporal de las barras" viven DENTRO del expander "Parámetros de sesión"
# (Ticker primero, antes del "Modo de fecha"). Creamos el expander acá y rendeamos en él con
# `_sesion_exp.X`. La "Info {ticker}" queda como expander SEPARADO después (no se pueden anidar).
# «Modo de backtesting» — contenedor propio del sidebar, ARRIBA de «Parámetros de sesión».
# Es un placeholder: el radio + uploader se renderizan acá más abajo (vía `_btmode_box`).
_btmode_box = st.sidebar.container()
# «Parámetros de sesión» arranca EXPANDIDO en «Backtest visual paso a paso» (el default) y se CONTRAE en
# «Cargar backtesting file». El valor del radio ya vive en session_state aunque el widget se renderice más
# abajo; al hacer clic, el rerun re-evalúa esto y abre/cierra el expander.
_sesion_expanded = st.session_state.get("bt_mode_radio", "Backtest visual paso a paso") == "Backtest visual paso a paso"
_sesion_exp = st.sidebar.expander("Parámetros de sesión", expanded=_sesion_expanded)
_sesion_exp.markdown(
    "<p style='display:flex; justify-content:space-between; align-items:center; "
    "font-weight:bold; margin: 0.3rem 0 0.3rem 0;'>"
    "<span>Ticker</span>"
    f"<span title='{_ticker_legend}' style='cursor:help; color:#888; "
    "font-weight:normal;'>ⓘ</span>"
    "</p>",
    unsafe_allow_html=True,
)
# Default de "Ticker": QQQ, SPY, IWM — el Data seed del template (núcleo 0DTE diario con data
# local completa). Antes se autocompletaba por día de la semana (ticker_prefs); se fijó al trío
# a pedido del usuario (2026-07-03) para calzar con la config global del batch.
_PREF_DEF = ["QQQ", "SPY", "IWM"]
# Re-siembra SOLO cuando la lista por defecto cambia → no pisa una selección manual.
_valid_pref = ([t for t in _PREF_DEF if t in TICKER_OPTIONS]
               or ([TICKER_OPTIONS[_default_idx]] if TICKER_OPTIONS else []))
if st.session_state.get("_pref_sig_bt") != tuple(_valid_pref):
    st.session_state["_pref_sig_bt"] = tuple(_valid_pref)
    st.session_state["tickers_select"] = _valid_pref
tickers = _sesion_exp.multiselect(
    "Ticker",
    options=TICKER_OPTIONS,
    key="tickers_select",
    format_func=_ticker_label,
    label_visibility="collapsed",
    help="Uno o varios. Con varios tickers, el backtest corre todos (× fecha o rango de fechas) "
         "y muestra los resultados COMBINADOS (totales/riesgo agregados + columna Ticker).",
)
# El PRIMER ticker manda para la Info/Rango/Horario del panel lateral (que son por-ticker).
# La CORRIDA usa la lista completa `tickers`; cada ticker aplica su propio rango de prima.
ticker = tickers[0] if tickers else TICKER_OPTIONS[_default_idx]

# Aviso si el ticker elegido solo vence Lun/Mié/Vie — evita el error
# "No 0 DTE option ..." al elegir un martes o jueves.
if _zerodte_map.get(ticker) == "mwf":
    _sesion_exp.caption(
        f"🟡 **{ticker}** vence **Lun / Mié / Vie** — no hay 0DTE los **Mar / Jue**. "
        f"Para esos días no existe contrato del mismo día."
    )
elif _zerodte_map.get(ticker) is None:
    _sesion_exp.caption(
        f"⚪ **{ticker}** vence **solo los viernes** (weekly) — no hay 0DTE de **Lun a Jue**. "
        f"Para backtestearlo elegí una fecha que sea **viernes**."
    )

# Aviso si el ticker elegido NO tiene cache local — vamos a tener que pegarle
# a Polygon en vivo (lento) y puede fallar para fechas históricas o feriados.
if ticker not in _cached_set:
    _sesion_exp.caption(
        f"⚠ **{ticker}** no tiene cache local. Cada backtest va a bajar datos "
        f"de Polygon en vivo (lento, consume rate limit)."
    )


# ── Granularidad temporal de las barras (1 min / 30 seg / 15 seg) ──────────────────────────────
@st.cache_data(ttl=20)
def _subsec_tickers() -> set:
    """Tickers con la data fina (30s/15s) PRE-DESCARGADA (options_replay/download_subsecond.py
    escribe data/resolutions_available.json)."""
    try:
        import json as _json
        _p = Path(__file__).resolve().parent / "data" / "resolutions_available.json"
        return set(_json.loads(_p.read_text(encoding="utf-8")).get("tickers", []))
    except Exception:
        return set()


# Granularidad temporal de las barras: ya NO se elige en el manual (se configura por iteración en «Backtest
# de señales / iteraciones», una vez añadida la iteración). El backtest manual corre a 1 min.
bar_resolution = "1min"
get_downloader(api_key).resolution = bar_resolution   # los handlers comparten este downloader

def_premium_min, def_premium_max = _defaults_for(ticker)

# Info del ticker (Excel) + Rango óptimo Min/Max — todo dentro del expander.
_ticker_info_all = load_ticker_info()
_tinfo = _ticker_info_all.get(ticker.upper().strip())

# Inicializar inputs Min/Max en session_state si todavía no existen
# (necesario para el primer render).
if "premium_min_input" not in st.session_state:
    st.session_state["premium_min_input"] = def_premium_min
if "premium_max_input" not in st.session_state:
    st.session_state["premium_max_input"] = def_premium_max

# «Info {ticker}» del backtest MANUAL ya NO se muestra en el sidebar (manual deprecado). El rango
# de prima se toma de los defaults del ticker; el resto del código todavía referencia estas vars.
premium_min, premium_max = def_premium_min, def_premium_max

# Rango EXTENDIDO (Min-Max ÷100) que el motor usa como 2do nivel de la cascada
# de selección de contratos. Si el usuario editó los inputs MIN/MAX del panel,
# se toman de session_state; sino del JSON ÷100; sino cae al rango óptimo.
if _tinfo and _tinfo.get("min") is not None and _tinfo.get("max") is not None:
    ext_premium_min = float(st.session_state.get(f"min_input_{ticker}", _tinfo["min"] / 100.0))
    ext_premium_max = float(st.session_state.get(f"max_input_{ticker}", _tinfo["max"] / 100.0))
else:
    ext_premium_min = premium_min
    ext_premium_max = premium_max


def _ranges_for(_tk):
    """(premium_min, premium_max, ext_min, ext_max) por ticker. El PRIMERO usa lo editado en
    el panel; el resto, sus defaults del JSON (cada ticker tiene su propio rango de prima)."""
    if _tk == ticker:
        return float(premium_min), float(premium_max), float(ext_premium_min), float(ext_premium_max)
    _pmn, _pmx = _defaults_for(_tk)
    _ti = _ticker_info_all.get(_tk.upper().strip())
    if _ti and _ti.get("min") is not None and _ti.get("max") is not None:
        return float(_pmn), float(_pmx), float(_ti["min"]) / 100.0, float(_ti["max"]) / 100.0
    return float(_pmn), float(_pmx), float(_pmn), float(_pmx)


# Default = ayer, ajustado al último día hábil de mercado (si ayer fue sábado,
# domingo o feriado US, retrocede al viernes hábil anterior). Solo para «Fecha fija»;
# el RANGO usa los defaults fijos del Data seed del template (2026-01-01 → 2026-06-24).
default_date = _last_open_market_day(date_cls.today() - timedelta(days=1))

# El header "Parámetros de sesión", los time_input "Inicio/Fin" y el radio
# "Modo de fecha" viven FUERA del form pero visualmente forman parte del mismo
# bloque (no hay separador). Necesitan estar fuera del form para que sus cambios
# sean reactivos inmediatos:
# - Inicio/Fin: el dropdown "Hora de orden" (dentro del form) se actualiza con
#   las opciones válidas al cambiar la ventana horaria.
# - Modo de fecha: para que aparezca/desaparezca el segundo date_input
#   inmediatamente al cambiar entre "Fecha fija" y "Rango de fechas".
# Usamos st.container(border=True) en vez de st.form porque queremos:
# (a) Un rectángulo con borde y esquinas redondeadas que agrupe TODO visualmente.
# (b) Reactividad inmediata en los widgets reactivos (Inicio/Fin, Modo de fecha)
#     — st.form gatea cambios hasta el submit y eso rompe la UX del condicional
#     "Fecha fija / Rango de fechas".
# Como ya no hay form, los botones de abajo son st.button normales (el handler
# corre cuando se clickean, leyendo el estado actual de todos los widgets).
with _sesion_exp:
    # Inicio/Fin ya NO se ingresan por UI — vienen de market_hours.json
    # (default 09:30–16:00, override por ticker si hace falta).
    t_start, t_end = get_market_hours(ticker)
    _date_mode = st.radio(
        "Modo de fecha",
        options=["Fecha fija", "Rango de fechas"],
        index=0,
        horizontal=True,
        key="date_mode_radio",
        help=(
            "'Fecha fija' = simulación de un solo día (permite usar 'Próxima iteración'). "
            "'Rango de fechas' = backtest día por día sobre el rango (1 iteración por día hábil)."
        ),
    )

    # Callback compartido por los date_inputs: al cambiar de fecha,
    # resetear el "Horario de entrada" a 09:30.
    def _reset_hora_on_date_change():
        st.session_state["horario_entrada"] = time_cls(9, 30)

    # Date inputs condicionales según el radio "Modo de fecha".
    if _date_mode == "Fecha fija":
        c_fecha, _c_fecha_spacer = st.columns(2)
        sel_start = c_fecha.date_input(
            "Fecha",
            value=default_date,
            format="YYYY-MM-DD",
            key="sel_fecha_unica",
            on_change=_reset_hora_on_date_change,
            help="Fecha del día a simular.",
        )
        sel_end = sel_start
    else:
        c_fecha_ini, c_fecha_fin = st.columns(2)
        sel_start = c_fecha_ini.date_input(
            "Fecha inicial",
            value=date_cls(2026, 1, 1),         # default del Data seed del template
            format="YYYY-MM-DD",
            key="sel_fecha_inicial",
            on_change=_reset_hora_on_date_change,
            help="Fecha de inicio del rango (inclusiva). Default: 2026-01-01 (el Data seed del template).",
        )
        # Robustez: la final NO puede ser < inicial. Si la guardada quedó anterior a
        # la inicial (porque el usuario movió la inicial más adelante), la subimos a
        # la inicial ANTES de crear el widget — si no, Streamlit crashea con
        # "value must lie between min_value and max_value".
        if (st.session_state.get("sel_fecha_final") is not None
                and st.session_state["sel_fecha_final"] < sel_start):
            st.session_state["sel_fecha_final"] = sel_start
        sel_end = c_fecha_fin.date_input(
            "Fecha final",
            value=max(date_cls(2026, 6, 24), sel_start),   # default del Data seed del template
            format="YYYY-MM-DD",
            min_value=sel_start,
            key="sel_fecha_final",
            on_change=_reset_hora_on_date_change,
            help=(
                "Fecha de cierre del rango (inclusiva). No puede ser anterior "
                "a la fecha inicial."
            ),
        )
    # `is_range` se deriva del modo explícito (no de si las fechas coinciden):
    # si el usuario eligió "Rango de fechas" con start == end, se trata igual
    # como batch de 1 día. Si eligió "Fecha fija", siempre es single-day.
    is_range = _date_mode == "Rango de fechas"
    # Backward-compat: el resto del código usa `sel_date` para single-day.
    sel_date = sel_start

    # "Estrategia" va acá (debajo de la Fecha, dentro de «Parámetros de sesión»). El selectbox y su
    # descripción se RENDERIZAN en este placeholder más abajo (necesitan _mode_opts / _MODE_DESC, que
    # se arman en «Parámetros por iteración»).
    _estrat_box = st.container()

    # "Horario de entrada" y "Horario de salida" LADO A LADO (misma fila), cada uno
    # un componente hora:minuto. Reemplaza el cierre fijo 16:00 en la lógica.

    # --- Preparar session_state ANTES de instanciar los widgets ---
    # Entrada: aplicar sync pendiente de la iteración anterior (la "Próxima iteración"
    # sugiere el minuto siguiente). Solo en modo single (en rango no se hereda).
    _pending_sync = st.session_state.pop("_pending_hora_sync", None)
    if _pending_sync is not None and not is_range:
        if isinstance(_pending_sync, time_cls):
            st.session_state["horario_entrada"] = _pending_sync
        else:  # compat: tuplas (h, m) o (h, m, step) de versiones previas
            st.session_state["horario_entrada"] = time_cls(
                int(_pending_sync[0]), int(_pending_sync[1]))
    if "horario_entrada" not in st.session_state:
        _def = time_cls(9, 30)
        st.session_state["horario_entrada"] = _def if t_start <= _def <= t_end else t_start
    if "horario_salida" not in st.session_state:
        st.session_state["horario_salida"] = time_cls(16, 0)

    # Restringir SIEMPRE entrada y salida a [09:30, 16:00]. Si quedó un valor fuera de
    # ese rango (tipeado en el time_input), se ajusta al borde más cercano ANTES de
    # instanciar el widget → el componente "no permite" quedar fuera del horario.
    _MKT_OPEN, _MKT_CLOSE = time_cls(9, 30), time_cls(16, 0)
    for _hk in ("horario_entrada", "horario_salida"):
        _hv = st.session_state.get(_hk)
        if _hv is not None and not (_MKT_OPEN <= _hv <= _MKT_CLOSE):
            st.session_state[_hk] = min(max(_hv, _MKT_OPEN), _MKT_CLOSE)

    # --- Config de la corrida: AHORA FIJA. DTE, horarios, criterio de contrato, modelo de
    #     fills y ventana de búsqueda se configuran en «Backtest de señales / iteraciones»
    #     (vía el puente «📤 Backtestear con TODAS las funciones →»). El sidebar manual solo
    #     elige Ticker + Fecha + Resolución + Modo de fecha. ---
    dte = 0
    _is_dte1 = False

    hora_orden = time_cls(9, 30)
    horario_salida = time_cls(16, 0)

    # Verificación de venta: siempre cada minuto (se quitó el selector dedicado).
    sell_check_min = 1

    selection_criterion = "spread"
    _spread_cfg = None
    _f1, _f2 = _fill_flags(_FILL_MODE_F2)
    entry_at_ask = exit_at_bid = (_f1 or _f2)
    nbbo_timeline = _f2
    search_window_min = 4.0

# El aviso de "Modo rango" se muestra como tooltip ⓘ en el header
# "Parámetros por iteración" (más abajo), solo cuando is_range.

# Parámetros por iteración — VISIBLES EN AMBOS MODOS (single y rango). En rango,
# estos mismos valores (auto-aplicados desde el slider, editables a mano) se usan
# para todos los días del batch. Antes esta sección se ocultaba en modo rango, lo
# que daba la sensación de que "desaparecía todo el panel".
with st.sidebar.container():
    if is_range:
        st.caption("🤖 Modo rango — estos parámetros se aplican a todos "
                   "los días del rango (un solo set de parámetros para el batch).")

    # Modo de straddle — radio con 3 opciones (mutuamente excluyente por diseño).
    # El callback fuerza el split CALL%/PUT% según el modo seleccionado:
    #   CALL y PUT → 50/50,  Sólo CALL → 100/0,  Sólo PUT → 0/100.
    def _sync_straddle_mode_changed():
        mode = st.session_state.get("straddle_mode_radio", "CALL y PUT")
        if mode.startswith("Sólo CALL"):
            new_call_pct, new_put_pct = 100.0, 0.0
        elif mode.startswith("Sólo PUT"):
            new_call_pct, new_put_pct = 0.0, 100.0
        else:  # "CALL y PUT" / "CALL o PUT" / EOD de dos piernas → ambas 50/50
            new_call_pct, new_put_pct = 50.0, 50.0
        st.session_state["call_pct"] = new_call_pct
        st.session_state["put_pct"] = new_put_pct
        tot = st.session_state.get("invest_total", 1000.0)
        st.session_state["call_dollars"] = (new_call_pct / 100.0) * tot
        st.session_state["put_dollars"] = (new_put_pct / 100.0) * tot
        # Al SELECCIONAR cualquier modo, resetear los params POR PIERNA a su
        # baseline: Umbral de ROI 10% y Stop loss -100% para CALL y para PUT.
        st.session_state["call_roi_pct"] = 10.0
        st.session_state["put_roi_pct"] = 10.0
        st.session_state["call_stop_pct"] = -100.0
        st.session_state["put_stop_pct"] = -100.0

    # La etiqueta MOSTRADA (sin "Sólo") se controla con format_func; el VALOR INTERNO se mantiene
    # ("Sólo CALL", …) para NO tocar la detección de modo de abajo.
    # Opciones ORDENADAS alfabéticamente por lo que se ve.
    _MODE_DISPLAY = {"Sólo CALL": "CALL", "Sólo PUT": "PUT",
                     "Sólo CALL (End of Day)": "CALL (End of Day)",
                     "Sólo PUT (End of Day)": "PUT (End of Day)"}
    _mode_opts = sorted(
        ["CALL y PUT", "CALL y PUT (Refuerzo)", "CALL y PUT (Refuerzo) (End of Day)",
         "CALL y PUT (plus)", "Sólo CALL", "Sólo PUT", "CALL o PUT", "CALL o PUT (plus)",
         "CALL o PUT (End of Day)", "CALL o PUT (Until reach ROI(%))",
         "Sólo CALL (End of Day)", "Sólo PUT (End of Day)"],
        key=lambda o: _MODE_DISPLAY.get(o, o))
    with _estrat_box:
        _straddle_mode = st.selectbox(
            "Tipo de operación",
            options=_mode_opts,
            format_func=lambda o: _MODE_DISPLAY.get(o, o),
            index=_mode_opts.index("CALL y PUT"),   # default = straddle 50/50 (antes lo fijaba el auto-apply)
            key="straddle_mode_radio",
            on_change=_sync_straddle_mode_changed,
        )
    is_call_only_eod = _straddle_mode == "Sólo CALL (End of Day)"
    is_put_only_eod = _straddle_mode == "Sólo PUT (End of Day)"
    only_call_now = _straddle_mode.startswith("Sólo CALL")   # incluye (End of Day) → split 100/0
    only_put_now = _straddle_mode.startswith("Sólo PUT")     # incluye (End of Day) → split 0/100
    is_call_or_put = _straddle_mode == "CALL o PUT"
    is_call_or_put_plus = _straddle_mode == "CALL o PUT (plus)"
    is_call_or_put_eod = _straddle_mode == "CALL o PUT (End of Day)"
    is_call_or_put_until = _straddle_mode == "CALL o PUT (Until reach ROI(%))"
    is_both_plus = _straddle_mode == "CALL y PUT (plus)"
    is_refuerzo_eod = _straddle_mode == "CALL y PUT (Refuerzo) (End of Day)"
    is_refuerzo = _straddle_mode in ("CALL y PUT (Refuerzo)", "CALL y PUT (Refuerzo) (End of Day)")
    if is_call_only_eod:
        engine_mode = "call_only_eod"
    elif is_put_only_eod:
        engine_mode = "put_only_eod"
    elif only_call_now:
        engine_mode = "call_only"
    elif only_put_now:
        engine_mode = "put_only"
    elif is_call_or_put:
        engine_mode = "call_or_put"
    elif is_call_or_put_plus:
        engine_mode = "call_or_put_plus"
    elif is_call_or_put_eod:
        engine_mode = "call_or_put_eod"
    elif is_call_or_put_until:
        engine_mode = "call_or_put_until_roi"
    elif is_both_plus:
        engine_mode = "both_plus"
    elif is_refuerzo_eod:
        engine_mode = "both_refuerzo_eod"
    elif is_refuerzo:
        engine_mode = "both_refuerzo"
    else:
        engine_mode = "both"

    # Descripción del Tipo de operación elegido (texto por modo).
    _MODE_DESC = {
        "CALL y PUT": "🎯 **CALL y PUT** — se compran ambas piernas (50/50) y la salida es "
                      "**combinada por ROI total** (Umbral de ROI / Stop loss sobre la suma de "
                      "las dos). Termina al umbral, al stop o al cierre del día.",
        "CALL y PUT (Refuerzo)": "🎯 **CALL y PUT (Refuerzo)** — martingala **por pierna**. Igual que "
                                 "CALL y PUT (50/50): cada pierna mira su propio "
                                 "ROI y, cuando cae a **≤ −Umbral de pérdida refuerzo (%)**, se refuerza "
                                 "la **pierna que más pierde** comprando **más de ESA misma pierna** "
                                 "(mismo tipo, nunca la contraria) con su inversión inicial. Termina cuando "
                                 "el **ROI total** (ambas piernas) alcanza el **Umbral de ROI (%)** (gana) o "
                                 "cae al **−Stop loss (%)** (corta — stop sobre el TOTAL, no por pierna), o "
                                 "al cierre del día.",
        "CALL y PUT (Refuerzo) (End of Day)": "🎯 **CALL y PUT (Refuerzo) (End of Day)** — la martingala de "
                                              "**CALL y PUT (Refuerzo)** (se refuerza la **pierna que más "
                                              "pierde** al caer a **≤ −Umbral de pérdida refuerzo (%)**), pero "
                                              "**corre hasta el cierre del día**: **NO** usa Umbral de ROI (no "
                                              "corta por ganancia). El **Stop loss** sobre el total sigue "
                                              "activo; la venta es al cierre.",
        "CALL y PUT (plus)": "🎯 **CALL y PUT (plus)** — se compran ambas piernas (50/50) y se "
                             "venden las dos **solo en el Horario de salida** (sin Umbral de ROI ni "
                             "Stop loss). Termina al horario o al cierre del día.",
        "Sólo CALL": "🎯 **Sólo CALL** — una sola pierna (100% CALL). Sale por su **Umbral de "
                     "ROI** o su **Stop loss**. Termina al umbral, al stop o al cierre del día.",
        "Sólo PUT": "🎯 **Sólo PUT** — una sola pierna (100% PUT). Sale por su **Umbral de "
                    "ROI** o su **Stop loss**. Termina al umbral, al stop o al cierre del día.",
        "Sólo CALL (End of Day)": "🎯 **Sólo CALL (End of Day)** — una sola pierna (100% CALL) que se "
                                  "vende **al cierre del día**. No depende de Umbral de ROI ni Stop "
                                  "loss del ticker — el **colectivo** (si el alcance de salida lo "
                                  "incluye) sí puede cerrarla antes.",
        "Sólo PUT (End of Day)": "🎯 **Sólo PUT (End of Day)** — una sola pierna (100% PUT) que se "
                                 "vende **al cierre del día**. No depende de Umbral de ROI ni Stop "
                                 "loss del ticker — el **colectivo** (si el alcance de salida lo "
                                 "incluye) sí puede cerrarla antes.",
        "CALL o PUT": "🎯 **CALL o PUT** — se compran ambas piernas y se venden las dos en cuanto "
                      "**cualquiera alcanza +100%** (se duplica). No depende de Umbral de ROI ni "
                      "Stop loss del ticker (el **colectivo**, si está activado, sí aplica). "
                      "Termina al +100%, por colectivo o al cierre del día.",
        "CALL o PUT (plus)": "🎯 **CALL o PUT (plus)** — se compran ambas piernas. La **1ª pierna "
                             "que alcanza el Umbral de salida (%)** se vende; la otra se vende "
                             "cuando, entre lo bancado y su valor, se **recupera la inversión "
                             "total**. Termina ahí o al cierre del día.",
        "CALL o PUT (End of Day)": "🎯 **CALL o PUT (End of Day)** — se compran ambas piernas (50/50) "
                                   "y se venden las dos **al cierre del día**. No depende de Umbral de "
                                   "ROI ni Stop loss del ticker — ⚠ el **umbral/stop COLECTIVO** (si "
                                   "el alcance de salida lo incluye) **sí puede cerrarlas antes**; "
                                   "para un EOD puro elegí «Aplicar solo a tickers».",
        "CALL o PUT (Until reach ROI(%))": "🎯 **CALL o PUT (Until reach ROI(%))** — se compran ambas "
                                           "piernas (50/50) y **cada una se vende SOLA** cuando alcanza "
                                           "el **Umbral ROI (%) del ticker**; la que no llega, se vende "
                                           "**al cierre del día**. Las piernas no se esperan entre sí. "
                                           "Sin Stop loss (el colectivo, si está activado, sí aplica).",
    }
    # La descripción de la estrategia se muestra DIRECTAMENTE debajo del dropdown (caption, no
    # expander) → al elegir una estrategia, su info aparece enseguida.
    _desc = _MODE_DESC.get(_straddle_mode, "")
    if _desc:
        with _estrat_box:
            st.caption(_desc)
    # Modo de backtest: visual (flujo de siempre) o cargar un Excel de configuraciones (batch desde archivo).
    with _btmode_box:
        with st.expander("Modo de backtesting", expanded=True):
            _bt_mode = st.radio(
                "Modo de backtest",
                ["Backtest visual paso a paso", "Cargar backtesting file", "Interpretar resultados"],
                index=0, key="bt_mode_radio", label_visibility="collapsed",
                help="**Visual paso a paso**: el flujo de siempre (las filas se cargan en «Options Replay» y "
                     "corrés ahí). **Cargar backtesting file**: subís un Excel de configuraciones y, al tocar el "
                     "botón rojo, se corre el backtest de CADA fila. **Interpretar resultados**: subís un "
                     "results file y el motor lo analiza (robustez, correlaciones, clustering, día de la "
                     "semana, playbook + JSON).")
            if _bt_mode == "Cargar backtesting file":
                st.file_uploader("📄 Template de entrada — Data seed + scenarios (.xlsx)", type=["xlsx"],
                                 key="bt_file_upload",
                                 help="Backtesting_use_cases_template.xlsx: globales (tickers/fechas/tipo/…) + escenarios C001…")
                st.file_uploader("📊 Results file a LLENAR — salida (.xlsx)", type=["xlsx"],
                                 key="bt_fill_upload",
                                 help="Backtesting_Results_….xlsx con los IDs C001… pre-cargados; se rellena 1 fila por escenario "
                                      "(agregando sus 3 tickers × N días).")

    # Refuerzo: ya NO se ingresa en el manual (Umbral de pérdida refuerzo + No. de veces a reforzar
    # se configuran en «Backtest de señales / iteraciones»). Defaults fijos para la corrida manual.
    refuerzo_loss_pct = 50.0
    refuerzo_max = 2

    # -------- Bloque Inversión: total + %-split + $-split (bidireccional) --------
    # Source of truth en session_state. Callbacks mantienen % y $ sincronizados
    # entre sí y con el monto total. Funciona porque estamos en un container
    # (no en st.form, donde los on_change no se permiten).

    # Inicialización: la primera vez que se renderiza, sembramos los 5 keys.
    if "invest_total" not in st.session_state:
        st.session_state["invest_total"] = 1000.0
    if "call_pct" not in st.session_state:
        st.session_state["call_pct"] = 50.0
    if "put_pct" not in st.session_state:
        st.session_state["put_pct"] = 50.0
    if "call_dollars" not in st.session_state:
        st.session_state["call_dollars"] = 500.0
    if "put_dollars" not in st.session_state:
        st.session_state["put_dollars"] = 500.0

    # Aplicar reset de % de inversión pendiente desde la iteración anterior.
    # Debe ocurrir ANTES de instanciar los number_inputs para que tomen el
    # nuevo valor. Respeta el modo activo: si "Sólo CALL"/"Sólo PUT", el split
    # va a 100/0 ó 0/100, sino 50/50.
    if st.session_state.pop("_pending_invest_reset", False):
        _reset_mode = st.session_state.get("straddle_mode_radio", "CALL y PUT")
        if _reset_mode == "Sólo CALL":
            _reset_cpct, _reset_ppct = 100.0, 0.0
        elif _reset_mode == "Sólo PUT":
            _reset_cpct, _reset_ppct = 0.0, 100.0
        else:
            _reset_cpct, _reset_ppct = 50.0, 50.0
        _reset_tot = st.session_state["invest_total"]
        st.session_state["call_pct"] = _reset_cpct
        st.session_state["put_pct"] = _reset_ppct
        st.session_state["call_dollars"] = (_reset_cpct / 100.0) * _reset_tot
        st.session_state["put_dollars"] = (_reset_ppct / 100.0) * _reset_tot

    # Invariante: CALL (%) + PUT (%) == 100 SIEMPRE.
    # Cualquier edición que toque un % o un $ propaga al otro leg para mantener
    # la suma. Si CALL ($) excede la Inversión total, se clampea a 100/0.

    def _sync_total_to_dollars():
        """Cambió Inversión total → recomputar $ de cada leg manteniendo %s
        (los %s ya suman 100 por invariante)."""
        tot = st.session_state["invest_total"]
        st.session_state["call_dollars"] = (st.session_state["call_pct"] / 100.0) * tot
        st.session_state["put_dollars"] = (st.session_state["put_pct"] / 100.0) * tot

    def _sync_call_pct_changed():
        """Editaron CALL (%) → forzar PUT (%) = 100 - CALL (%), recomputar ambos $."""
        tot = st.session_state["invest_total"]
        call_pct = float(st.session_state["call_pct"])
        st.session_state["put_pct"] = 100.0 - call_pct
        st.session_state["call_dollars"] = (call_pct / 100.0) * tot
        st.session_state["put_dollars"] = (st.session_state["put_pct"] / 100.0) * tot

    def _sync_put_pct_changed():
        """Editaron PUT (%) → forzar CALL (%) = 100 - PUT (%), recomputar ambos $."""
        tot = st.session_state["invest_total"]
        put_pct = float(st.session_state["put_pct"])
        st.session_state["call_pct"] = 100.0 - put_pct
        st.session_state["put_dollars"] = (put_pct / 100.0) * tot
        st.session_state["call_dollars"] = (st.session_state["call_pct"] / 100.0) * tot

    def _sync_call_dollars_changed():
        """Editaron CALL ($) → derivar CALL (%), forzar PUT (%) = 100 - CALL (%),
        recomputar PUT ($). Si CALL ($) > total, clampea a 100% CALL / 0% PUT."""
        tot = st.session_state["invest_total"]
        if tot <= 0:
            return
        call_d = float(st.session_state["call_dollars"])
        # Clamp a [0, tot]: si excede, ajusta el $ a tot y % a 100.
        if call_d > tot:
            call_d = tot
            st.session_state["call_dollars"] = tot
        call_pct = (call_d / tot) * 100.0
        st.session_state["call_pct"] = call_pct
        st.session_state["put_pct"] = 100.0 - call_pct
        st.session_state["put_dollars"] = tot - call_d

    def _sync_put_dollars_changed():
        """Editaron PUT ($) → derivar PUT (%), forzar CALL (%) = 100 - PUT (%),
        recomputar CALL ($). Si PUT ($) > total, clampea a 100% PUT / 0% CALL."""
        tot = st.session_state["invest_total"]
        if tot <= 0:
            return
        put_d = float(st.session_state["put_dollars"])
        if put_d > tot:
            put_d = tot
            st.session_state["put_dollars"] = tot
        put_pct = (put_d / tot) * 100.0
        st.session_state["put_pct"] = put_pct
        st.session_state["call_pct"] = 100.0 - put_pct
        st.session_state["call_dollars"] = tot - put_d

    # Inversión: ya NO se ingresa en el manual (se configura en «Backtest de señales /
    # iteraciones»). Se toma del estado: sembrado 1000 al 50/50 y ajustado por el modo (Estrategia).
    invest_call = float(st.session_state.get("call_dollars", 500.0))
    invest_put = float(st.session_state.get("put_dollars", 500.0))

    # Default del Umbral de ROI si nunca se sembró (caso primera carga).
    if "umbral_roi_pct" not in st.session_state:
        st.session_state["umbral_roi_pct"] = 10.0
    # Defaults de params por pierna ("CALL o PUT") y del umbral de salida ("plus").
    for _k, _v in (("call_roi_pct", 10.0), ("call_stop_pct", -100.0),
                   ("put_roi_pct", 10.0), ("put_stop_pct", -100.0),
                   ("exit_plus_pct", 5.0)):
        if _k not in st.session_state:
            st.session_state[_k] = _v
    # El cierre por umbral siempre se evalúa sobre el ROI (%) total.
    exit_metric = "total"
    exit_plus_threshold_pct = 0.05  # default; solo en "CALL o PUT (plus)"
    exit_plus_time = None           # default; solo en "CALL o PUT (plus)"

    if is_call_or_put:
        # CALL o PUT: salida COMBINADA al +100%. Se venden AMBAS piernas cuando
        # CUALQUIERA alcanza +100% (se duplica). NO usa Umbral de ROI ni Stop
        # loss — por eso no se muestran esos inputs. Termina al +100% o al cierre.
        exit_threshold_pct = 1.0          # +100% (solo referencia de estilo)
        stop_loss_pct = -1.0
        call_exit_threshold_pct = put_exit_threshold_pct = 1.0
        call_stop_loss_pct = put_stop_loss_pct = -1.0
    elif is_both_plus:
        # CALL y PUT (plus): se compran ambas y se venden las DOS SOLO al llegar al
        # Horario de salida (1 min antes). NO usa Umbral de ROI ni Stop loss.
        exit_threshold_pct = 1.0          # ignorado por el modo both_plus
        stop_loss_pct = -1.0
        call_exit_threshold_pct = put_exit_threshold_pct = 1.0
        call_stop_loss_pct = put_stop_loss_pct = -1.0
    elif is_call_or_put_eod:
        # CALL o PUT (End of Day): se compran ambas y se venden las DOS al cierre del día.
        # NO usa Umbral de ROI ni Stop loss — por eso no se muestran esos inputs.
        exit_threshold_pct = 1.0          # ignorado por el modo call_or_put_eod
        stop_loss_pct = -1.0
        call_exit_threshold_pct = put_exit_threshold_pct = 1.0
        call_stop_loss_pct = put_stop_loss_pct = -1.0
    elif is_call_only_eod or is_put_only_eod:
        # Sólo CALL/PUT (End of Day): una sola pierna, vendida al cierre del día.
        # NO usa Umbral de ROI ni Stop loss — por eso no se muestran esos inputs.
        exit_threshold_pct = 1.0          # ignorado por los modos *_only_eod
        stop_loss_pct = -1.0
        call_exit_threshold_pct = put_exit_threshold_pct = 1.0
        call_stop_loss_pct = put_stop_loss_pct = -1.0
    elif is_call_or_put_plus:
        # CALL o PUT (plus): la 1ª pierna que alcanza el "Umbral de salida (%)" se
        # vende y banca su ganancia; la otra se vende cuando, sumando lo bancado +
        # su valor, se recupera la inversión TOTAL. Si no, cierran al fin del día.
        exit_plus_threshold_pct = st.number_input(
            "Umbral de salida (%)", key="exit_plus_pct",
            step=5.0, min_value=1.0, format="%.2f",
            help="ROI% al que se vende la PRIMERA pierna (la que llegue primero al umbral).",
        ) / 100.0
        # La "Hora de salida" del plus es ahora el "Horario de salida" GENERAL
        # (arriba, default 16:00): la pierna pendiente se liquida en el minuto ANTES
        # de esa hora (vía end_ts = salida - 1min), igual que las demás estrategias.
        exit_plus_time = horario_salida
        exit_threshold_pct = exit_plus_threshold_pct   # referencia de estilo
        stop_loss_pct = -1.0
        call_exit_threshold_pct = put_exit_threshold_pct = exit_plus_threshold_pct
        call_stop_loss_pct = put_stop_loss_pct = -1.0
    else:
        # Umbral de ROI / Stop loss: ya NO se ingresan en el manual (se configuran en
        # «Backtest de señales / iteraciones»). Se toman del estado: umbral sembrado 10%,
        # stop fijo -100%.
        exit_threshold_pct = float(st.session_state.get("umbral_roi_pct", 10.0)) / 100.0
        stop_loss_pct = -1.0
        call_exit_threshold_pct = put_exit_threshold_pct = exit_threshold_pct
        call_stop_loss_pct = put_stop_loss_pct = stop_loss_pct

st.sidebar.markdown("---")
# --- PUENTE (botón principal): manda la config manual (ticker × fecha × hora) a «Backtest de
#     señales / iteraciones» para correr ahí con TODAS las funciones (confirmación, flip,
#     sin-lookahead, ROI colectivo, Auto-DTE). Reemplaza al viejo "Iniciar nueva simulación". ---
_TIPO_BRIDGE = {"Sólo CALL": "CALL", "Sólo PUT": "PUT"}   # el panel de señales usa "CALL"/"PUT"
if st.sidebar.button("📤 Backtestear con TODAS las funciones →", type="primary", use_container_width=True,
                     key="btn_to_signals",
                     help="Genera filas en «Backtest de señales / iteraciones» (derecha) con tu "
                          "ticker(s) × fecha(s) y la hora de entrada, para correr ahí con confirmación, "
                          "flip, sin-lookahead, ROI colectivo, etc. — las funciones que el backtest "
                          "manual no tiene."):
    _hora = hora_orden.strftime("%H:%M")
    _tipo = st.session_state.get("straddle_mode_radio", "CALL y PUT")
    _tipo = _TIPO_BRIDGE.get(_tipo, _tipo)
    if is_range:
        _dates = [d.date().isoformat() for d in pd.date_range(sel_start, sel_end, freq="B")]
    else:
        _dates = [sel_date.isoformat()]
    _dates = [d for d in _dates if _non_trading_reason(d) is None]   # sin feriados / fin de semana
    _bt_mode = st.session_state.get("bt_mode_radio", "Backtest visual paso a paso")
    _bt_file = st.session_state.get("bt_file_upload")
    _bt_fill = st.session_state.get("bt_fill_upload")
    if _bt_mode == "Cargar backtesting file" and _bt_file is not None and _bt_fill is not None:
        # === BATCH dirigido por el template (Data seed + scenarios). TODA la config sale del Data
        #     seed (tickers, fechas, tipo, inversión, horarios…); se IGNORAN los Session Parameters.
        #     Lanza run_ucbatch.py en una CONSOLA NUEVA (async): no bloquea la app, no se cuelga,
        #     multiproceso. Ves el progreso en esa ventana; al terminar salta un aviso de Windows. ===
        try:
            from ucbatch import reader as _ucr
            _seed, _scens = _ucr.read_template(_bt_file)
        except Exception as _e:   # noqa: BLE001
            _seed, _scens = None, []
            st.sidebar.error(f"No pude leer el template: {_e}")
        if not _scens:
            st.sidebar.warning("⚠️ El template no tiene escenarios (hoja «Backtesting scenarios» con columna ID).")
        elif not _seed.tickers:
            st.sidebar.warning("⚠️ El «Data seed» no tiene Tickers cargados.")
        else:
            import datetime as _dt
            import subprocess as _sp
            import sys as _sysx
            from ucbatch import report as _ucrep, runner as _ucrun
            _dl0 = get_downloader(api_key)
            _or_dir = Path(_dl0.data_dir).parent                         # options_replay/
            _job_id = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            _jobs = Path(_dl0.data_dir) / ".batch_jobs" / _job_id
            _jobs.mkdir(parents=True, exist_ok=True)
            _in_xlsx = _jobs / "input.xlsx"
            _in_xlsx.write_bytes(_bt_file.getvalue())
            _fill_xlsx = _jobs / "results_fill.xlsx"                      # el results file a rellenar
            _fill_xlsx.write_bytes(_bt_fill.getvalue())
            _res_dir = _or_dir.parent / "resultados"                     # <Traiding>/resultados/
            _res_dir.mkdir(parents=True, exist_ok=True)
            _out_xlsx = _res_dir / _ucrep.output_filename(_seed)
            _days = _ucrun.trading_days(_seed.fecha_inicial, _seed.fecha_final)
            _cmd = [_sysx.executable, str(_or_dir / "run_ucbatch.py"),
                    "--excel", str(_in_xlsx), "--fill", str(_fill_xlsx),
                    "--out", str(_res_dir), "--notify"]
            try:
                _sp.Popen(_cmd, creationflags=getattr(_sp, "CREATE_NEW_CONSOLE", 0), cwd=str(_or_dir))
                for _k in ("batch_results", "batch_meta", "replay", "_batch_pending"):
                    st.session_state.pop(_k, None)
                st.session_state["batch_job"] = {
                    "out": str(_out_xlsx), "n": len(_scens) * len(_days) * len(_seed.tickers),
                    "n_cfg": len(_scens), "tickers": _seed.tickers, "dates": _days, "at": _job_id}
                st.rerun()
            except Exception as _le:   # noqa: BLE001
                st.sidebar.error(f"No pude lanzar el runner en consola ({_le}). Corrélo a mano: "
                                 f"`python options_replay/run_ucbatch.py --excel \"{_in_xlsx}\"`")
    elif _bt_mode == "Cargar backtesting file":
        st.sidebar.warning("⚠️ Subí **ambos** archivos: el **template de entrada** y el **results file a llenar**.")
    else:
        # === Puente normal: manda las filas (ticker × fecha) al panel de señales. ===
        _rows = [{"Ticker": str(tk).upper(), "Fecha": d, "Hora": _hora, "Tipo": _tipo}
                 for tk in tickers for d in _dates]
        if not _rows:
            st.sidebar.warning("⚠️ Sin filas: elegí ≥1 ticker y ≥1 fecha hábil (no feriado/fin de semana).")
        else:
            for _k in ("batch_results", "batch_meta", "replay", "_batch_pending"):   # limpiar la derecha
                st.session_state.pop(_k, None)
            st.session_state.pop("_iters_cfg_por_dia", None)  # seed nuevo → el dropdown decide
            st.session_state.pop("_iters_es_tv", None)        # seed del sidebar ≠ Trading view
            st.session_state["bt_iters"] = _rows
            st.session_state.pop("bt_iters_editor", None)   # forzar re-seed del editor
            st.session_state["_iters_sel_seed"] = True       # todas seleccionadas
            st.session_state["bt_iters_open"] = True          # abrir el panel de señales
            st.toast(f"📤 {len(_rows)} fila(s) cargadas en «Backtest de señales» — corré ahí con todo.")
            st.rerun()

def _validate_form() -> bool:
    if premium_min >= premium_max:
        st.error("Premium `Min` debe ser menor que `Max`.")
        return False
    if t_start >= t_end:
        st.error("La hora de inicio debe ser menor que la de fin.")
        return False
    if not (t_start <= hora_orden <= t_end):
        st.error(
            f"El 'Horario de entrada' ({hora_orden:%H:%M}) debe estar dentro de la "
            f"ventana horaria ({t_start:%H:%M} – {t_end:%H:%M})."
        )
        return False
    if sel_end < sel_start:
        st.error(
            f"La 'Fecha final' ({sel_end}) no puede ser anterior a la "
            f"'Fecha inicial' ({sel_start})."
        )
        return False

    # No permitir iniciar si la inversión no alcanza para ≥1 contrato entero.
    # El contrato más caro posible (óptimo o extendido) cuesta cost_1 = premium×100.
    # Si la inversión del leg es menor, daría 0 contratos → bloqueamos.
    _prem_cap = max(float(premium_max), float(ext_premium_max))
    cost_1 = _prem_cap * 100.0
    if is_range:
        _inv = float(st.session_state.get("invest_total", 1000.0))
        if _inv < cost_1:
            st.error(
                f"La inversión (${_inv:,.2f}) no alcanza para comprar ni 1 contrato entero. "
                f"Un contrato cuesta hasta ${cost_1:,.2f} (premium máx ${_prem_cap:.2f} × 100). "
                f"Aumentá la **Inversión ($)**."
            )
            return False
    else:
        if not only_put_now and float(invest_call) < cost_1:
            st.error(
                f"La **Inversión en CALL** (${float(invest_call):,.2f}) no alcanza para 1 contrato entero "
                f"(hasta ${cost_1:,.2f} = premium máx ${_prem_cap:.2f} × 100). Aumentala."
            )
            return False
        if not only_call_now and float(invest_put) < cost_1:
            st.error(
                f"La **Inversión en PUT** (${float(invest_put):,.2f}) no alcanza para 1 contrato entero "
                f"(hasta ${cost_1:,.2f} = premium máx ${_prem_cap:.2f} × 100). Aumentala."
            )
            return False

    # ("CALL o PUT (plus)": la Hora de salida se autocorrige más arriba, junto al
    #  widget, para que sea siempre posterior a la hora de orden — sin bloquear acá.)
    return True


# === Aviso de un backtest lanzado en SEGUNDO PLANO (consola aparte; modo «Cargar backtesting file»). ===
_job = st.session_state.get("batch_job")
if _job:
    st.markdown("### 🚀 Backtest en segundo plano")
    st.success("Lo lancé en una **ventana de terminal aparte** — rápido (multiproceso) y sin bloquear la "
               "app. Mirá esa ventana para el **progreso / ETA**; al terminar salta un **aviso de Windows** "
               "y el Excel queda guardado.")
    st.markdown(f"📄 **Resultados** (cuando termine): `{_job.get('out', '')}`")
    st.caption(f"{_job.get('n_cfg', '?')} configs × {len(_job.get('tickers', []))} ticker(s) × "
               f"{len(_job.get('dates', []))} fecha(s) = {_job.get('n', 0):,} backtests · lanzado {_job.get('at', '')}")
    _cja, _cjb = st.columns(2)
    if _cja.button("📂 Abrir carpeta de resultados", use_container_width=True):
        try:
            import os as _os2
            _os2.startfile(str(Path(_job["out"]).parent))   # noqa: S606  (abrir Explorer en Windows)
        except Exception as _oe:   # noqa: BLE001
            st.caption(f"No pude abrir la carpeta: {_oe}")
    if _cjb.button("🗑️ Limpiar aviso", use_container_width=True):
        st.session_state.pop("batch_job", None)
        st.rerun()
    st.stop()


# === Modo «Cargar backtesting file»: mostrar el Data seed (config global del batch) en Options Replay. ===
if (st.session_state.get("bt_mode_radio") == "Cargar backtesting file"
        and st.session_state.get("bt_file_upload") is not None):
    st.markdown("### 📄 Data seed — configuración del batch")
    try:
        from ucbatch import reader as _ucr2
        _seed2, _scens2 = _ucr2.read_template(st.session_state["bt_file_upload"])
        st.caption(f"**{len(_scens2)} escenarios** × {len(_seed2.tickers)} tickers "
                   f"({', '.join(_seed2.tickers)}) × días hábiles {_seed2.fecha_inicial} → "
                   f"{_seed2.fecha_final}. Toda la config sale de acá; **los Parámetros de sesión se "
                   "ignoran** en este modo.")
        st.dataframe(pd.DataFrame(_seed2.display, columns=["Parámetro", "Valor"]),
                     hide_index=True, use_container_width=True)
        st.info("Tocá el botón rojo **Backtest** (panel izquierdo) para lanzar el batch en segundo plano.")
    except Exception as _se:   # noqa: BLE001
        st.warning(f"No pude leer el Data seed del archivo: {_se}")
    st.stop()


# === Modo «Interpretar resultados»: motor de interpretación de un results file (bt_analysis). ===
if st.session_state.get("bt_mode_radio") == "Interpretar resultados":
    st.markdown("### 🔬 Interpretación de resultados de backtesting")
    st.caption("Subí un **results file** (el Excel que llena el batch). Opcionales: el **template** "
               "(para correlacionar condición→ROI) y un file **ID×día** (Analisis_ID_x_diasemana) para "
               "el análisis por día de la semana. El motor prioriza **robustez y consistencia**, no el "
               "mayor ROI aislado, y concluye **NO OPERAR** cuando no hay ventaja estadística.")
    _iu1, _iu2, _iu3 = st.columns(3)
    _res_up = _iu1.file_uploader("📊 Results file (.xlsx)", type=["xlsx"], key="bta_results")
    _tpl_up = _iu2.file_uploader("📄 Template (opcional)", type=["xlsx"], key="bta_template")
    _dow_up = _iu3.file_uploader("📅 ID×día (opcional)", type=["xlsx"], key="bta_dow")
    if _res_up is None:
        st.info("Subí al menos el **results file** para interpretar.")
        st.stop()
    try:
        from bt_analysis import engine as _bte, loader as _btl, ui as _btu
        _rdf, _gran = _btl.load_results(_res_up)
        _bseed, _bsc = _btl.load_template(_tpl_up) if _tpl_up is not None else ({}, None)
        _bdow = _btl.load_dow(_dow_up) if _dow_up is not None else None
        with st.spinner("Analizando…"):
            _rep = _bte.analyze(_rdf, _gran, scenarios_df=_bsc, dow_df=_bdow, seed=_bseed)
        # Playbook a sesión → fuente «🧠 Última interpretación» del generador rango+criterios.
        st.session_state["bta_playbook_json"] = _rep.get("playbook_json") or {}
        _btu.render(_rep)
    except Exception as _ie:   # noqa: BLE001
        st.error(f"No pude interpretar el file: {_ie}")
        st.exception(_ie)
    st.stop()


replay_state = st.session_state.get("replay")
if replay_state is None:
    st.info("Configurá **Ticker** y **Fecha** en la barra lateral y pulsá "
            "**📤 Backtestear con TODAS las funciones →** para cargar las filas en el "
            "panel **🔬 Backtest de señales / iteraciones** y correr ahí.")
    st.stop()


def _color_leg_pct(v):
    if pd.isna(v):
        return ""
    if v >= 0.01:  # >= 1%
        return "background-color: #c8e6c9"  # verde claro
    if v < 0.0:
        return "background-color: #ffcdd2"  # rojo claro
    return ""


def _make_total_pct_styler(threshold: float, stop_loss: float | None = None, is_metric: bool = True):
    """Closure que estiliza la columna 'ROI (%)' (o la métrica activa):
    - primera celda que cruza el umbral del usuario → verde oscuro (si is_metric)
    - celda <= stop_loss (valor negativo del usuario) → rojo oscuro
    - cualquier celda > 100% → verde claro
    - cualquier celda < 0% → rojo claro
    """
    def _style(col):
        styles = []
        first_over_seen = False
        for v in col:
            if pd.isna(v):
                styles.append("")
                continue
            if stop_loss is not None and v <= stop_loss:
                styles.append("background-color: #b71c1c; color: white; font-weight: bold")  # rojo oscuro
            elif is_metric and (not first_over_seen) and v >= threshold:
                styles.append("background-color: #2e7d32; color: white; font-weight: bold")
                first_over_seen = True
            elif v >= 0.01:  # >= 1%
                styles.append("background-color: #c8e6c9")  # verde claro
            elif v < 0.0:
                styles.append("background-color: #ffcdd2")  # rojo claro
            else:
                styles.append("")
        return styles
    return _style


def _make_leg_exit_styler(exit_idx, exit_reason: str):
    """Estiliza una columna de % de pierna (% Call / % Put) en modo 'CALL o PUT':
    marca la celda EXACTA donde la pierna se vendió — verde oscuro si fue por su
    Umbral de ROI (señal de venta), rojo oscuro si por stop loss. El resto de las
    celdas usa el coloreo suave por valor."""
    def _style(col):
        styles = []
        for i, v in enumerate(col):
            if exit_idx is not None and i == exit_idx and exit_reason == "100%_threshold":
                styles.append("background-color: #2e7d32; color: white; font-weight: bold")  # verde oscuro = venta
            elif exit_idx is not None and i == exit_idx and exit_reason == "stop_loss":
                styles.append("background-color: #b71c1c; color: white; font-weight: bold")  # rojo oscuro = stop
            elif pd.isna(v):
                styles.append("")
            elif v >= 0.01:
                styles.append("background-color: #c8e6c9")
            elif v < 0.0:
                styles.append("background-color: #ffcdd2")
            else:
                styles.append("")
        return styles
    return _style


def _build_display_df(it: IterationResult) -> pd.DataFrame:
    tdf = it.df.copy()
    _mode = getattr(it, "mode", "both")

    # En modo "Sólo PUT" la pierna CALL no operó: forzamos % Call y Capital
    # CALL a 0 en lugar de calcular (que daría NaN por entry_premium=0/None).
    if _mode == "put_only" or not it.call_entry_premium:
        tdf["pct_call"] = 0.0
        tdf["val_call"] = 0.0
    else:
        tdf["pct_call"] = (tdf["call_px"] - it.call_entry_premium) / it.call_entry_premium
        tdf["val_call"] = it.invest_call * (1.0 + tdf["pct_call"])

    # Idem espejo: en "Sólo CALL" la pierna PUT no operó.
    if _mode == "call_only" or not it.put_entry_premium:
        tdf["pct_put"] = 0.0
        tdf["val_put"] = 0.0
    else:
        tdf["pct_put"] = (tdf["put_px"] - it.put_entry_premium) / it.put_entry_premium
        tdf["val_put"] = it.invest_put * (1.0 + tdf["pct_put"])

    # ROI real sobre la inversión total (ponderado por inversión por leg).
    _it_total_invest = it.invest_call + it.invest_put
    if _it_total_invest > 0:
        tdf["pct_total"] = (it.invest_call * tdf["pct_call"] + it.invest_put * tdf["pct_put"]) / _it_total_invest
    else:
        tdf["pct_total"] = 0.0
    tdf["val_total"] = tdf["val_call"] + tdf["val_put"]
    # Capital acum por pierna: si la pierna no operó, val_*=0 → diff/cumsum=0.
    tdf["cum_delta_call"] = tdf["val_call"].diff().fillna(0.0).cumsum()
    tdf["cum_delta_put"] = tdf["val_put"].diff().fillna(0.0).cumsum()
    # ROI en DÓLARES por pierna y total (P&L sobre la inversión de cada leg).
    #   ROI ($) CALL = invest_call × pct_call   (= Capital CALL − invest_call)
    #   ROI ($)      = ROI ($) CALL + ROI ($) PUT (= P&L total en $)
    tdf["roi_dol_call"] = it.invest_call * tdf["pct_call"]
    tdf["roi_dol_put"] = it.invest_put * tdf["pct_put"]
    tdf["roi_dol_total"] = tdf["roi_dol_call"] + tdf["roi_dol_put"]
    # Modo "CALL y PUT (Refuerzo)" (martingala): las columnas TOTAL reflejan el capital
    # MULTI-TRANCHE (no la entrada única). El motor dejó ref_roi/ref_value/ref_invested por
    # minuto; las piernas (Px/ROI Call/Put) siguen mostrando el PRIMER tranche.
    if getattr(it, "refuerzo", None) is not None and "ref_roi" in tdf.columns:
        tdf["pct_total"] = tdf["ref_roi"]
        tdf["val_total"] = tdf["ref_value"]
        tdf["roi_dol_total"] = tdf["ref_value"] - tdf["ref_invested"]
    # Timestamp → solo hora:minuto (HH:MM). Cada iteración es de un día, así que
    # el string ordena bien al clickear el header.
    _sub_min = bool((tdf["timestamp"].dt.second != 0).any())   # 30s/15s → mostrar segundos
    tdf["timestamp"] = tdf["timestamp"].dt.strftime("%H:%M:%S" if _sub_min else "%H:%M")
    # Marca con ➕ los minutos donde hubo un refuerzo (modo martingala).
    if getattr(it, "refuerzo", None) is not None:
        _tcol = tdf.columns.get_loc("timestamp")
        for _ri in it.refuerzo.get("idxs", []):
            if 0 <= _ri < len(tdf):
                tdf.iat[_ri, _tcol] = f"{tdf.iat[_ri, _tcol]} ➕"
    return tdf.rename(columns={
        "timestamp": "Minuto",
        "spot": "Spot",
        "call_px": "Px Call",
        "pct_call": "ROI (%) CALL",
        "roi_dol_call": "ROI ($) CALL",
        "val_call": "Capital CALL",
        "cum_delta_call": "Capital acum CALL",
        "put_px": "Px Put",
        "pct_put": "ROI (%) PUT",
        "roi_dol_put": "ROI ($) PUT",
        "val_put": "Capital PUT",
        "cum_delta_put": "Capital acum PUT",
        "pct_total": "ROI (%)",
        "roi_dol_total": "ROI ($)",
        "val_total": "$ Total",
    })[["Minuto", "Spot",
         "Px Call", "ROI (%) CALL", "ROI ($) CALL", "Capital CALL", "Capital acum CALL",
         "Px Put", "ROI (%) PUT", "ROI ($) PUT", "Capital PUT", "Capital acum PUT",
         "ROI (%)", "ROI ($)", "$ Total"]]


METRIC_COLUMN = {
    "total": "ROI (%)",
    "call": "ROI (%) CALL",
    "put": "ROI (%) PUT",
}

DARK_GREEN_STYLE = "background-color: #2e7d32; color: white; font-weight: bold"
# Azul para la fila del ROI MÁXIMO alcanzado (tabla de detalle por iteración).
BLUE_MAX_STYLE = "background-color: #1565c0; color: white; font-weight: bold"


def _highlight_max_roi(display_df: pd.DataFrame) -> pd.DataFrame:
    """Colorea las columnas ROI (%) y ROI ($) de la tabla minuto a minuto (ambas comparten
    fila): el MÁXIMO de los valores con ROI(%) > 0 → verde OSCURO; el MÍNIMO de los ROI(%) < 0
    → rojo OSCURO; el resto, verde/rojo claro según signo. Mismo criterio que las filas del
    heatmap. Se aplica AL FINAL del Styler para tapar el color de signo/umbral previo."""
    styles = pd.DataFrame("", index=display_df.index, columns=display_df.columns)
    _col = "ROI (%)"
    if _col not in display_df.columns:
        return styles
    _s = pd.to_numeric(display_df[_col], errors="coerce")
    _pos = _s[_s > 0]
    _neg = _s[_s < 0]
    _imax = _pos.idxmax() if not _pos.empty else None
    _imin = _neg.idxmin() if not _neg.empty else None
    for _i in display_df.index:
        _v = _s.get(_i)
        if pd.isna(_v) or _v == 0:
            continue
        if _i == _imax:
            _css = "background-color: #1b5e20; color: white; font-weight: bold"   # máx (+) → verde OSCURO
        elif _i == _imin:
            _css = "background-color: #b71c1c; color: white; font-weight: bold"   # mín (−) → rojo OSCURO
        elif _v > 0:
            _css = "background-color: #c8e6c9"   # verde claro
        else:
            _css = "background-color: #ffcdd2"   # rojo claro
        for _c in ("ROI (%)", "ROI ($)"):
            if _c in styles.columns:
                styles.loc[_i, _c] = _css
    return styles


def _max_roi_of_iteration(it) -> tuple[float, float]:
    """Pico de ROI alcanzado durante la iteración → (max_roi_pct, max_roi_dol). Se
    calcula igual que la columna ROI ($) del detalle (ponderado por inversión por pierna
    sobre it.df), así coincide con la celda azul del máximo en la tabla de detalle."""
    df = getattr(it, "df", None)
    if df is None or df.empty:
        return 0.0, 0.0
    roi = pd.Series(0.0, index=df.index)
    if it.call_entry_premium and "call_px" in df.columns:
        roi = roi + it.invest_call * (df["call_px"] / it.call_entry_premium - 1.0)
    if it.put_entry_premium and "put_px" in df.columns:
        roi = roi + it.invest_put * (df["put_px"] / it.put_entry_premium - 1.0)
    max_dol = float(roi.max()) if len(roi) else 0.0
    max_pct = (max_dol / it.invest_total) if it.invest_total else 0.0
    return max_pct, max_dol


def _style_display_df(display_df: pd.DataFrame, threshold: float, exit_metric: str, stop_loss: float, it=None):
    fmt = {
        "ROI (%) CALL": "{:+.1%}", "ROI (%) PUT": "{:+.1%}",
        "ROI ($) CALL": "{:+,.2f}", "ROI ($) PUT": "{:+,.2f}",
        "ROI (%)": "{:+.1%}", "ROI ($)": "{:+,.2f}",
        "Spot": "{:.2f}", "Px Call": "{:.2f}", "Px Put": "{:.2f}",
        "Capital CALL": "${:,.2f}", "Capital PUT": "${:,.2f}", "$ Total": "${:,.2f}",
        "Capital acum CALL": "{:+,.2f}",
        "Capital acum PUT": "{:+,.2f}",
    }
    # La columna ROI siempre lleva el rojo oscuro del stop loss
    total_is_metric = (exit_metric == "total")
    total_styler = _make_total_pct_styler(threshold, stop_loss=stop_loss, is_metric=total_is_metric)
    metric_col = METRIC_COLUMN.get(exit_metric, "ROI (%)")

    # Modos "CALL o PUT" / "(plus)" / "(Until reach ROI(%))": cada pierna marca su propia
    # celda de venta (verde oscuro). La columna ROI combinada NO marca umbral.
    if getattr(it, "mode", "") in ("call_or_put", "call_or_put_plus", "call_or_put_until_roi"):
        call_styler = _make_leg_exit_styler(
            getattr(it, "call_exit_idx", None), getattr(it, "call_exit_reason", ""))
        put_styler = _make_leg_exit_styler(
            getattr(it, "put_exit_idx", None), getattr(it, "put_exit_reason", ""))
        styler = (
            display_df.style
            .apply(call_styler, subset=["ROI (%) CALL"])
            .apply(put_styler, subset=["ROI (%) PUT"])
            .map(_color_leg_pct, subset=["ROI ($) CALL", "ROI ($) PUT", "ROI ($)"])
            .apply(_make_total_pct_styler(threshold, stop_loss=stop_loss, is_metric=False),
                   subset=["ROI (%)"])
            .apply(_highlight_max_roi, axis=None)
            .format(fmt)
        )
        return styler.set_properties(**{"text-align": "center"})

    if exit_metric == "total":
        styler = (
            display_df.style
            .map(_color_leg_pct, subset=["ROI (%) CALL", "ROI (%) PUT",
                                          "ROI ($) CALL", "ROI ($) PUT", "ROI ($)"])
            .apply(total_styler, subset=["ROI (%)"])
            .apply(_highlight_max_roi, axis=None)
            .format(fmt)
        )
    else:
        # exit_metric == "call" or "put"
        _leg_pct_cols = [c for c in ["ROI (%) CALL", "ROI (%) PUT"] if c != metric_col]
        styler = (
            display_df.style
            .map(_color_leg_pct, subset=_leg_pct_cols + ["ROI ($) CALL", "ROI ($) PUT", "ROI ($)"])
            .apply(_make_total_pct_styler(threshold, stop_loss=None, is_metric=True), subset=[metric_col])
            .apply(total_styler, subset=["ROI (%)"])
            .apply(_highlight_max_roi, axis=None)
            .format(fmt)
        )
    # Centrar las CELDAS. st.dataframe respeta text-align de las celdas vía
    # Styler, pero NO centra los headers (limitación del grid de Glide) — se
    # acepta ese trade-off a cambio de conservar ordenamiento/búsqueda al click.
    return styler.set_properties(**{"text-align": "center"})


def render_ops_report(it: IterationResult):
    """Reporte de operaciones de la iteración: contratos comprados/vendidos por
    pierna, comisión, ganancia neta y ROI. Los contratos se calculan como
    enteros = inversión // (prima_entrada × 100)."""
    # ── Modo "CALL y PUT (Refuerzo)" (martingala): reportar TODOS los tranches ──
    if getattr(it, "refuerzo", None) is not None and it.refuerzo.get("n"):
        _entries = [(it.start_dt, float(it.call_entry_premium or 0),
                     float(it.put_entry_premium or 0), "Apertura")]
        # Cada refuerzo es de UNA pierna (mismo tipo): se compra SOLO esa pierna, la otra va en 0.
        for _j, _ev in enumerate(it.refuerzo.get("events", []), start=1):
            _i = int(_ev["idx"])
            if 0 <= _i < len(it.df):
                _r = it.df.iloc[_i]
                _leg = _ev["leg"]
                _ce = float(_r["call_px"]) if _leg == "CALL" else 0.0
                _pe = float(_r["put_px"]) if _leg == "PUT" else 0.0
                _entries.append((_r["timestamp"], _ce, _pe, f"Refuerzo {_j} ({_leg})"))
        _cx, _px = float(it.call_exit_premium), float(it.put_exit_premium)
        buy_lines, buy_total, nc_tot, np_tot = [], 0.0, 0, 0
        for _ts, _ce, _pe, _lbl in _entries:
            _t = pd.Timestamp(_ts).strftime("%H:%M")
            _parts, _lc = [], 0.0
            if _ce > 0:
                _n = int(it.invest_call // (_ce * 100.0))
                if _n > 0:
                    _b = _n * _ce * 100.0; buy_total += _b; nc_tot += _n; _lc += _b
                    _parts.append(f"CALL {_n}×${_ce * 100:,.2f}")
            if _pe > 0:
                _n = int(it.invest_put // (_pe * 100.0))
                if _n > 0:
                    _b = _n * _pe * 100.0; buy_total += _b; np_tot += _n; _lc += _b
                    _parts.append(f"PUT {_n}×${_pe * 100:,.2f}")
            buy_lines.append(f"- **{_lbl}** ({_t}): {' · '.join(_parts) or 'sin contratos'} = **${_lc:,.2f}**")
        total_contracts = nc_tot + np_tot
        if total_contracts == 0:
            st.info("La inversión no alcanza para 1 contrato entero. Aumentá la inversión.")
            return
        sell_total = nc_tot * _cx * 100.0 + np_tot * _px * 100.0
        commission = COMMISSION_PER_CONTRACT * total_contracts
        net = sell_total - buy_total - commission
        roi = (net / buy_total * 100.0) if buy_total else 0.0
        _ok = "✅" if net >= 0 else "🔻"
        sell_lines = []
        if nc_tot:
            sell_lines.append(f"- **CALL**: {nc_tot} contratos a ${_cx * 100:,.2f} = **${nc_tot * _cx * 100:,.2f}**")
        if np_tot:
            sell_lines.append(f"- **PUT**: {np_tot} contratos a ${_px * 100:,.2f} = **${np_tot * _px * 100:,.2f}**")
        _split = (f" ({it.refuerzo.get('n_call', 0)} CALL, {it.refuerzo.get('n_put', 0)} PUT)"
                  if it.refuerzo.get("events") else "")
        md = [f"**🟢 Compras** — apertura @ {it.start_dt:%H:%M} + {it.refuerzo['n']} refuerzo(s){_split} · capital total ${it.invest_total:,.0f}",
              *buy_lines, f"➡ **Total compra: ${buy_total:,.2f}**", "",
              f"**🔴 Venta (cierre @ {it.end_dt:%H:%M})** — se venden TODOS los contratos de todos los tranches",
              *sell_lines, f"➡ **Total venta: ${sell_total:,.2f}**", "",
              f"**💸 Comisión** · ${COMMISSION_PER_CONTRACT:.2f}/contrato × {total_contracts} contratos = **${commission:,.2f}**", "",
              f"**Ganancia neta** · ${sell_total:,.2f} − ${buy_total:,.2f} − ${commission:,.2f} = {_ok} **${net:,.2f}**",
              f"**ROI** · (${net:,.2f} ÷ ${buy_total:,.2f}) × 100 = {_ok} **{roi:+.1f}%**"]
        st.markdown("\n".join(md))
        st.caption("Refuerzo (martingala): cada compra es un tranche CALL+PUT con la misma inversión "
                   "inicial, a su precio de entrada; al cierre se venden todos. Contratos enteros + "
                   "comisión → puede diferir levemente de la 'Ganancia total' de arriba.")
        return

    mode = getattr(it, "mode", "both")
    legs = []
    if mode != "put_only" and it.call_entry_premium > 0:
        legs.append(("CALL", it.invest_call, it.call_entry_premium, it.call_exit_premium))
    if mode != "call_only" and it.put_entry_premium > 0:
        legs.append(("PUT", it.invest_put, it.put_entry_premium, it.put_exit_premium))

    buy_lines, sell_lines = [], []
    buy_total = sell_total = 0.0
    total_contracts = 0
    for name, invest, entry, exit_ in legs:
        cost_per = entry * 100.0           # costo de 1 contrato (prima × 100)
        n = int(invest // cost_per) if cost_per > 0 else 0
        if n <= 0:
            buy_lines.append(
                f"- **{name}**: inversión ${invest:,.2f} menor a ${cost_per:,.2f}/contrato → **0 contratos**")
            continue
        buy = n * cost_per
        sell = n * exit_ * 100.0
        buy_total += buy
        sell_total += sell
        total_contracts += n
        buy_lines.append(f"- **{name}**: {n} contratos a ${cost_per:,.2f} = **${buy:,.2f}**")
        sell_lines.append(f"- **{name}**: {n} contratos a ${exit_ * 100.0:,.2f} = **${sell:,.2f}**")

    if total_contracts == 0:
        st.info("La inversión no alcanza para comprar ni 1 contrato entero. "
                "Aumentá la inversión para ver el desglose por contratos.")
        return

    commission = COMMISSION_PER_CONTRACT * total_contracts
    net = sell_total - buy_total - commission
    roi = (net / buy_total * 100.0) if buy_total else 0.0
    _ok = "✅" if net >= 0 else "🔻"

    md = [f"**🟢 Compra (apertura @ {it.start_dt:%H:%M})**", *buy_lines,
          f"➡ **Total compra: ${buy_total:,.2f}**", "",
          f"**🔴 Venta (cierre @ {it.end_dt:%H:%M})**", *sell_lines,
          f"➡ **Total venta: ${sell_total:,.2f}**", "",
          f"**💸 Comisión** · ${COMMISSION_PER_CONTRACT:.2f}/contrato × {total_contracts} contratos = **${commission:,.2f}**", "",
          f"**Ganancia neta** · ${sell_total:,.2f} − ${buy_total:,.2f} − ${commission:,.2f} = {_ok} **${net:,.2f}**",
          f"**ROI** · (${net:,.2f} ÷ ${buy_total:,.2f}) × 100 = {_ok} **{roi:+.1f}%**"]
    st.markdown("\n".join(md))
    st.caption(
        "Cálculo con contratos enteros + comisión. Puede diferir levemente de la "
        "'Ganancia total' de arriba, que usa asignación exacta en dólares sin comisión."
    )


# `_LWC_TF` y `_render_lwc_chart` viven ahora en `ui_charts.py` (importados arriba) — compartidos con
# la vista «Simulación Intradía» de Tendencia del mercado, sin duplicar el componente.


def render_iteration(it: IterationResult, ticker: str, date: str):
    # Subheader removido: la info ya está en el label del expander que envuelve esta función.

    # Warning si alguna pierna se eligió por fallback (no había prima dentro del rango).
    _fb_msgs = []
    if getattr(it, "call_fallback", False):
        _fb_msgs.append(
            f"**CALL** strike {it.call_strike:g} con prima ${it.call_entry_premium:.2f} "
            f"(fuera del rango [{it.premium_min:.2f}, {it.premium_max:.2f}])"
        )
    if getattr(it, "put_fallback", False):
        _fb_msgs.append(
            f"**PUT** strike {it.put_strike:g} con prima ${it.put_entry_premium:.2f} "
            f"(fuera del rango [{it.premium_min:.2f}, {it.premium_max:.2f}])"
        )
    if _fb_msgs:
        st.warning(
            "⚠ Sin match dentro del rango premium — se eligió el contrato más cercano:\n\n- "
            + "\n- ".join(_fb_msgs)
        )

    # Minuto de la sesión en que se ENTRÓ a la estrategia, contado desde la
    # apertura del mercado (09:30 ET). Ej.: orden a las 10:00 → minuto 30.
    _open_dt = it.start_dt.normalize() + pd.Timedelta(hours=9, minutes=30)
    _entry_min = int(round((it.start_dt - _open_dt).total_seconds() / 60.0))
    st.markdown(
        f"<div style='padding:8px 12px; background:#f0f2f6; border-radius:6px; "
        f"display:inline-block; margin-bottom:8px;'>"
        f"<b>Entrada:</b> {it.start_dt:%H:%M} (minuto {_entry_min} desde apertura 09:30)  ·  "
        f"<b>Strike Call:</b> {it.call_strike:g}  ·  "
        f"<b>Strike Put:</b> {it.put_strike:g}  ·  "
        f"<b>Spot @ entrada:</b> ${it.spot_at_start:.2f}  ·  "
        f"<code>{it.call_occ}</code> / <code>{it.put_occ}</code>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Banner PROMINENTE de refuerzos (martingala): cuántos y a qué hora. Solo si hubo ≥1.
    if getattr(it, "refuerzo", None) and it.refuerzo.get("n"):
        _rtimes = [pd.Timestamp(it.df.iloc[_ri]["timestamp"]).strftime("%H:%M")
                   for _ri in it.refuerzo.get("idxs", []) if 0 <= _ri < len(it.df)]
        _bsplit = (f" ({it.refuerzo.get('n_call', 0)} CALL, {it.refuerzo.get('n_put', 0)} PUT)"
                   if it.refuerzo.get("events") else "")
        st.markdown(
            f"<div style='padding:8px 12px; background:#fff3cd; border:1px solid #ffe08a; "
            f"border-radius:6px; display:inline-block; margin-bottom:8px;'>"
            f"➕ <b>{it.refuerzo['n']} refuerzo(s)</b>{_bsplit} (martingala, mismo tipo) — capital total "
            f"<b>${it.invest_total:,.0f}</b>. Comprados a las: <b>{', '.join(_rtimes) or '—'}</b> "
            f"<span style='color:#7a6000'>· también marcados con ➕ en la tabla minuto a minuto.</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # Bid/Ask/Spread + tier de rango por pierna (si hay NBBO disponible).
    def _leg_quote_line(label, bid, ask, spread, tier):
        if bid is None or ask is None:
            return f"<b>{label}:</b> <span style='color:#999'>sin quote</span>"
        _tier_badge = {
            "optimo": "<span style='color:#2e7d32'>óptimo</span>",
            "extended": "<span style='color:#e65100'>extendido</span>",
            "fallback": "<span style='color:#b71c1c'>fallback</span>",
            "value": "<span style='color:#1565c0'>valor</span>",
            "itm_first": "<span style='color:#1565c0'>1-ITM</span>",
            "itm_fallback": "<span style='color:#b71c1c'>1-ITM (fallback)</span>",
        }.get(tier, tier or "")
        return (f"<b>{label}:</b> bid ${bid:.2f} / ask ${ask:.2f} · "
                f"spread <b>${spread:.2f}</b> · {_tier_badge}")

    _ql = []
    if getattr(it, "mode", "both") != "put_only":
        _ql.append(_leg_quote_line("CALL", it.call_bid, it.call_ask, it.call_spread, it.call_range_tier))
    if getattr(it, "mode", "both") != "call_only":
        _ql.append(_leg_quote_line("PUT", it.put_bid, it.put_ask, it.put_spread, it.put_range_tier))
    if _ql:
        st.markdown(
            "<div style='padding:6px 12px; background:#fafafa; border:1px solid #eee; "
            "border-radius:6px; display:inline-block; margin-bottom:8px; font-size:0.85rem;'>"
            + "  &nbsp;|&nbsp;  ".join(_ql) + "</div>",
            unsafe_allow_html=True,
        )

    mc = st.columns(6)
    pct_call_iter = (it.call_exit_premium / it.call_entry_premium - 1.0) if it.call_entry_premium else 0.0
    pct_put_iter = (it.put_exit_premium / it.put_entry_premium - 1.0) if it.put_entry_premium else 0.0
    mc[0].metric("Inversión iter.", f"${it.invest_total:,.2f}")
    mc[1].metric("Ganancia CALL", f"${it.gain_call:+,.2f}", delta=f"{pct_call_iter:+.1%}")
    mc[2].metric("Ganancia PUT", f"${it.gain_put:+,.2f}", delta=f"{pct_put_iter:+.1%}")

    # Ganancia total con fondo verde/rojo claro según signo
    # (verde = mismo del banner success de Streamlit)
    if it.gain_total > 0:
        it_bg, it_dc, it_arrow = "rgba(33, 195, 84, 0.1)", "#2e7d32", "▲"
    elif it.gain_total < 0:
        it_bg, it_dc, it_arrow = "#ffcdd2", "#b71c1c", "▼"
    else:
        it_bg, it_dc, it_arrow = "#f0f2f6", "#555", "–"
    it_delta_txt = (
        f"{it_arrow} {abs(it.gain_total / it.invest_total):.1%}"
        if it.invest_total else ""
    )
    mc[3].markdown(
        f"<div style='border:1px solid rgba(49,51,63,0.2); border-radius:0.5rem; "
        f"padding:0.85rem 1rem; background-color:{it_bg};'>"
        f"<div style='font-size:0.85rem; font-weight:bold; color:rgba(49,51,63,0.65); "
        f"margin-bottom:0.35rem;'>Ganancia total</div>"
        f"<div style='font-size:1.5rem; font-weight:600; line-height:1.15;'>${it.gain_total:+,.2f}</div>"
        f"<div style='font-size:0.85rem; color:{it_dc}; margin-top:0.25rem;'>{it_delta_txt}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    mc[4].metric("Combined % exit", f"{it.pnl_pct_combined:+.1%}")
    mc[5].metric("Capital acumulado", f"${it.invest_total + it.gain_total:,.2f}")

    # ── Detalle PESADO bajo demanda (pedido UX 2026-07-05): la info preliminar de arriba se
    # muestra SIEMPRE; las pestañas (Operaciones · Strikes · Gráfico · Tabla minuto a minuto)
    # — que cotizan cadenas por red y arman gráficos — solo al tocar «Ver detalles». Con
    # decenas de iteraciones en pantalla, la lista queda liviana y se abre lo que interesa.
    _key_suffix = f"{date}_{it.iteration}"
    _det_key = f"iter_det_{ticker}_{_key_suffix}"
    if not st.session_state.get(_det_key):
        if st.button("🔍 Ver detalles de esta señal", key=f"btn_{_det_key}"):
            st.session_state[_det_key] = True
            st.rerun()
        return
    if st.button("➖ Ocultar detalles", key=f"btn_hide_{_det_key}"):
        st.session_state.pop(_det_key, None)
        st.rerun()

    # Detalle de la iteración en PESTAÑAS: el render va DENTRO del expander de la iteración
    # y Streamlit no soporta expanders ANIDADOS (daban un scroll cortado que no dejaba ver
    # la tabla minuto a minuto). Las pestañas no anidan → se ve todo bien.
    display_df = _build_display_df(it)
    _step_sec = 60
    if getattr(it, "df", None) is not None and len(it.df) > 1:
        _d = it.df["timestamp"].diff().dropna().dt.total_seconds().round()
        if len(_d):
            _step_sec = int(_d.median())
    if _step_sec <= 20:
        _tbl_title = "📋 Tabla 15 segundos a 15 segundos"
    elif _step_sec <= 45:
        _tbl_title = "📋 Tabla 30 segundos a 30 segundos"
    elif _step_sec <= 90:
        _tbl_title = "📋 Tabla minuto a minuto"
    else:
        _tbl_title = f"📋 Tabla (paso {_step_sec // 60} min)"
    _tab_ops, _tab_chain, _tab_chart, _tab_tbl = st.tabs([
        "📑 Operaciones",
        f"Strikes ({len(it.call_probes)}C · {len(it.put_probes)}P)",
        "📈 Gráfico",
        _tbl_title,
    ])
    with _tab_ops:
        render_ops_report(it)

    # La tabla minuto a minuto se construye ANTES que la cadena (que cotiza por red la 1ª
    # vez): como st.tabs ejecuta todos los tabs en orden de código, ponerla acá hace que
    # aparezca rápido aunque la cadena esté cotizando en su propio tab.
    with _tab_tbl:
        total_rows = len(display_df)
        st.caption(f"{total_rows} fila(s)" + (" · scrolleá DENTRO de la tabla (rueda sobre la "
                   "tabla) para verlas todas." if total_rows > 9 else "."))
        _styled_show = _style_display_df(
            display_df, it.exit_threshold_pct, getattr(it, "exit_metric", "total"),
            getattr(it, "stop_loss_pct", 1.0), it=it,
        )
        # Alto ADAPTATIVO: se ajusta a la cantidad de filas (sin ranuras vacías cuando hay
        # pocas), con tope ~360px (~10 filas) para que las tablas largas (390 filas) entren
        # en el viewport y el scroll interno alcance la última fila. SELECCIÓN = "Ver Gráfico".
        _mev = st.dataframe(_styled_show, use_container_width=True,
                            height=min(360, 38 + 35 * max(1, total_rows)),
                            on_select="rerun", selection_mode="multi-row",
                            key=f"mtable_{_key_suffix}")
        # "Temporalidad del gráfico": DEBAJO de la tabla, justo encima del gráfico.
        _tf = st.selectbox(
            "Temporalidad del gráfico", list(_LWC_TF.keys()), index=2, key=f"tf_{_key_suffix}",
            help="Marcá una o más FILAS (minutos) con la casilla de la izquierda → se dibuja "
                 "el gráfico de esa zona (mismo ticker/fecha) a esta temporalidad.")
        _msel = sorted(_mev.selection.rows) if (_mev and _mev.selection) else []
        for _ri in _msel[:4]:   # hasta 4 gráficos a la vez (evita recargar de más)
            _hora = str(display_df.iloc[_ri]["Minuto"])
            st.markdown(f"**📈 Ver Gráfico — {ticker} · {date} · {_hora} · {_tf}**")
            _render_lwc_chart(get_downloader(api_key), ticker, date, _hora, _tf,
                              f"{_key_suffix}_{_ri}")
        if len(_msel) > 4:
            st.caption(f"Marcaste {len(_msel)} filas; muestro 4 gráficos para no recargar.")

    with _tab_chain:
        # Cadena de opciones estilo thinkorswim: CALLS (izq) · Strike (centro) · PUTS
        # (der). Orden: Last, Bid, Ask, Spread | Strike | Bid, Ask, Spread, Last.
        # Spread = Ask - Bid. Strikes ascendentes; fila azul = el contrato elegido.
        _it_mode = getattr(it, "mode", "both")
        _show_call = _it_mode != "put_only"
        _show_put = _it_mode != "call_only"

        # Cotizamos el NBBO de TODA la cadena (no solo los strikes del rango) y lo CACHEAMOS
        # por iteración en session_state: se pide UNA sola vez (la 1ª vez que se abre este
        # detalle) y los reruns/reaperturas lo leen del cache → instantáneo. Como además la
        # tabla minuto a minuto se renderiza ANTES (más arriba en el código), este cotizado
        # no demora lo que importa. Los quotes ya pedidos en el backtest (in-range) no se repiten.
        _qcache_key = f"chain_extraq_{_key_suffix}"
        if _qcache_key in st.session_state:
            _extra_q = st.session_state[_qcache_key]
        else:
            _extra_q = {}
            _probes_all = ((list(it.call_probes) if _show_call else [])
                           + (list(it.put_probes) if _show_put else []))
            _need = [p for p in _probes_all
                     if getattr(p, "bid", None) is None and getattr(p, "occ", "")]
            if _need:
                _dlq = get_downloader(api_key)

                def _q1(p):
                    try:
                        q = _robust_quote(_dlq, p.occ, date, it.start_dt,
                                          ref_premium=p.opening_premium)
                        return p.occ, q.get("bid"), q.get("ask")
                    except Exception:  # noqa: BLE001
                        return p.occ, None, None
                with st.spinner(f"Cotizando la cadena ({len(_need)} strikes)… solo la 1ª vez"):
                    with ThreadPoolExecutor(max_workers=8) as _exq:
                        for _occ, _b, _a in _exq.map(_q1, _need):
                            _extra_q[_occ] = (_b, _a)
            st.session_state[_qcache_key] = _extra_q

        def _leg_df(probes, s):
            def _ba(p):
                # bid/ask del probe (rango) o, si falta, el cotizado on-demand (_extra_q).
                b, a = getattr(p, "bid", None), getattr(p, "ask", None)
                if b is None and getattr(p, "occ", "") in _extra_q:
                    b, a = _extra_q[p.occ]
                return b, a
            rows = []
            for p in probes:
                b, a = _ba(p)
                # Spread = |Ask - Bid| × 100 (costo del spread por contrato, en $).
                _spv = abs(a - b) * 100.0 if (b is not None and a is not None) else None
                rows.append({
                    "Strike": p.strike,
                    f"{s} Last": p.opening_premium,
                    f"{s} Bid": b,
                    f"{s} Ask": a,
                    f"{s} Spread": _spv,
                })
            return pd.DataFrame(rows).set_index("Strike") if rows else pd.DataFrame()

        _cdf = _leg_df(it.call_probes, "CALL") if _show_call else pd.DataFrame()
        _pdf = _leg_df(it.put_probes, "PUT") if _show_put else pd.DataFrame()
        if not _cdf.empty and not _pdf.empty:
            _chain = _cdf.join(_pdf, how="outer")
        elif not _cdf.empty:
            _chain = _cdf
        else:
            _chain = _pdf

        if _chain.empty:
            st.caption("Sin contratos probados.")
        else:
            _chain = _chain.sort_index().reset_index()
            _cc = ["CALL Last", "CALL Bid", "CALL Ask", "CALL Spread"]
            _pc = ["PUT Bid", "PUT Ask", "PUT Spread", "PUT Last"]
            _order = ([c for c in _cc if c in _chain.columns] + ["Strike"]
                      + [c for c in _pc if c in _chain.columns])
            _chain = _chain[_order]

            _sel_c = getattr(it, "call_strike", None) if _show_call else None
            _sel_p = getattr(it, "put_strike", None) if _show_put else None
            _spot = getattr(it, "spot_at_start", None)
            _ITM = "background-color: #cfe2ff"   # azul claro = in the money
            _OTM = "background-color: #fff3cd"   # amarillo claro = out of the money

            def _hl_chain(row):
                k = float(row["Strike"])
                _ic = _sel_c is not None and abs(k - float(_sel_c)) < 1e-9
                _ip = _sel_p is not None and abs(k - float(_sel_p)) < 1e-9
                out = []
                for col in row.index:
                    style = ""
                    if col.startswith("CALL ") and _spot is not None:
                        style = _ITM if k < _spot else _OTM      # CALL ITM: strike < spot
                        if _ic:
                            style += "; font-weight: bold"
                    elif col.startswith("PUT ") and _spot is not None:
                        style = _ITM if k > _spot else _OTM      # PUT ITM: strike > spot
                        if _ip:
                            style += "; font-weight: bold"
                    elif col == "Strike" and (_ic or _ip):
                        style = "font-weight: bold"
                    out.append(style)
                return out

            def _fmt2nz(v):
                # 2 decimales SIN cero a la izquierda: 0.03 -> .03, -0.03 -> -.03.
                if pd.isna(v):
                    return "—"
                s = f"{float(v):.2f}"
                if s.startswith("0."):
                    return s[1:]
                if s.startswith("-0."):
                    return "-" + s[2:]
                return s
            _fmt = {c: _fmt2nz for c in _chain.columns if c != "Strike"}
            _fmt["Strike"] = "{:.2f}"
            _styled = (_chain.style
                       .apply(_hl_chain, axis=1)
                       .format(_fmt, na_rep="—")
                       .hide(axis="index")
                       # Headers fijos: al scrollear vertical el thead queda pegado arriba.
                       # box-shadow = borde inferior que NO se pierde con border-collapse.
                       .set_table_styles([{
                           "selector": "thead th",
                           "props": [("position", "sticky"), ("top", "0"),
                                     ("background-color", "#eef1f6"), ("z-index", "3"),
                                     ("box-shadow", "inset 0 -1px 0 #c9ced6")],
                       }]))
            st.caption("⬅ CALLS · Strike · PUTS ➡   ·   azul claro = ITM · amarillo claro = OTM · negrita = contrato elegido")
            # Render HTML con table-layout:fixed → las columnas se reparten para
            # AJUSTARSE al panel (entran todas, sin scroll horizontal). Ancho completo:
            # con 9 columnas, a <50% los headers quedan ilegibles (apilados).
            _tbl_html = _styled.set_table_attributes(
                'style="width:100%; table-layout:fixed; border-collapse:collapse; '
                'font-size:0.8rem; text-align:right"'
            ).to_html()
            st.markdown(
                f'<div style="max-height:430px; overflow:auto">{_tbl_html}</div>',
                unsafe_allow_html=True,
            )

    with _tab_chart:
        st.plotly_chart(build_chart(it), use_container_width=True, key=f"chart_iter_{_key_suffix}")

    dc = st.columns(2)
    csv_bytes = display_df.to_csv(index=False).encode("utf-8")
    dc[0].download_button(
        f"CSV iter {it.iteration}",
        csv_bytes,
        file_name=f"{ticker}_{date}_iter{it.iteration}_{it.start_dt:%H%M}.csv",
        mime="text/csv",
        key=f"csv_iter_{_key_suffix}",
    )
    buf = io.BytesIO()
    excel_df = display_df.copy()
    if pd.api.types.is_datetime64_any_dtype(excel_df["Minuto"]) and excel_df["Minuto"].dt.tz is not None:
        excel_df["Minuto"] = excel_df["Minuto"].dt.tz_localize(None)
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame([{
            "iteration": it.iteration,
            "start": str(it.start_dt), "end": str(it.end_dt), "exit_reason": it.exit_reason,
            "spot_at_start": it.spot_at_start,
            "call_strike": it.call_strike, "call_occ": it.call_occ,
            "call_entry_premium": it.call_entry_premium, "call_exit_premium": it.call_exit_premium,
            "put_strike": it.put_strike, "put_occ": it.put_occ,
            "put_entry_premium": it.put_entry_premium, "put_exit_premium": it.put_exit_premium,
            "invest_call": it.invest_call, "invest_put": it.invest_put,
            "gain_call": it.gain_call, "gain_put": it.gain_put, "gain_total": it.gain_total,
        }]).to_excel(xw, sheet_name="summary", index=False)
        excel_df.to_excel(xw, sheet_name="minute_table", index=False)
    dc[1].download_button(
        f"Excel iter {it.iteration}",
        buf.getvalue(),
        file_name=f"{ticker}_{date}_iter{it.iteration}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"xlsx_iter_{_key_suffix}",
    )


# ============================================================================
# Render: header + totals + status + per-iteration sections
# ============================================================================

# Modo SEÑALES (multi-iteración): cada señal = 1 "sesión" rica, REUSANDO render_iteration
# (Totales + detalle por iteración), igual que un backtest manual.
def render_roi_heatmap(records: list, key_prefix: str = "roi_hm", coll_exit_thr: float | None = None) -> None:
    """Heatmap ROI por ticker × intervalo de tiempo, para UNA fecha. `records` = lista de dicts
    con 'ticker', 'fecha', 'hora', 'iteration'. Fila TOTAL agregada arriba (ΣROI$ / Σinvertido)
    con umbral de ROI colectivo que resalta los mejores intervalos. Reusable: señales + manual."""
    _oks = [r for r in records if r.get("iteration") is not None]
    with st.expander(f"🗓️ ROI por ticker e intervalo de tiempo ({len(_oks)})", expanded=False):
        # Totales del DÍA seleccionado — MISMO cálculo que «Totales del backtest» (Σ invest_total /
        # Σ gain_total de las señales de ese día). La fecha se lee del estado (el selectbox está más
        # abajo) → refleja la última selección. Va arriba del todo, encima de la leyenda.
        _sel_date_prev = st.session_state.get(f"{key_prefix}_date")
        if _sel_date_prev and _sel_date_prev != "(todas)":
            _drecs = [r for r in _oks if r.get("fecha") == _sel_date_prev]
            _dinv = sum(float(getattr(r["iteration"], "invest_total", 0.0) or 0.0) for r in _drecs)
            _dgain = sum(float(getattr(r["iteration"], "gain_total", 0.0) or 0.0) for r in _drecs)
            if _dinv > 0:
                st.markdown(f"**Totales del día {_sel_date_prev}** · {len(_drecs)} señal(es)")
                _mc1, _mc2 = st.columns(2)
                _mc1.metric("Inversión total", f"${_dinv:,.2f}")
                _mc2.metric("Ganancia total", f"${_dgain:+,.2f}",
                            f"{_dgain / _dinv * 100.0:+.1f}%")
        st.caption("Para una FECHA específica: cada celda = **ROI(%) / ROI($)** del ticker en ese "
                   "instante · **CLOSED** = sin posición · 🟢 ganancia / 🔴 pérdida. La fila **TOTAL** "
                   "(arriba) agrega todos los tickers por intervalo; verde OSCURO = mejor (máx +) · "
                   "rojo OSCURO = peor (mín −) · **🟡 amarillo** = primer intervalo donde el TOTAL "
                   "alcanza el **Umbral de ROI colectivo de las Condiciones de salida** (ahí dispara "
                   "el corte). El «Filtro de columnas» de abajo es aparte — solo visual, no toca el backtest.")
        _hdates = sorted({r.get("fecha") for r in _oks if r.get("fecha")})
        if not _hdates:
            st.caption("Sin fechas para mostrar.")
            return
        _hc0, _hc1, _hc2 = st.columns([1, 1, 2])
        _hdate = _hc0.selectbox("Fecha", ["(todas)"] + _hdates, key=f"{key_prefix}_date")
        if _hdate == "(todas)":
            # RESUMEN por ticker en TODO el backtest (sin detalle minuto a minuto): ROI total
            # de cada ticker = Σ ganancias (final - inicial) / Σ invertido, sobre todas las fechas.
            _bytk: dict = {}
            for r in _oks:
                it = r["iteration"]
                _tk = r.get("ticker") or "?"
                try:
                    _inv = float(getattr(it, "invest_total", 0.0) or 0.0)
                    # ROI($) REALIZADA = último valor del timeline (mismo cálculo que el heatmap;
                    # initial/final_total son prima POR ACCIÓN, no $, así que NO se restan directo).
                    _gn = float(_build_display_df(it)["ROI ($)"].to_numpy()[-1])
                except Exception:
                    continue
                _d = _bytk.setdefault(_tk, {"inv": 0.0, "gain": 0.0, "n": 0})
                _d["inv"] += _inv
                _d["gain"] += _gn
                _d["n"] += 1
            if not _bytk:
                st.caption("Sin resultados para resumir.")
                return
            _trows = [{"Ticker": _t, "Señales": _v["n"], "Invertido": _v["inv"],
                       "ROI ($)": _v["gain"],
                       "ROI (%)": (_v["gain"] / _v["inv"] * 100.0) if _v["inv"] else 0.0}
                      for _t, _v in sorted(_bytk.items(), key=lambda kv: kv[1]["gain"], reverse=True)]
            _tinv = sum(_v["inv"] for _v in _bytk.values())
            _tgain = sum(_v["gain"] for _v in _bytk.values())
            _sum_df = pd.DataFrame(
                [{"Ticker": "TOTAL", "Señales": sum(_v["n"] for _v in _bytk.values()),
                  "Invertido": _tinv, "ROI ($)": _tgain,
                  "ROI (%)": (_tgain / _tinv * 100.0) if _tinv else 0.0}] + _trows)
            st.caption(f"Resumen por ticker · **TODAS** las fechas — {len(_oks)} señal(es) con resultado. "
                       "ROI($) = suma de ganancias del ticker · ROI(%) = ROI$ ÷ invertido. "
                       "🟢 verde oscuro = mejor ticker · 🔴 rojo oscuro = peor.")

            def _style_sum(_df):
                _sty = pd.DataFrame("", index=_df.index, columns=_df.columns)
                _vals = [(i, _df.loc[i, "ROI ($)"]) for i in _df.index if _df.loc[i, "Ticker"] != "TOTAL"]
                _pos = [x for x in _vals if x[1] > 0]
                _neg = [x for x in _vals if x[1] < 0]
                _imax = max(_pos, key=lambda x: x[1])[0] if _pos else None
                _imin = min(_neg, key=lambda x: x[1])[0] if _neg else None
                for i in _df.index:
                    _is_tot = _df.loc[i, "Ticker"] == "TOTAL"
                    _b = "font-weight:bold; background-color:#eef;" if _is_tot else ""
                    for c in _df.columns:
                        _sty.loc[i, c] = _b
                    if not _is_tot:
                        _v = _df.loc[i, "ROI ($)"]
                        if i == _imax:
                            _cell = "background-color:#1b5e20; color:white; font-weight:bold;"
                        elif i == _imin:
                            _cell = "background-color:#b71c1c; color:white; font-weight:bold;"
                        elif _v > 0:
                            _cell = "background-color:#c8e6c9;"
                        elif _v < 0:
                            _cell = "background-color:#ffcdd2;"
                        else:
                            _cell = ""
                        _sty.loc[i, "ROI ($)"] = _cell
                        _sty.loc[i, "ROI (%)"] = _cell
                return _sty

            st.dataframe(
                _sum_df.style.apply(_style_sum, axis=None).format(
                    {"Invertido": "${:,.2f}", "ROI ($)": "${:+,.2f}", "ROI (%)": "{:+.1f}%"}),
                hide_index=True, use_container_width=True)
            st.divider()
            st.markdown("**Detalle minuto a minuto** — todas las fechas (1 fila por señal · "
                        "columnas = hora del día):")
        # FILTRO local del heatmap (independiente de las Condiciones de salida): alimenta el checkbox
        # «Solo columnas con ROI colectivo > filtro». NO es una condición de salida.
        _thr = float(_hc1.number_input(
            "Filtro de columnas — ROI colectivo ≥ (%)", value=5.0, step=1.0, key=f"{key_prefix}_thr",
            help="Filtro de VISUALIZACIÓN (no toca el backtest): con «Solo columnas > filtro» deja "
                 "solo los intervalos cuyo ROI colectivo (fila TOTAL) supera este valor."))
        _hres = _hc2.radio("Rango temporal de verificación de ROI (%)", ["15 segundos", "30 segundos", "1 minuto"],
                           index=2, horizontal=True, key=f"{key_prefix}_res")
        _hsec = {"15 segundos": 15, "30 segundos": 30, "1 minuto": 60}[_hres]
        _oks_date = _oks if _hdate == "(todas)" else [r for r in _oks if r.get("fecha") == _hdate]
        _all_tks = sorted({r.get("ticker") for r in _oks_date if r.get("ticker")})
        # Re-siembra el filtro al cambiar la FECHA → por defecto TODOS los tickers que operaron ese
        # día (la columna "Tickers" del Resumen por día). Sin esto el multiselect quedaba pegado en
        # la selección anterior por su `key`.
        _tks_sig = (key_prefix, str(_hdate))
        if st.session_state.get(f"{key_prefix}_tks_sig") != _tks_sig:
            st.session_state[f"{key_prefix}_tks_sig"] = _tks_sig
            st.session_state.pop(f"{key_prefix}_tks", None)
        _ft1, _ft2 = st.columns([3, 2])
        _sel_tks = _ft1.multiselect("Filtrar tickers (filas)", _all_tks, default=_all_tks,
                                    key=f"{key_prefix}_tks")
        _only_pos = _ft2.checkbox("Solo columnas con ROI% > 0", value=False, key=f"{key_prefix}_pos")
        _only_thr = _ft2.checkbox(
            "Solo columnas con ROI colectivo (%) > filtro", value=False,
            key=f"{key_prefix}_thrcol",
            help="Deja solo los intervalos cuyo ROI agregado (fila TOTAL) SUPERA el «Filtro de "
                 "columnas» de arriba.")
        _full_range = _ft2.checkbox(
            "🕘 Rango horario completo (en una fecha única)", value=False,
            key=f"{key_prefix}_fullrange",
            help="En UNA sola fecha, extiende las columnas al MISMO rango horario que la vista "
                 "«(todas)» (rellena CLOSED los minutos sin posición). Por defecto la fecha única solo "
                 "muestra los minutos con alguna posición abierta. No aplica a «(todas)».")
        # Si el multiselect quedó vacío (transición al cambiar de fecha, o deselección total),
        # caemos a TODOS los tickers del día → la tabla nunca se "esconde" por un filtro vacío.
        _oks_date = [r for r in _oks_date if r.get("ticker") in (_sel_tks or _all_tks)]

        def _hfloor(_t):
            return ((_t.hour * 3600 + _t.minute * 60 + _t.second) // _hsec) * _hsec

        def _hlbl(_s):
            _h, _r = divmod(_s, 3600)
            _m, _x = divmod(_r, 60)
            return f"{_h:02d}:{_m:02d}:{_x:02d}" if _hsec < 60 else f"{_h:02d}:{_m:02d}"

        def _grange(_recs):
            """(min entrada, max salida) en segundos-del-día (a _hsec) sobre TODOS los registros —
            para extender la grilla de una fecha única al rango global (= vista «todas»)."""
            _es, _xs = [], []
            for _r in _recs:
                _it = _r.get("iteration")
                try:
                    _tc = _it.df["timestamp"]
                    if len(_tc):
                        _es.append(_hfloor(pd.to_datetime(_tc.iloc[0])))
                        _xs.append(_hfloor(pd.to_datetime(_tc.iloc[-1])))
                except Exception:
                    continue
            return (min(_es), max(_xs)) if _es else (None, None)

        def _build_hits(_recs, _day_labels: bool) -> list:
            """(label, entrada_s, salida_s, celdas, invertido, es_pierna, refuerzos, venta_s)
            por FILA del heatmap: la fila combinada de cada señal + una fila por PIERNA
            (· CALL / · PUT) con su ROI propio minuto a minuto. Las filas de pierna NO suman
            al TOTAL (la combinada ya representa a la señal en la cartera — sumarlas
            duplicaría). `refuerzos` = [(segundo, pierna), …] de la martingala: marca ⟳n en
            las celdas del combinado (donde viven los lotes extra) y el label de la pierna
            reforzada. `venta_s` (solo piernas) = segundo en que la pierna se vendió SOLA por
            su propio trigger (umbral/stop de pierna) — desde ahí su valor queda CONGELADO;
            None si acompañó a la posición hasta el final. Con `_day_labels` (seccionado por
            fecha) el label es local al día."""
            _cnt: dict = {}
            for _r in _recs:
                _cnt[_r.get("ticker")] = _cnt.get(_r.get("ticker"), 0) + 1
            _hs = []
            for r in sorted(_recs, key=lambda x: (x.get("ticker") or "", x.get("hora") or "")):
                it = r["iteration"]
                try:
                    _disp = _build_display_df(it)
                    _ts = pd.to_datetime(it.df["timestamp"])
                    _pc = _disp["ROI (%)"].to_numpy()
                    _dl = _disp["ROI ($)"].to_numpy()
                except Exception:  # noqa: BLE001 — una señal sin timeline no rompe el heatmap
                    continue
                if len(_ts) == 0:
                    continue
                _tk = r.get("ticker") or "?"
                _lab = (_tk if _cnt.get(_tk, 0) == 1 else f"{_tk} · {r.get('hora')}") \
                    if _day_labels else f"{_tk} · {r.get('hora')}"
                _inv = float(getattr(it, "invest_total", 0.0) or 0.0)
                _e_s, _x_s = _hfloor(_ts.iloc[0]), _hfloor(_ts.iloc[-1])
                # Eventos de REFUERZO (martingala por pierna) → (segundo, pierna) ordenados.
                # El motor los registra en it.refuerzo["events"] con el índice del minuto.
                _ref_ev = []
                try:
                    for _ev in ((getattr(it, "refuerzo", None) or {}).get("events") or []):
                        _ei = int(_ev.get("idx", -1))
                        if 0 <= _ei < len(_ts):
                            _ref_ev.append((_hfloor(_ts.iloc[_ei]),
                                            str(_ev.get("leg") or "?")))
                except Exception:  # noqa: BLE001 — sin refuerzos legibles no se marca nada
                    _ref_ev = []
                _ref_ev.sort()

                def _celdas(_pcs, _dls):
                    _cs = {}
                    for _t, _p, _d in zip(_ts, _pcs, _dls):
                        _cs[_hfloor(_t)] = (float(_p), float(_d))
                    return _cs

                _hs.append((_lab, _e_s, _x_s, _celdas(_pc, _dl), _inv, False, _ref_ev, None))
                # Filas por PIERNA (si la señal la tiene y el display trae sus columnas). La
                # pierna REFORZADA lleva ⟳n en su nombre: su fila muestra SOLO el contrato
                # original (los lotes de refuerzo viven en la fila combinada del ticker). La
                # pierna VENDIDA por su propio trigger lleva ✔HH:MM (desde ahí va congelada).
                for _leg, _sufx in (("CALL", "CALL"), ("PUT", "PUT")):
                    if not getattr(it, f"{_leg.lower()}_entry_premium", None):
                        continue
                    _cp, _cd = f"ROI (%) {_sufx}", f"ROI ($) {_sufx}"
                    if _cp not in _disp.columns or _cd not in _disp.columns:
                        continue
                    _n_rl = sum(1 for _, _lg in _ref_ev if _lg == _leg)
                    _vend_s = None
                    try:
                        _xi = getattr(it, f"{_leg.lower()}_exit_idx", None)
                        _xr = str(getattr(it, f"{_leg.lower()}_exit_reason", "") or "")
                        if (_xi is not None and 0 <= int(_xi) < len(_ts)
                                and _xr and _xr != "session_end"):
                            _vend_s = _hfloor(_ts.iloc[int(_xi)])
                    except Exception:  # noqa: BLE001 — sin dato de venta no se marca nada
                        _vend_s = None
                    _lab_leg = (f"{_lab} · {_leg}" + (f" ⟳{_n_rl}" if _n_rl else "")
                                + (f" ✔{_hlbl(_vend_s)}" if _vend_s is not None else ""))
                    try:
                        _hs.append((_lab_leg, _e_s, _x_s,
                                    _celdas(_disp[_cp].to_numpy(), _disp[_cd].to_numpy()),
                                    _inv, True, [], _vend_s))
                    except Exception:  # noqa: BLE001
                        continue
            return _hs

        # SECCIONADO POR DÍA (pedido UX 2026-07-05): en «(todas)», una tabla por FECHA — cada
        # día con su propia fila TOTAL (que es exactamente la base del corte colectivo de ESE
        # día). La grilla horaria y el slider son GLOBALES para que las secciones alineen.
        _seccionado = (_hdate == "(todas)")
        if _seccionado:
            _grupos = [(_f, [r for r in _oks_date if r.get("fecha") == _f])
                       for _f in sorted({str(r.get("fecha")) for r in _oks_date})]
        else:
            _grupos = [(None, _oks_date)]
        _hits_grp = [(_f, _recs, _build_hits(_recs, _day_labels=_seccionado))
                     for _f, _recs in _grupos]
        _hits_grp = [(_f, _recs, _hs) for _f, _recs, _hs in _hits_grp if _hs]
        if not _hits_grp:
            st.caption("Sin timeline para esa fecha.")
            return
        _all_hits = [h for _f, _recs, _hs in _hits_grp for h in _hs]
        # Índices posicionales (h[1]=entrada, h[2]=salida) — robusto a que la tupla del hit
        # crezca (ya pasó: el 7º campo de refuerzos rompió el unpack fijo de 6).
        _gmin = min(h[1] for h in _all_hits)
        _gmax = max(h[2] for h in _all_hits)
        if _full_range and not _seccionado:
            _ga, _gb = _grange(_oks)   # rango GLOBAL (todas las fechas) → columnas como en «(todas)»
            if _ga is not None:
                _gmin, _gmax = min(_gmin, _ga), max(_gmax, _gb)
        _grid = list(range(_gmin, _gmax + _hsec, _hsec))
        _cols_full = [_hlbl(_s) for _s in _grid]
        # Slider de horas ÚNICO (rango global): aplica a TODAS las secciones por igual.
        _cols = list(_cols_full)
        if len(_cols_full) > 1:
            _hr_sig = (str(_hdate), _hres, tuple(_sel_tks), _cols_full[0], _cols_full[-1],
                       len(_cols_full), bool(_only_pos), bool(_only_thr))
            if st.session_state.get(f"{key_prefix}_hrange_sig") != _hr_sig:
                st.session_state[f"{key_prefix}_hrange_sig"] = _hr_sig
                st.session_state.pop(f"{key_prefix}_hrange", None)
            _tr = st.select_slider("Rango de horas (columnas)", options=_cols_full,
                                   value=(_cols_full[0], _cols_full[-1]),
                                   key=f"{key_prefix}_hrange")
            _cols = _cols_full[_cols_full.index(_tr[0]):_cols_full.index(_tr[1]) + 1]
        # Guardián de tamaño DESPUÉS del slider (antes cortaba ANTES de mostrarlo — sin salida
        # para el usuario — y contaba todas las columnas aunque se mirara una ventana chica).
        # La carga real de dibujo: filas de la PEOR sección × columnas VISIBLES (cada sección
        # es una tabla aparte), más un tope global para el conjunto. El tope viejo de 25k se
        # disparaba solo porque las filas · CALL/· PUT triplicaron el conteo global.
        _max_sec = max((len(_hs) for _f, _recs, _hs in _hits_grp), default=0)
        if _max_sec * len(_cols) > 25000 or len(_all_hits) * len(_cols) > 300000:
            st.warning(f"⚠️ {len(_all_hits)} filas × {len(_cols)} intervalos visibles — muy "
                       "pesado para dibujar. Achicá el **rango de horas** con el slider de "
                       "arriba, filtrá **tickers**, o mirá una fecha específica.")
            return

        def _render_grid(_hits, _cols_vista) -> None:
            """UNA sección del heatmap (un día, o la fecha única): filas + su fila TOTAL,
            filtros de columnas locales, estilo y tabla."""
            _tot_dol = {c: 0.0 for c in _cols_full}   # Σ ROI$ por columna (solo celdas válidas)
            _tot_inv = {c: 0.0 for c in _cols_full}   # Σ invertido por columna (tickers abiertos)
            _tot_n = {c: 0 for c in _cols_full}
            _hay_ref = any(_h[6] for _h in _hits)     # ¿algún refuerzo en la sección? → leyenda
            _hay_vta = any(_h[7] is not None for _h in _hits)   # ¿alguna pierna vendida sola?
            _hdata = []
            for _label, _e, _xe, _cells, _inv, _es_pierna, _ref_ev, _vend_s in _hits:
                _row = {"Ticket": _label}
                _last = None
                for _s in _grid:
                    _c = _hlbl(_s)
                    # Fuera de la operación → CLOSED (no suma). Una PIERNA ya vendida, ídem:
                    # desde el minuto SIGUIENTE a su venta muestra CLOSED — su resultado final
                    # queda visible (con ✔) solo en el minuto de la venta (pedido UX 2026-07-06;
                    # antes el valor congelado se repetía hasta el final y parecía viva).
                    if (_s < _e or _s > _xe
                            or (_es_pierna and _vend_s is not None and _s > _vend_s)):
                        _row[_c] = "CLOSED"
                        _last = None
                    else:                              # abierta → forward-fill
                        if _s in _cells:
                            _last = _cells[_s]
                        if _last is not None:
                            _p, _d = _last
                            _txt = f"{_p*100:.1f}% / ${_d:,.2f}"
                            # ⟳n = refuerzos YA disparados a este minuto — solo en la fila
                            # combinada, que es donde entran los lotes extra (base que crece).
                            if not _es_pierna and _ref_ev:
                                _nr = sum(1 for _rs, _ in _ref_ev if _rs <= _s)
                                if _nr:
                                    _txt += f" ⟳{_nr}"
                            # ✔ = minuto exacto en que la pierna se vendió por SU trigger
                            # (desde ahí su valor queda congelado — ya está bancado).
                            if _es_pierna and _vend_s is not None and _s == _vend_s:
                                _txt += " ✔"
                            _row[_c] = _txt
                            if not _es_pierna:         # las piernas NO suman al TOTAL (duplicarían)
                                _tot_dol[_c] += _d
                                _tot_inv[_c] += _inv
                                _tot_n[_c] += 1
                        else:
                            _row[_c] = "CLOSED"
                _hdata.append(_row)
            # Fila TOTAL: ROI% agregado = Σ ROI$ / Σ invertido (excluye CLOSED) · ROI$ = Σ ROI$.
            # En el modo seccionado es el TOTAL de ESE día = la base del corte colectivo del día.
            _total = {"Ticket": "TOTAL"}
            for _c in _cols_full:
                if _tot_n[_c] > 0 and _tot_inv[_c] > 0:
                    _agg = _tot_dol[_c] / _tot_inv[_c] * 100.0
                    _total[_c] = f"{_agg:.1f}% / ${_tot_dol[_c]:,.2f}"
                else:
                    _total[_c] = "CLOSED"
            _cg = list(_cols_vista)
            # Filtro — solo columnas con ROI% > 0 en alguna fila de ticker (de ESTA sección):
            if _only_pos:
                def _col_pos(_c):
                    for _row in _hdata:
                        _v = _row.get(_c, "")
                        if isinstance(_v, str) and "%" in _v:
                            try:
                                if float(_v.split("%")[0]) > 0:
                                    return True
                            except Exception:
                                pass
                    return False
                _cg = [_c for _c in _cg if _col_pos(_c)]
            # Filtro — solo columnas cuyo TOTAL (de ESTA sección) supera el filtro visual:
            if _only_thr:
                _cg = [_c for _c in _cg
                       if (_tot_n.get(_c, 0) > 0 and _tot_inv.get(_c, 0) > 0
                           and (_tot_dol[_c] / _tot_inv[_c] * 100.0) > _thr)]
            if not _cg:
                st.caption("Ninguna columna cumple los filtros en esta sección.")
                return
            _full = pd.DataFrame([_total] + _hdata)[["Ticket"] + _cg]

            def _style(df):
                sty = pd.DataFrame("", index=df.index, columns=df.columns)
                # 🟡 Corte por ROI colectivo: PRIMER intervalo donde el TOTAL (%) ALCANZA el
                # Umbral de ROI colectivo de las CONDICIONES DE SALIDA (coll_exit_thr) → ahí
                # dispara el corte. (Es el umbral REAL del backtest, NO el filtro visual.)
                _trig_col = None
                if coll_exit_thr is not None:
                    _tot_ix = [_i for _i in df.index if df.at[_i, "Ticket"] == "TOTAL"]
                    if _tot_ix:
                        for _c in _cg:
                            _tv = df.at[_tot_ix[0], _c]
                            if isinstance(_tv, str) and "%" in _tv:
                                try:
                                    if float(_tv.split("%")[0]) >= coll_exit_thr:
                                        _trig_col = _c
                                        break
                                except Exception:
                                    pass
                # Por CADA fila (TOTAL y tickers): el MÁXIMO de sus positivos → verde OSCURO;
                # el MÍNIMO de sus negativos → rojo OSCURO; el resto, claro según signo.
                for _i in df.index:
                    _is_tot = (df.at[_i, "Ticket"] == "TOTAL")
                    _bold = "; font-weight:bold" if _is_tot else ""
                    sty.at[_i, "Ticket"] = ("background-color:#37474f; color:white; "
                                            "font-weight:bold" if _is_tot else "")
                    _vals = []
                    for _c in _cg:
                        _v = df.at[_i, _c]
                        if isinstance(_v, str) and "%" in _v:
                            try:
                                _vals.append(float(_v.split("%")[0]))
                            except Exception:
                                pass
                    _rmax = max([v for v in _vals if v > 0], default=None)
                    _rmin = min([v for v in _vals if v < 0], default=None)
                    for _c in _cg:
                        _v = df.at[_i, _c]
                        if not isinstance(_v, str) or "%" not in _v:
                            sty.at[_i, _c] = "color:#9aa0a6"      # CLOSED → gris tenue
                            continue
                        try:
                            _p = float(_v.split("%")[0])
                        except Exception:
                            continue
                        if _rmax is not None and _p == _rmax:       # máx (+) fila → verde OSCURO
                            sty.at[_i, _c] = ("background-color:#1b5e20; color:white; "
                                              "font-weight:bold")
                        elif _rmin is not None and _p == _rmin:     # mín (−) fila → rojo OSCURO
                            sty.at[_i, _c] = ("background-color:#b71c1c; color:white; "
                                              "font-weight:bold")
                        elif _p > 0:
                            sty.at[_i, _c] = "background-color:#c8e6c9" + _bold   # verde claro
                        elif _p < 0:
                            sty.at[_i, _c] = "background-color:#ffcdd2" + _bold   # rojo claro
                        # 🟡 la celda del corte colectivo (fila TOTAL) manda sobre el signo
                        if _is_tot and _trig_col is not None and _c == _trig_col:
                            sty.at[_i, _c] = "background-color:#ffeb3b; color:#000; font-weight:bold"
                return sty

            st.dataframe(_full.style.apply(_style, axis=None), use_container_width=True,
                         hide_index=True, height=min(560, 60 + 35 * (len(_hdata) + 1)))
            if _hay_ref:
                st.caption(
                    "⟳n = **refuerzos** acumulados hasta ese minuto (martingala por pierna: al "
                    "caer una pierna al umbral de refuerzo se compran MÁS contratos de esa "
                    "misma pierna, al ask). La fila del ticker incluye esos lotes extra — su "
                    "base invertida crece en cada ⟳ — mientras que las filas · CALL / · PUT "
                    "(la reforzada lleva ⟳n en su nombre) muestran SOLO el contrato original. "
                    "Por eso los $ de las piernas no suman exactamente el combinado, y los % "
                    "del ticker usan una base mayor tras cada refuerzo.")
            if _hay_vta:
                st.caption(
                    "✔ = la pierna se **vendió sola** en ese minuto (por su propio umbral/stop) "
                    "— el nombre de la fila lleva ✔HH:MM y la celda del minuto de venta muestra "
                    "su **resultado final bancado**; desde el minuto siguiente la fila pasa a "
                    "CLOSED. La pierna hermana sigue viva hasta su propia salida o el cierre, y "
                    "la fila combinada del ticker sigue incluyendo lo ya bancado.")

        _WD_HM = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        for _f_hm, _recs_hm, _hs_hm in _hits_grp:
            if _seccionado:
                try:
                    _wd_hm = " · " + _WD_HM[pd.Timestamp(str(_f_hm)).weekday()]
                except Exception:  # noqa: BLE001
                    _wd_hm = ""
                # Stats del día para el encabezado: ROI final de la cartera del día + el máx
                # alcanzado por cada pierna entre TODAS las señales del día.
                _g_d = sum(float(getattr(_r["iteration"], "gain_total", 0.0) or 0.0)
                           for _r in _recs_hm)
                _i_d = sum(float(getattr(_r["iteration"], "invest_total", 0.0) or 0.0)
                           for _r in _recs_hm)
                _roi_d = (_g_d / _i_d * 100.0) if _i_d else 0.0
                _mxs = [_max_leg_roi_vals(_r["iteration"]) for _r in _recs_hm]
                _mxc = [c for c, p in _mxs if c is not None]
                _mxp = [p for c, p in _mxs if p is not None]
                _mx_txt = " · máx " + " / ".join(
                    ([f"CALL {max(_mxc):+.1f}%"] if _mxc else [])
                    + ([f"PUT {max(_mxp):+.1f}%"] if _mxp else [])) if (_mxc or _mxp) else ""
                st.markdown(f"##### 📅 {_f_hm}{_wd_hm} · ROI: {_roi_d:+.1f}% / "
                            f"${_g_d:+,.2f}{_mx_txt}")
            _render_grid(_hs_hm, _cols)


def _render_signals_session(rs):
    results = rs.get("sig_results", [])
    oks = [r for r in results if r.get("iteration") is not None]
    errs = [r for r in results if r.get("iteration") is None]
    _h1, _h2 = st.columns([4, 1])
    _h1.subheader("🔬 Backtest de señales — resultados")
    if _h2.button("🧹 Limpiar", key="sig_clear_render", use_container_width=True):
        st.session_state.pop("replay", None)
        st.rerun()
    if rs.get("sig_elapsed") is not None:
        st.caption(f"⏱️ Completado en {rs['sig_elapsed']:0.1f}s · "
                   f"{rs.get('sig_workers', 1)} en paralelo")
    _combo_rs = rs.get("sig_pb_combo") or {}
    if _combo_rs:
        _escs_rs = _combo_rs.get("escenarios") or {}
        st.caption("⚙️ **Config por día del playbook** · 🧩 combinación "
                   f"**«{_combo_rs.get('nombre') or _combo_rs.get('combination') or '?'}»** · "
                   + " · ".join(f"{_d}→{_escs_rs[_d]}"
                                for _d in ("Lun", "Mar", "Mié", "Jue", "Vie")
                                if _escs_rs.get(_d)))

    # Totales RICOS — el MISMO helper que el backtest por rango (vista idéntica:
    # días procesados, inversión, ganancia, capital final, ganadores/perdedores,
    # win rate, razones de salida + panel de riesgo).
    render_batch_totals(oks, total_days=len(results),
                        title="💼 Totales del backtest", show_risk=True)

    # Iteraciones que el motor RECHAZÓ (sin contrato, hora/tipo inválidos, etc.). Antes se
    # calculaban (errs) pero NO se mostraban → el usuario veía "0 procesados" sin saber por qué.
    if errs:
        _all_failed = not oks
        st.warning(f"⚠️ **{len(errs)} de {len(results)}** iteración(es) no generaron operación."
                   + (" Por eso los totales están en cero." if _all_failed else ""))
        with st.expander(f"🔎 Ver por qué ({len(errs)} sin operación)", expanded=_all_failed):
            _cat: dict = {}
            for _e in errs:
                _msg = str(_e.get("error", "error"))
                if "Sin contrato" in _msg:
                    _k = "Sin contrato — ningún strike 0DTE pasó el filtro de spread/rango"
                elif "incompleta" in _msg or "inválid" in _msg:
                    _k = "Iteración inválida (Tipo u hora)"
                else:
                    _k = _msg[:70]
                _cat[_k] = _cat.get(_k, 0) + 1
            for _k, _v in sorted(_cat.items(), key=lambda x: -x[1]):
                st.markdown(f"- **{_v}×** — {_k}")
            st.dataframe(
                pd.DataFrame([{"Ticker": _e.get("ticker", "?"), "Fecha": _e.get("fecha", ""),
                               "Hora": _e.get("hora", ""), "Tipo": _e.get("tipo", ""),
                               "Motivo": str(_e.get("error", "error"))} for _e in errs]),
                use_container_width=True, hide_index=True)
            st.caption("💡 **«Sin contrato»** casi siempre es **spread demasiado ancho** (opciones "
                       "ilíquidas). Ajustá el filtro en **Configuración → §1** o usá tickers más "
                       "líquidos (QQQ/SPY/IWM). No es un error del backtest — es la compuerta "
                       "protegiéndote de un fill malo.")

    # Totales AGRUPADOS por semana / ticker / grupo (sector) — igual que el rango.
    if oks:
        _grp_by = st.radio(
            "📂 Agrupar totales por",
            ["General", "Por semana", "Por ticker", "Por grupo (sector)"],
            horizontal=True, key="sig_group_by",
            help="Recalcula inversión, ganancia, capital final, ganadores/perdedores "
                 "y win rate para cada grupo (semana, ticker o sector).",
        )
        if _grp_by == "Por semana":
            render_grouped_totals(
                oks,
                lambda r: "Sem. " + pd.Timestamp(r["fecha"]).to_period("W").start_time.strftime("%Y-%m-%d"),
                "Semana (lun)")
        elif _grp_by == "Por ticker":
            render_grouped_totals(oks, lambda r: r.get("ticker", "—"), "Ticker")
        elif _grp_by == "Por grupo (sector)":
            _ti_grp = load_ticker_info()

            def _grp_of(r):
                _i = _ti_grp.get((r.get("ticker") or "").upper().strip()) or {}
                return _i.get("bloque_sector") or _i.get("indice") or "Otros"

            render_grouped_totals(oks, _grp_of, "Grupo")

    _pb_skipped = rs.get("sig_skipped_playbook") or []
    if _pb_skipped:
        _skpb = ", ".join(sorted({f"{s.get('ticker')} {s.get('fecha')}"
                                  for s in _pb_skipped})[:12])
        if len(_pb_skipped) > 12:
            _skpb += f" … (+{len(_pb_skipped) - 12})"
        st.caption(f"🗓 Se saltaron **{len(_pb_skipped)}** fila(s) **por el playbook** — caen en "
                   f"días NO OPERAR / sin config operable (decisión del veredicto, no un "
                   f"problema de datos). No cuentan como error ni en los totales: {_skpb}")
    _skipped = rs.get("sig_skipped") or []
    if _skipped:
        _sk = ", ".join(f"{s.get('ticker')} {s.get('fecha')}" for s in _skipped[:12])
        if len(_skipped) > 12:
            _sk += f" … (+{len(_skipped) - 12})"
        st.caption(f"ℹ️ Se saltaron **{len(_skipped)}** señal(es) sin **0DTE** ese día "
                   f"(activá *Auto-DTE* arriba para operarlas al vencimiento más cercano). "
                   f"No cuentan como error ni en los totales: {_sk}")

    # Distribución temporal de operaciones exitosas (baseline 09:30 → hora de cierre real).
    render_temporal_distribution([r["iteration"] for r in oks],
                                 entrada=pd.Timestamp("09:30").time(),
                                 chart_key="temporal_chart_sig")

    # --- 🗓️ Heatmap: ROI por ticker × intervalo de tiempo (función reusable, con fila TOTAL) ---
    render_roi_heatmap(oks, "sig_heatmap",
                       coll_exit_thr=(st.session_state.get("sig_coll_thr")
                                      if st.session_state.get("sig_coll_exit") else None))

    # Resumen por día — agrega TODAS las señales de cada fecha (1 fila por día).
    if oks:
        _by_day: dict = {}
        for r in oks:
            _it = r["iteration"]
            _e = _by_day.setdefault(r["fecha"], {"gain": 0.0, "invest": 0.0,
                                                 "n": 0, "win": 0, "tks": set()})
            _e["gain"] += _it.gain_total
            _e["invest"] += _it.invest_total
            _e["n"] += 1
            _e["win"] += 1 if _it.gain_total > 0 else 0
            _e["tks"].add(r.get("ticker", ""))
        # Trazabilidad: escenario del playbook con el que corrió cada fecha (por día de semana).
        _escs_day = ((rs.get("sig_pb_combo") or {}).get("escenarios")) or {}
        _WD_DAY = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

        def _esc_de(_f):
            try:
                return _escs_day.get(_WD_DAY[pd.Timestamp(str(_f)).weekday()], "—")
            except Exception:  # noqa: BLE001
                return "—"
        _drows, _cum = [], 0.0
        for _d in sorted(_by_day):
            _e = _by_day[_d]
            _cum += _e["gain"]
            _drows.append({
                "Fecha": _d,
                **({"Escenario": _esc_de(_d)} if _escs_day else {}),
                "Ops": _e["n"], "Ganancia": _e["gain"],
                "ROI %": (_e["gain"] / _e["invest"] * 100.0) if _e["invest"] else 0.0,
                "Ganadores": f"{_e['win']}/{_e['n']}",
                "Ganancia acumulada": _cum,
                "Tickers": ", ".join(sorted(t for t in _e["tks"] if t)),
            })

        def _gcol(v):
            if not isinstance(v, (int, float)) or pd.isna(v):
                return ""
            return ("background-color: #c8e6c9" if v > 0
                    else ("background-color: #ffcdd2" if v < 0 else ""))

        with st.expander(f"📋 Días con resultados ({len(_drows)})", expanded=False):
            st.markdown("### Resumen por día")
            _styled_day = (pd.DataFrame(_drows).style
                           .map(_gcol, subset=["Ganancia", "Ganancia acumulada"])
                           .format({"Ganancia": "${:+,.0f}", "ROI %": "{:+.1f}%",
                                    "Ganancia acumulada": "${:+,.0f}"}))
            st.dataframe(_styled_day, use_container_width=True, hide_index=True,
                         height=min(440, 38 + 35 * max(1, len(_drows))))

    st.markdown("#### 🔍 Detalle por señal")
    # --- Filtro de filas: Todas / ROI ≥ 0 / ROI < 0 / por motivo de salida (con contadores) ---
    _det = sorted(oks, key=lambda x: (x.get("fecha") or "", x.get("hora") or "",
                                      x.get("ticker") or ""))

    def _reason_cell(r):
        _rk = r["iteration"].exit_reason
        return f"{_REASON_ICONS.get(_rk, '•')} {_REASON_LABELS.get(_rk, _rk)}"

    _n_pos = sum(1 for r in _det if r["iteration"].gain_total >= 0)
    _n_neg = sum(1 for r in _det if r["iteration"].gain_total < 0)
    _reason_counts: dict = {}
    for r in _det:
        _c = _reason_cell(r)
        _reason_counts[_c] = _reason_counts.get(_c, 0) + 1
    # umbral/stop/cierre van SIEMPRE (aunque tengan 0); wrong_direction/overnight solo si aparecen.
    _reason_opts = []
    for _rk, _always in (("100%_threshold", True), ("stop_loss", True), ("session_end", True),
                         ("wrong_direction", False), ("weak_confirmation", False),
                         ("overnight_1dte", False)):
        _cell = f"{_REASON_ICONS.get(_rk, '•')} {_REASON_LABELS.get(_rk, _rk)}"
        if _always or _reason_counts.get(_cell, 0) > 0:
            _reason_opts.append(_cell)
    _filter_options = ["Todas", "ROI ≥ 0", "ROI < 0"] + _reason_opts
    if st.session_state.get("sig_roi_filter") not in _filter_options:
        st.session_state.pop("sig_roi_filter", None)
    _sig_filter = st.radio(
        "Filtrar filas", options=_filter_options, index=0, horizontal=True, key="sig_roi_filter",
        format_func=lambda o: {
            "Todas": f"Todas ({len(_det)})",
            "ROI ≥ 0": f"ROI ≥ 0 ({_n_pos})",
            "ROI < 0": f"ROI < 0 ({_n_neg})",
        }.get(o, f"{o} ({_reason_counts.get(o, 0)})"),
    )
    if _sig_filter == "ROI ≥ 0":
        _det = [r for r in _det if r["iteration"].gain_total >= 0]
    elif _sig_filter == "ROI < 0":
        _det = [r for r in _det if r["iteration"].gain_total < 0]
    elif _sig_filter in _reason_opts:
        _det = [r for r in _det if _reason_cell(r) == _sig_filter]
    if not _det:
        st.caption("No hay señales que cumplan el filtro seleccionado.")
    # Info PRELIMINAR siempre visible (pedido UX 2026-07-05): la parte liviana de
    # render_iteration (entrada, strikes, quotes, métricas) se renderiza directo dentro del
    # expander; lo PESADO (Operaciones/Strikes/Gráfico/Tabla) queda detrás del botón
    # «🔍 Ver detalles de esta señal» que gestiona render_iteration internamente.
    _trows = []                                           # tabla ordenable post-detalle
    for _i, r in enumerate(_det):
        it = r["iteration"]
        _reason = _REASON_LABELS.get(it.exit_reason, it.exit_reason)
        if it.gain_total >= 0:
            _icon, _gp = ":green[▲]", f":green[**${it.gain_total:+,.2f}**]"
        else:
            _icon, _gp = ":red[▼]", f":red[**${it.gain_total:+,.2f}**]"
        _roi_pct = (it.gain_total / it.invest_total) if it.invest_total else 0.0
        _pct_part = (f":green[▲ {abs(_roi_pct):.1%}]" if it.gain_total >= 0
                     else f":red[▼ {abs(_roi_pct):.1%}]")
        _ref_part = (f"  ·  ➕ {it.refuerzo['n']} refuerzo(s)"
                     if getattr(it, "refuerzo", None) and it.refuerzo["n"] else "")
        _title = (f"{_icon} {r['ticker']} {r['tipo']}  ·  {r['fecha']} {r['hora']} → "
                  f"{it.end_dt:%H:%M} ({_op_dur(it)})  ·  "
                  f"{_reason}  ·  Ganancia: {_gp} ({_pct_part}){_max_leg_rois(it)}{_ref_part}")
        _auto = (len(_det) == 1)                          # 1 sola señal → se abre con detalle
        if _auto:
            # misma clave que usa render_iteration para su compuerta de detalle
            st.session_state.setdefault(f"iter_det_{r['ticker']}_{r['fecha']}_{it.iteration}",
                                        True)
        with st.expander(_title, expanded=_auto):
            st.markdown(f"**{r['ticker']} — {r['fecha']}  ·  0 DTE  ·  Ventana 09:30–16:00**")
            render_iteration(it, r["ticker"], r["fecha"])
        _mx_call, _mx_put = _max_leg_roi_vals(it)
        _trows.append({
            "Res": "▲" if it.gain_total >= 0 else "▼",
            "Ticker": r["ticker"], "Tipo": r["tipo"], "Fecha": r["fecha"],
            "Entrada": r["hora"], "Salida": f"{it.end_dt:%H:%M}", "Duración": _op_dur(it),
            "Motivo": _reason,
            "Ganancia $": round(float(it.gain_total), 2),
            "ROI %": round(_roi_pct * 100.0, 1),
            "Máx CALL %": (round(_mx_call, 1) if _mx_call is not None else None),
            "Máx PUT %": (round(_mx_put, 1) if _mx_put is not None else None),
            "Refuerzos": int(it.refuerzo["n"]) if getattr(it, "refuerzo", None) else 0,
        })

    # ── Tabla ordenable de señales (pedido UX 2026-07-05): las columnas del header de cada
    # resultado, en números crudos → clic en cualquier encabezado ordena de verdad. Respeta
    # el filtro de arriba (muestra lo mismo que la lista de expanders).
    if _trows:
        st.markdown("### 📊 Tabla de señales (clic en una columna para ordenar)")
        st.dataframe(
            pd.DataFrame(_trows), use_container_width=True, hide_index=True,
            height=min(38 + 35 * len(_trows), 500),
            column_config={
                "Ganancia $": st.column_config.NumberColumn(format="$%.2f"),
                "ROI %": st.column_config.NumberColumn(format="%.1f%%"),
                "Máx CALL %": st.column_config.NumberColumn(
                    format="%.1f%%", help="ROI máximo que alcanzó la pierna CALL mientras "
                                          "la posición estuvo abierta."),
                "Máx PUT %": st.column_config.NumberColumn(
                    format="%.1f%%", help="ROI máximo que alcanzó la pierna PUT mientras "
                                          "la posición estuvo abierta."),
            })
    if errs:
        with st.expander(f"❌ Señales sin resultado ({len(errs)})", expanded=False):
            for r in errs:
                st.markdown(f"**{r.get('ticker', '?')} {r.get('tipo', '')} "
                            f"{r.get('fecha', '')} {r.get('hora', '')}** — "
                            f"{r.get('error', 'error')}")


if replay_state.get("mode") == "signals":
    _render_signals_session(replay_state)
    st.stop()

ticker_str = (", ".join(replay_state["tickers"])
              if replay_state.get("tickers") else replay_state["ticker"])
_mode = replay_state.get("mode", "single")

if _mode == "range":
    # ========================================================================
    # Range / batch mode — 1 iteración por día hábil
    # ========================================================================
    day_runs = replay_state["day_runs"]
    successful = [r for r in day_runs if r.get("iteration") is not None]
    failed = [r for r in day_runs if r.get("iteration") is None]

    st.subheader(
        f"{ticker_str}  ·  Backtest {replay_state['date_start']} → {replay_state['date_end']}  ·  "
        f"Ventana {replay_state['time_start']:%H:%M}–{replay_state['time_end']:%H:%M}  ·  "
        f"Orden @ {replay_state['order_time']:%H:%M}"
    )

    _elapsed = replay_state.get("elapsed_sec")
    if _elapsed is not None:
        _wk = int(replay_state.get("workers", 1))
        st.caption(
            f"⏱️ Backtest completado en **{_elapsed:0.1f}s** "
            f"({_wk} iteraci{'ón' if _wk == 1 else 'ones'} en paralelo · "
            f"{len(day_runs)} días con resultado)."
        )

    # Panel de totales — usa el mismo helper que se llama en vivo durante el
    # batch, así garantizamos que la vista preliminar y la final sean idénticas.
    render_batch_totals(
        day_runs,
        total_days=len(day_runs),
        title="💼 Totales del backtest",
        show_risk=True,
    )

    # --- Totales AGRUPADOS: recalcula las métricas por semana / ticker / grupo (sector). ---
    if successful:
        _grp_by = st.radio(
            "📂 Agrupar totales por",
            ["General", "Por semana", "Por ticker", "Por grupo (sector)"],
            horizontal=True, key="batch_group_by",
            help="Recalcula días procesados, inversión, ganancia, capital final, "
                 "ganadores/perdedores y win rate para cada grupo (semana, ticker o sector).",
        )
        if _grp_by == "Por semana":
            render_grouped_totals(
                successful,
                lambda r: "Sem. " + pd.Timestamp(r["date"]).to_period("W").start_time.strftime("%Y-%m-%d"),
                "Semana (lun)")
        elif _grp_by == "Por ticker":
            render_grouped_totals(successful, lambda r: r.get("ticker", "—"), "Ticker")
        elif _grp_by == "Por grupo (sector)":
            _ti_grp = load_ticker_info()

            def _grp_of(r):
                _i = _ti_grp.get((r.get("ticker") or "").upper().strip()) or {}
                return _i.get("bloque_sector") or _i.get("indice") or "Otros"

            render_grouped_totals(successful, _grp_of, "Grupo")
    _n_holiday = int(replay_state.get("skipped_holiday", 0))
    if _n_holiday:
        st.caption(
            f"🏖️ Se saltaron **{_n_holiday}** (ticker × día) sin sesión — **feriado / fin de "
            f"semana** (mercado cerrado, no hay data). Ej.: Juneteenth (19-jun). No cuentan como "
            f"error ni en los totales."
        )
    _n_skipped = int(replay_state.get("skipped_no0dte", 0))
    if _n_skipped:
        st.caption(
            f"ℹ️ Se saltaron **{_n_skipped}** días sin 0DTE (ticker weekly: solo los "
            f"viernes vencen el mismo día). No cuentan como error ni en los totales."
        )
    _n_skip_1dte = int(replay_state.get("skipped_1dte", 0))
    if _n_skip_1dte:
        st.caption(
            f"🌙 DTE=1 (overnight): se saltaron **{_n_skip_1dte}** días sin **día hábil "
            f"siguiente** con datos (último(s) día(s) del rango o vencimiento futuro). "
            f"No cuentan como error ni en los totales."
        )

    # Distribución temporal de operaciones exitosas (tiempo a salida) — modo RANGO.
    render_temporal_distribution(
        [r.get("iteration") for r in day_runs],
        entrada=replay_state["order_time"], chart_key="temporal_chart_range",
    )

    # --- 🗓️ Heatmap: ROI por ticker × intervalo de tiempo (con fila TOTAL) — modo RANGO ---
    _hm_recs = []
    for r in successful:
        _it = r.get("iteration")
        if _it is None:
            continue
        try:
            _h = pd.to_datetime(_it.df["timestamp"]).iloc[0].strftime("%H:%M")
        except Exception:
            _h = ""
        _hm_recs.append({"ticker": r.get("ticker"), "fecha": r.get("date"),
                         "hora": _h, "iteration": _it})
    render_roi_heatmap(_hm_recs, "manual_heatmap")

    # --- Resumen COMBINADO por día (solo con >1 ticker): la ganancia de cada día
    #     sumando TODOS los tickers seleccionados (1 fila por fecha). ---
    _tk_list = replay_state.get("tickers") or [replay_state.get("ticker")]
    if len(_tk_list) > 1 and successful:
        _by_day: dict = {}
        for r in successful:
            _it = r["iteration"]
            _e = _by_day.setdefault(r["date"], {"gain": 0.0, "invest": 0.0, "n": 0, "win": 0, "tks": []})
            _e["gain"] += _it.gain_total
            _e["invest"] += _it.invest_total
            _e["n"] += 1
            _e["win"] += 1 if _it.gain_total > 0 else 0
            _e["tks"].append(r.get("ticker", ""))
        _drows, _cum = [], 0.0
        for _d in sorted(_by_day):
            _e = _by_day[_d]
            _cum += _e["gain"]
            _drows.append({
                "Fecha": _d, "Ops": _e["n"],
                "Ganancia": _e["gain"],
                "ROI %": (_e["gain"] / _e["invest"] * 100.0) if _e["invest"] else 0.0,
                "Ganadores": f"{_e['win']}/{_e['n']}",
                "Ganancia acumulada": _cum,
                "Tickers": ", ".join(_e["tks"]),
            })
        _ddf = pd.DataFrame(_drows)

        def _gcol(v):
            if not isinstance(v, (int, float)) or pd.isna(v):
                return ""
            return ("background-color: #c8e6c9" if v > 0
                    else ("background-color: #ffcdd2" if v < 0 else ""))

        with st.expander(f"📅 Resumen combinado por día ({len(_drows)} días · {len(_tk_list)} tickers)",
                         expanded=False):
            st.caption("Ganancia de cada día **sumando todos los tickers** seleccionados. "
                       "1 fila por fecha; ordenado cronológicamente.")
            _styled_day = (_ddf.style
                           .map(_gcol, subset=["Ganancia", "Ganancia acumulada"])
                           .format({"Ganancia": "${:+,.0f}", "ROI %": "{:+.1f}%",
                                    "Ganancia acumulada": "${:+,.0f}"}))
            st.dataframe(
                _styled_day, use_container_width=True, hide_index=True,
                height=min(440, 38 + 35 * max(1, len(_drows))),
                column_config={"Tickers": st.column_config.TextColumn("Tickers", width="large")},
            )

    # ------------------------------------------------------------------
    # Días con resultados — tabla + descargas dentro de un expander
    # ------------------------------------------------------------------
    selected_dates: list[str] = []
    with st.expander(f"📋 Días con resultados ({len(successful)})", expanded=False):
        st.markdown("### Resumen por día")
        if successful:
            rows = []
            cum_gain = 0.0
            for r in successful:
                it = r["iteration"]
                day_roi = (it.gain_total / it.invest_total) if it.invest_total else 0.0
                _max_roi_pct, _max_roi_dol = _max_roi_of_iteration(it)
                cum_gain += it.gain_total
                # Texto completo del fallback (se usa como contenido de la celda
                # para que Streamlit muestre el detalle al hacer hover sobre ⚠).
                _fb_bits = []
                if it.call_fallback:
                    _fb_bits.append(
                        f"CALL strike {it.call_strike:g} con prima ${it.call_entry_premium:.2f} "
                        f"(fuera del rango [{it.premium_min:.2f}, {it.premium_max:.2f}])"
                    )
                if it.put_fallback:
                    _fb_bits.append(
                        f"PUT strike {it.put_strike:g} con prima ${it.put_entry_premium:.2f} "
                        f"(fuera del rango [{it.premium_min:.2f}, {it.premium_max:.2f}])"
                    )
                _fb_text = ""
                if _fb_bits:
                    _fb_text = (
                        "⚠ Sin match dentro del rango premium — se eligió el contrato más cercano: "
                        + "; ".join(_fb_bits)
                    )
                # Datos de predicción + params aplicados (solo presentes en runs
                # del nuevo batch loop con predicción por día). Para day_runs viejos
                # sin estas keys → caemos en defaults vacíos.
                _pred = r.get("prediction") or {}
                _params = r.get("day_params") or {}
                _pred_prob = _pred.get("probability")
                _pred_label = _pred.get("label") or ""
                _pred_str = (
                    f"{int(_pred_prob)}% {_pred_label}"
                    if _pred_prob is not None else "—"
                )
                # Tipo de operación COMPLETO (ej. "CALL y PUT (Refuerzo)"); fallback al modo
                # base para runs viejos sin la key "tipo".
                _mode_str = _params.get("tipo") or {
                    "call_only": "Sólo CALL",
                    "put_only":  "Sólo PUT",
                    "both":      "CALL+PUT",
                }.get(_params.get("mode"), "—")

                # Razón como icono + texto: la celda lleva ambos; con width="small"
                # el grid lo trunca al icono y muestra el texto completo al hacer hover.
                _reason_full = _REASON_LABELS.get(it.exit_reason, it.exit_reason)
                _reason_cell = f"{_REASON_ICONS.get(it.exit_reason, '•')} {_reason_full}"

                # Máx/mín de la prima alcanzada por cada pierna durante la iteración.
                _mode_it = getattr(it, "mode", "both")
                _cpx = it.df["call_px"] if "call_px" in it.df else None
                _ppx = it.df["put_px"] if "put_px" in it.df else None
                if _mode_it != "put_only" and _cpx is not None and len(_cpx):
                    _call_mm = f"${float(_cpx.min()):.2f} – ${float(_cpx.max()):.2f}"
                else:
                    _call_mm = "—"
                if _mode_it != "call_only" and _ppx is not None and len(_ppx):
                    _put_mm = f"${float(_ppx.min()):.2f} – ${float(_ppx.max()):.2f}"
                else:
                    _put_mm = "—"

                rows.append({
                    "Ticker": r.get("ticker", ""),
                    "Fecha": r["date"],
                    "Hora de entrada": it.start_dt.strftime("%H:%M"),   # hora REAL de compra (post-ventana)
                    "Hora de salida": it.end_dt.strftime("%H:%M"),
                    "Razón": _reason_cell,
                    "Refuerzos": (it.refuerzo["n"] if getattr(it, "refuerzo", None) else 0),
                    # === Columnas nuevas de Predicción Apertura ===
                    "Predicción": _pred_str,
                    "Operación": _mode_str,
                    "CALL %": _params.get("call_alloc", 50),
                    "PUT %": _params.get("put_alloc", 50),
                    "Umbral ROI": _params.get("roi_threshold", 10),
                    # === Existentes ===
                    "Strike Call": it.call_strike,
                    "Strike Put": it.put_strike,
                    # Rango de prima (mín – máx) que alcanzó cada pierna.
                    "CALL mín–máx": _call_mm,
                    "PUT mín–máx": _put_mm,
                    "Spot @ entrada": it.spot_at_start,
                    "Inversión": it.invest_total,
                    # Las 3 métricas de ROI van juntas y en este orden:
                    # ROI (%), ROI ($), ROI ($) acumulado.
                    "ROI (%)": day_roi,
                    "ROI ($)": it.gain_total,
                    "Max ROI (%)": _max_roi_pct,
                    "Max ROI ($)": _max_roi_dol,
                    "ROI ($) acumulado": cum_gain,
                    "Fallback": _fb_text,
                })
            summary_df = pd.DataFrame(rows)

            # Si NINGÚN día tuvo fallback (columna toda vacía), no mostrar la
            # columna. El column_config para "Fallback" se ignora si la columna
            # no existe, así que es seguro dropearla.
            if "Fallback" in summary_df.columns and \
               not summary_df["Fallback"].astype(str).str.strip().ne("").any():
                summary_df = summary_df.drop(columns=["Fallback"])
            # Si NINGÚN día usó refuerzo (no fue el Tipo "CALL y PUT (Refuerzo)"), ocultar
            # la columna para no ensuciar el resumen con ceros.
            if "Refuerzos" in summary_df.columns and int(summary_df["Refuerzos"].sum()) == 0:
                summary_df = summary_df.drop(columns=["Refuerzos"])

            # Filtro por ROI — mantiene "ROI ($) acumulado" sobre la vista
            # original (no se recalcula sobre el subset, para que siga reflejando
            # el backtest real).
            _n_pos = int((summary_df["ROI (%)"] >= 0).sum())
            _n_neg = int((summary_df["ROI (%)"] < 0).sum())

            # Opciones extra: filtrar por Razón (orden fijo umbral → stop → cierre →
            # overnight). Las 3 principales (umbral/stop/cierre) van SIEMPRE aunque tengan
            # 0 días (así el filtro de stop loss está siempre disponible); overnight solo
            # si aparece (es raro/dormido). La celda "Razón" es f"{icono} {label}", así que
            # la reconstruimos para contar (badge) y filtrar por igualdad de esa columna.
            _reason_counts = summary_df["Razón"].value_counts().to_dict()
            _reason_opts, _reason_tag = [], {}
            for _rk, _tag, _always in (("100%_threshold", "umbral", True),
                                       ("stop_loss", "stop", True),
                                       ("session_end", "cierre", True),
                                       ("overnight_1dte", "overnight", False)):
                _cell = f"{_REASON_ICONS.get(_rk, '•')} {_REASON_LABELS.get(_rk, _rk)}"
                if _always or _reason_counts.get(_cell, 0) > 0:
                    _reason_opts.append(_cell)
                    _reason_tag[_cell] = _tag

            _filter_options = ["Todas", "ROI ≥ 0", "ROI < 0"] + _reason_opts
            # Si la selección guardada ya no figura entre las opciones (otro backtest
            # con distintos motivos), borrarla → el radio cae a "Todas" (index=0).
            if st.session_state.get("batch_roi_filter") not in _filter_options:
                st.session_state.pop("batch_roi_filter", None)
            _roi_filter = st.radio(
                "Filtrar filas",
                options=_filter_options,
                index=0,
                horizontal=True,
                key="batch_roi_filter",
                format_func=lambda o: {
                    "Todas": f"Todas ({len(summary_df)})",
                    "ROI ≥ 0": f"ROI ≥ 0 ({_n_pos})",
                    "ROI < 0": f"ROI < 0 ({_n_neg})",
                }.get(o, f"{o} ({_reason_counts.get(o, 0)})"),
            )
            if _roi_filter == "ROI ≥ 0":
                view_df = summary_df[summary_df["ROI (%)"] >= 0].copy()
            elif _roi_filter == "ROI < 0":
                view_df = summary_df[summary_df["ROI (%)"] < 0].copy()
            elif _roi_filter in _reason_opts:
                view_df = summary_df[summary_df["Razón"] == _roi_filter].copy()
            else:
                view_df = summary_df.copy()

            def _color_ganancia(v):
                if pd.isna(v):
                    return ""
                if v > 0:
                    return "background-color: #c8e6c9"
                if v < 0:
                    return "background-color: #ffcdd2"
                return ""

            if view_df.empty:
                st.info("Ningún día cumple el filtro.")
            else:
                # Tabla de resultados con SELECCIÓN multi-fila nativa (shift+click = rango):
                # las filas seleccionadas se expanden abajo. (Sin columna de casilla.)
                editor_df = view_df.copy().reset_index(drop=True)

                styled_editor = (
                    editor_df.style
                    .map(_color_ganancia, subset=["ROI (%)", "ROI ($)"])
                    # Tinte celeste en las columnas de pico (eco del azul del máximo en el detalle).
                    .set_properties(subset=["Max ROI (%)", "Max ROI ($)"],
                                    **{"background-color": "#e3f2fd"})
                    .format({
                        "Spot @ entrada": "${:,.2f}",
                        "Inversión": "${:,.2f}",
                        "ROI (%)": "{:+.1%}",
                        "ROI ($)": "${:+,.2f}",
                        "Max ROI (%)": "{:+.1%}",
                        "Max ROI ($)": "${:+,.2f}",
                        "ROI ($) acumulado": "${:+,.2f}",
                        "Strike Call": "{:g}",
                        "Strike Put": "{:g}",
                        # Columnas nuevas de predicción
                        "CALL %": "{:.0f}",
                        "PUT %": "{:.0f}",
                        "Umbral ROI": "{:.0f}",
                    })
                )

                # Incluyo el filtro en el key para que al cambiar de filtro la tabla
                # resetee su selección interna (evita "checks fantasmas" en índices
                # de fila que cambiaron de día).
                _table_key = f"batch_summary_table_{_roi_filter}"

                _event = st.dataframe(
                    styled_editor,
                    use_container_width=True,
                    height=min(38 + 35 * len(editor_df), 600),
                    hide_index=True,
                    key=_table_key,
                    on_select="rerun",
                    selection_mode="multi-row",
                    column_config={
                        "Razón": st.column_config.TextColumn(
                            "Razón",
                            width="small",
                            help=(
                                "Motivo de cierre de la iteración. 🎯 = umbral de profit · "
                                "🛑 = stop loss · 🕓 = cierre de sesión sin trigger. "
                                "Pasá el mouse sobre la celda para ver el texto completo."
                            ),
                        ),
                        "CALL mín–máx": st.column_config.TextColumn(
                            "CALL mín–máx", width="small",
                            help="Rango de prima (mínimo – máximo) que alcanzó el CALL durante la iteración.",
                        ),
                        "PUT mín–máx": st.column_config.TextColumn(
                            "PUT mín–máx", width="small",
                            help="Rango de prima (mínimo – máximo) que alcanzó el PUT durante la iteración.",
                        ),
                        "Fallback": st.column_config.TextColumn(
                            "Fallback",
                            width="small",
                            help=(
                                "⚠ = en ese día NO había contrato cuya prima de apertura "
                                "cayera dentro del rango premium pedido, y el motor eligió "
                                "el contrato más cercano al rango. Pasá el mouse sobre la "
                                "celda ⚠ para ver el strike y prima exactos."
                            ),
                        ),
                    },
                )

                _sel_rows = sorted(_event.selection.rows) if (_event and _event.selection) else []
                selected_dates = list(editor_df.iloc[_sel_rows]["Fecha"])
                st.caption("Seleccioná filas (click · **shift+click** para un rango) para ver "
                           "su detalle abajo.")

                # Leyenda visible de los iconos de la columna "Razón".
                st.caption(
                    "**Razón:**  🎯 Exit por umbral de profit  ·  "
                    "🛑 Exit por STOP LOSS  ·  🕓 Sin trigger (cierre de sesión)"
                )

            # Descarga CSV / Excel del resumen — exporta SOLO las filas que pasan
            # el filtro de ROI (Todas / ROI ≥ 0 / ROI < 0), es decir `view_df`.
            _filter_tag = {"ROI ≥ 0": "ROIpos", "ROI < 0": "ROIneg"}.get(
                _roi_filter, _reason_tag.get(_roi_filter, "todas"))
            _export_name = (
                f"{ticker_str}_backtest_{replay_state['date_start']}_"
                f"{replay_state['date_end']}_{_filter_tag}"
            )
            dc = st.columns(2)
            csv_bytes = view_df.to_csv(index=False).encode("utf-8")
            dc[0].download_button(
                f"CSV resumen backtest ({len(view_df)})",
                csv_bytes,
                file_name=f"{_export_name}.csv",
                mime="text/csv",
                key="csv_batch_summary",
            )
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xw:
                view_df.to_excel(xw, sheet_name="summary", index=False)
            dc[1].download_button(
                f"Excel resumen backtest ({len(view_df)})",
                buf.getvalue(),
                file_name=f"{_export_name}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="xlsx_batch_summary",
            )
        else:
            st.warning("Ningún día del rango produjo una iteración exitosa.")

    # Días con error (sigue igual — panel paralelo a "Días con resultados")
    if failed:
        with st.expander(f"❌ Días sin resultado ({len(failed)})", expanded=False):
            for r in failed:
                st.markdown(f"**{r['date']}** — {r['error']}")

    # Render lazy: 1 expander colapsado por cada día seleccionado en la tabla.
    if selected_dates:
        st.markdown("### 🔍 Detalle de los días seleccionados")
        for sel_fecha in selected_dates:
            sel_run = next((r for r in successful if r["date"] == sel_fecha), None)
            if sel_run is None:
                continue
            it = sel_run["iteration"]
            _reason = _REASON_LABELS.get(it.exit_reason, it.exit_reason)
            if it.gain_total >= 0:
                _icon, _gain_part = ":green[▲]", f":green[**${it.gain_total:+,.2f}**]"
            else:
                _icon, _gain_part = ":red[▼]", f":red[**${it.gain_total:+,.2f}**]"
            _roi_pct = (it.gain_total / it.invest_total) if it.invest_total else 0.0
            _pct_part = (f":green[▲ {abs(_roi_pct):.1%}]" if it.gain_total >= 0
                         else f":red[▼ {abs(_roi_pct):.1%}]")
            _fb_tag = "  ·  ⚠ fallback" if (it.call_fallback or it.put_fallback) else ""
            _ref_tag = (f"  ·  ➕ {it.refuerzo['n']} refuerzo(s)"
                        if getattr(it, "refuerzo", None) and it.refuerzo["n"] else "")
            _exp_title = (
                f"{_icon} {sel_fecha}  ·  {it.start_dt:%H:%M} → {it.end_dt:%H:%M} ({_op_dur(it)})  ·  "
                f"{_reason}  ·  Ganancia: {_gain_part} ({_pct_part})"
                f"{_max_leg_rois(it)}{_fb_tag}{_ref_tag}"
            )
            with st.expander(_exp_title, expanded=False):
                render_iteration(it, ticker_str, sel_run["date"])

    # En range mode no hay "próxima iteración" — fin del flujo.
    st.stop()


# ========================================================================
# Single-day mode (comportamiento original)
# ========================================================================
iterations = replay_state["iterations"]
date_str = replay_state["date"]

st.subheader(
    f"{ticker_str} — {date_str}  ·  0 DTE (expiry {replay_state['expiry']})  ·  "
    f"Ventana {replay_state['time_start']:%H:%M}–{replay_state['time_end']:%H:%M}"
)

total_invested = sum(i.invest_total for i in iterations)
total_gain = sum(i.gain_total for i in iterations)
final_capital = total_invested + total_gain
roi = total_gain / total_invested if total_invested else 0.0
n_trig = sum(1 for i in iterations if i.exit_reason == "100%_threshold")

st.markdown("### 💼 Totales de la sesión")
tc = st.columns(5)
tc[0].metric("# iteraciones", len(iterations))
tc[1].metric("Inversión total", f"${total_invested:,.2f}")

# Ganancia total con fondo verde/rojo claro según signo
# (verde = mismo del banner success de Streamlit)
if total_gain > 0:
    gain_bg, gain_delta_color, gain_arrow = "rgba(33, 195, 84, 0.1)", "#2e7d32", "▲"
elif total_gain < 0:
    gain_bg, gain_delta_color, gain_arrow = "#ffcdd2", "#b71c1c", "▼"
else:
    gain_bg, gain_delta_color, gain_arrow = "#f0f2f6", "#555", "–"
gain_delta_txt = f"{gain_arrow} {abs(roi):.1%}" if total_invested else ""
tc[2].markdown(
    f"<div style='border:1px solid rgba(49,51,63,0.2); border-radius:0.5rem; "
    f"padding:0.85rem 1rem; background-color:{gain_bg};'>"
    f"<div style='font-size:0.85rem; font-weight:bold; color:rgba(49,51,63,0.65); "
    f"margin-bottom:0.35rem;'>Ganancia total</div>"
    f"<div style='font-size:1.75rem; font-weight:600; line-height:1.15;'>${total_gain:+,.2f}</div>"
    f"<div style='font-size:0.85rem; color:{gain_delta_color}; margin-top:0.25rem;'>{gain_delta_txt}</div>"
    f"</div>",
    unsafe_allow_html=True,
)
tc[3].metric("Capital final", f"${final_capital:,.2f}")
tc[4].metric("Triggers ejecutados", f"{n_trig} / {len(iterations)}")

# Distribución temporal de operaciones exitosas — modo SINGLE: las "operaciones"
# son las iteraciones del loop (cada reentrada al cumplirse el +100%).
render_temporal_distribution(
    iterations, entrada=replay_state["order_time"], chart_key="temporal_chart_single",
)

# Espacio arriba de la primera iteración igual al que hay entre iteraciones.
st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
for it in iterations:
    _reason = _REASON_LABELS.get(it.exit_reason, it.exit_reason)
    if it.gain_total >= 0:
        _icon = ":green[▲]"
        _gain_part = f":green[**${it.gain_total:+,.2f}**]"
    else:
        _icon = ":red[▼]"
        _gain_part = f":red[**${it.gain_total:+,.2f}**]"
    _roi_pct = (it.gain_total / it.invest_total) if it.invest_total else 0.0
    _pct_part = (f":green[▲ {abs(_roi_pct):.1%}]" if it.gain_total >= 0
                 else f":red[▼ {abs(_roi_pct):.1%}]")
    _fb_tag = ""
    if getattr(it, "call_fallback", False) or getattr(it, "put_fallback", False):
        _fb_tag = "  ·  ⚠ fallback"
    _ref_tag = (f"  ·  ➕ {it.refuerzo['n']} refuerzo(s)"
                if getattr(it, "refuerzo", None) and it.refuerzo["n"] else "")
    _exp_title = (
        f"{_icon} Iteración {it.iteration}  ·  {it.start_dt:%H:%M} → {it.end_dt:%H:%M} ({_op_dur(it)})  ·  "
        f"{_reason}  ·  Ganancia: {_gain_part} ({_pct_part})"
        f"{_max_leg_rois(it)}{_fb_tag}{_ref_tag}"
    )
    with st.expander(_exp_title, expanded=False):
        render_iteration(it, ticker_str, date_str)

# ----- Status banner para la próxima iteración (al final, después de cada iteración) -----
st.markdown("---")
exhausted = _is_session_exhausted(replay_state)
next_ts = _next_start_ts(replay_state)
if exhausted:
    last_reason = iterations[-1].exit_reason
    if last_reason == "session_end":
        st.warning(
            f"**Sesión cerrada** — la última iteración llegó al cierre ({iterations[-1].end_dt:%H:%M}) "
            f"sin disparar el umbral. No hay más espacio."
        )
    else:
        st.warning(
            f"**Sesión cerrada** — la próxima iteración empezaría a las {next_ts:%H:%M} "
            f"que está fuera de la ventana."
        )
else:
    # Cada minuto es seleccionable (componente hora:minuto) → snap exacto (step=1).
    _msg_step = 1
    _msg_h, _msg_m = _snap_to_orden_grid(
        next_ts, replay_state["time_start"], replay_state["time_end"], _msg_step
    )
    _next_display = f"{_msg_h:02d}:{_msg_m:02d}"
    st.success(
        f"**Próxima iteración listo a las {_next_display}** — "
        f"ajustá los parámetros en la barra lateral si querés y pulsá **Próxima iteración**."
    )

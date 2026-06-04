"""Streamlit UI — generic intraday options replay."""
from __future__ import annotations

import io
import sys
from datetime import date as date_cls, time as time_cls, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402
from engine import (  # noqa: E402
    IterationResult,
    NoMatchError,
    run_next_iteration,
    validate_0dte_session,
)
from predictor import (  # noqa: E402
    adjust_trading_parameters,
    load_config as load_predictor_config,
)

DATA_DIR = HERE / "data"
TICKER_INFO_PATH = HERE / "ticker_info.json"
MARKET_HOURS_PATH = HERE / "market_hours.json"

# Comisión por contrato (USD). Usada en el reporte de "Operaciones" por iteración.
COMMISSION_PER_CONTRACT = 1.0


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
def load_ticker_info() -> dict:
    if not TICKER_INFO_PATH.exists():
        return {}
    import json
    with TICKER_INFO_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_api_key() -> str:
    try:
        import config  # type: ignore
        return getattr(config, "POLYGON_API_KEY", "")
    except Exception:
        return ""


@st.cache_resource
def get_downloader(api_key: str) -> Downloader:
    return Downloader(PolygonAdapter(api_key), DATA_DIR)


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


def _to_ts(date_str: str, t: time_cls) -> pd.Timestamp:
    return pd.Timestamp.combine(pd.Timestamp(date_str).date(), t).tz_localize(TZ)


def _is_session_exhausted(replay: dict) -> bool:
    if not replay["iterations"]:
        return False
    last = replay["iterations"][-1]
    if last.exit_reason == "session_end":
        return True
    next_start = last.end_dt + pd.Timedelta(minutes=1)
    return next_start >= replay["day_end_ts"]


def _next_start_ts(replay: dict) -> pd.Timestamp:
    if not replay["iterations"]:
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


def render_batch_totals(
    day_runs: list,
    total_days: int,
    title: str = "💼 Totales del backtest",
) -> None:
    """Renderiza el panel de 5 métricas + caption de razones de salida.
    Pensado para ser llamado tanto en vivo durante el batch (con day_runs
    parcial) como en el render final (con day_runs completo). Definida aquí
    arriba porque el batch loop la llama antes del bloque de render."""
    successful = [r for r in day_runs if r.get("iteration") is not None]
    # "Ganancia total" = SUMATORIA de los ROI ($) de todas las iteraciones — es decir,
    # el valor FINAL de la columna "ROI ($) acumulado" de la tabla de días (cum_gain).
    total_invested = sum(r["iteration"].invest_total for r in successful)
    total_gain = sum(r["iteration"].gain_total for r in successful)
    final_capital = total_invested + total_gain
    roi = total_gain / total_invested if total_invested else 0.0
    n_trig = sum(1 for r in successful if r["iteration"].exit_reason == "100%_threshold")
    n_stop = sum(1 for r in successful if r["iteration"].exit_reason == "stop_loss")
    n_eod = sum(1 for r in successful if r["iteration"].exit_reason == "session_end")
    n_winning = sum(1 for r in successful if r["iteration"].gain_total > 0)
    n_losing = sum(1 for r in successful if r["iteration"].gain_total < 0)
    # Win rate = ganadores / (ganadores + perdedores). Excluye días con
    # ganancia exactamente 0 (neutrales) del denominador.
    _decisive = n_winning + n_losing
    win_rate = (n_winning / _decisive) if _decisive else 0.0

    st.markdown(f"### {title}")
    tc = st.columns(6)
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
        f"""
        <div style='
            border: 1px solid rgba(49, 51, 63, 0.2);
            border-radius: 0.5rem;
            padding: 0.85rem 1rem;
            background-color: {gain_bg};
        '>
            <div style='font-size: 0.85rem; font-weight: bold; color: rgba(49, 51, 63, 0.65); margin-bottom: 0.35rem;'>
                Ganancia total
            </div>
            <div style='font-size: 1.75rem; font-weight: 600; line-height: 1.15;'>
                ${total_gain:+,.2f}
            </div>
            <div style='font-size: 0.85rem; color: {gain_delta_color}; margin-top: 0.25rem;'>
                {gain_delta_txt}
            </div>
        </div>
        """,
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

    st.caption(
        f"📊 Razones de salida: "
        f"**{n_trig}** umbral · **{n_stop}** stop loss · **{n_eod}** cierre de sesión  ·  "
        f"💹 **Ganancia total** = suma de los ROI ($) de todas las iteraciones"
    )


# ============================================================================
# App
# ============================================================================
# try/except: standalone funciona normal; dentro del trading_suite (st.navigation)
# set_page_config ya se llamó en el entry → ignoramos el error de doble llamada.
try:
    st.set_page_config(
        page_title="Options Replay — 0 DTE",
        layout="wide",
        initial_sidebar_state="expanded",
    )
except Exception:
    pass
st.markdown(
    """
    <style>
    /* Sidebar ancho 420px solo cuando está expandida. Cuando se colapsa
       (aria-expanded="false"), no aplica el min/max-width y la animación
       de colapso de Streamlit puede correr normalmente. */
    section[data-testid="stSidebar"][aria-expanded="true"] {
        min-width: 420px !important;
        max-width: 420px !important;
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

    /* Tirar el h2 'Parámetros' hacia arriba para que quede pegado al borde y centrarlo. */
    section[data-testid="stSidebar"] h2:first-child,
    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] h2:first-child {
        margin-top: -1.5rem !important;
        padding-top: 0 !important;
        text-align: center !important;
    }

    /* Pegar el título 'Options Replay — Intraday 0 DTE' al extremo superior
       sin solaparlo con el header de Streamlit (que contiene Deploy / menú). */
    .main .block-container,
    [data-testid="stMainBlockContainer"],
    [data-testid="stAppViewContainer"] > .main > .block-container {
        padding-top: 1.5rem !important;
    }
    .main h1:first-child {
        margin-top: 0 !important;
        padding-top: 0 !important;
        font-size: 1.5rem !important;
        line-height: 1.2 !important;
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
st.title("Options Replay — Intraday 0 DTE")
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

# ----- Sidebar form -----
st.sidebar.header("Parámetros")

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
    Ej: `🟢 💾 SPY — …` · `🟡 ☁️ MSFT — …` · `☁️ COIN — …` (solo weekly)
    """
    info = _all_tickers_info.get(t) or {}
    nombre = info.get("nombre")
    tier = _zerodte_map.get(t)
    mark = "🟢 " if tier == "daily" else ("🟡 " if tier == "mwf" else "")
    cache = "💾" if t in _cached_set else "☁️"
    base = f"{mark}{cache} {t}"
    return f"{base} — {nombre}" if nombre else base


_ticker_legend = (
    "🟢 0DTE todos los días&#10;"
    "🟡 0DTE solo Lun/Mié/Vie&#10;"
    "💾 cacheado&#10;"
    "☁️ sin cache"
)
st.sidebar.markdown(
    "<p style='display:flex; justify-content:space-between; align-items:center; "
    "font-weight:bold; margin: 0.3rem 0 0.3rem 0;'>"
    "<span>Ticker</span>"
    f"<span title='{_ticker_legend}' style='cursor:help; color:#888; "
    "font-weight:normal;'>ⓘ</span>"
    "</p>",
    unsafe_allow_html=True,
)
ticker = st.sidebar.selectbox(
    "Ticker",
    options=TICKER_OPTIONS,
    index=_default_idx,
    key="ticker_select",
    on_change=_on_ticker_change,
    format_func=_ticker_label,
    label_visibility="collapsed",
)

# Aviso si el ticker elegido solo vence Lun/Mié/Vie — evita el error
# "No 0 DTE option ..." al elegir un martes o jueves.
if _zerodte_map.get(ticker) == "mwf":
    st.sidebar.caption(
        f"🟡 **{ticker}** vence **Lun / Mié / Vie** — no hay 0DTE los **Mar / Jue**. "
        f"Para esos días no existe contrato del mismo día."
    )

# Aviso si el ticker elegido NO tiene cache local — vamos a tener que pegarle
# a Polygon en vivo (lento) y puede fallar para fechas históricas o feriados.
if ticker not in _cached_set:
    st.sidebar.caption(
        f"⚠ **{ticker}** no tiene cache local. Cada backtest va a bajar datos "
        f"de Polygon en vivo (lento, consume rate limit)."
    )
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

with st.sidebar.expander(f"📊 Info {ticker}", expanded=False):
    if not _tinfo:
        st.caption(f"_No hay info para {ticker} en el archivo._")
    else:
        if _tinfo.get("nombre"):
            st.markdown(f"**{_tinfo['nombre']}**")
        _meta_bits = []
        if _tinfo.get("indice"):
            _meta_bits.append(f"📍 {_tinfo['indice']}")
        if _tinfo.get("bloque_sector"):
            _meta_bits.append(f"🏷️ {_tinfo['bloque_sector']}")
        if _meta_bits:
            st.caption(" · ".join(_meta_bits))
        if _tinfo.get("sectores"):
            st.caption(f"_{_tinfo['sectores']}_")

    # Rango óptimo Min/Max — debajo del sector
    _c_pmin, _c_pmax = st.columns(2)
    premium_min = _c_pmin.number_input(
        "Rango óptimo Min (USD)", step=0.05, min_value=0.0, format="%.2f",
        key="premium_min_input",
    )
    premium_max = _c_pmax.number_input(
        "Rango óptimo Max(USD)", step=0.05, min_value=0.0, format="%.2f",
        key="premium_max_input",
    )

    if _tinfo:
        # Min / Max — dos inputs estilo Rango óptimo. Defaults desde JSON / 100.
        _def_min = (_tinfo.get("min") / 100.0) if _tinfo.get("min") is not None else 0.30
        _def_max = (_tinfo.get("max") / 100.0) if _tinfo.get("max") is not None else 0.50
        _c_mn, _c_mx = st.columns(2)
        mn_val = _c_mn.number_input(
            "Rango extendido Min (USD)", step=0.05, min_value=0.0, format="%.2f",
            key=f"min_input_{ticker}",
            value=_def_min,
        )
        mx_val = _c_mx.number_input(
            "Rango extendido Max (USD)", step=0.05, min_value=0.0, format="%.2f",
            key=f"max_input_{ticker}",
            value=_def_max,
        )
        if _tinfo.get("fecha_analisis"):
            st.caption(f"📅 Fecha de análisis: {_tinfo['fecha_analisis']}")

# Rango EXTENDIDO (Min-Max ÷100) que el motor usa como 2do nivel de la cascada
# de selección de contratos. Si el usuario editó los inputs MIN/MAX del panel,
# se toman de session_state; sino del JSON ÷100; sino cae al rango óptimo.
if _tinfo and _tinfo.get("min") is not None and _tinfo.get("max") is not None:
    ext_premium_min = float(st.session_state.get(f"min_input_{ticker}", _tinfo["min"] / 100.0))
    ext_premium_max = float(st.session_state.get(f"max_input_{ticker}", _tinfo["max"] / 100.0))
else:
    ext_premium_min = premium_min
    ext_premium_max = premium_max


# Default = ayer, ajustado al último día hábil de mercado (si ayer fue sábado,
# domingo o feriado US, retrocede al viernes hábil anterior).
default_date = _last_open_market_day(date_cls.today() - timedelta(days=1))
# Default para "Fecha inicial" en modo rango: 10 meses atrás (calendario real,
# vía pd.DateOffset que respeta longitudes de mes — no aproxima a 300 días).
# También ajustado al último día hábil si cae en feriado/weekend.
default_start_date = _last_open_market_day(
    (pd.Timestamp.today() - pd.DateOffset(months=10)).date()
)

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
with st.sidebar.container(border=True):
    # Inicio/Fin ya NO se ingresan por UI — vienen de market_hours.json
    # (default 09:30–16:00, override por ticker si hace falta).
    t_start, t_end = get_market_hours(ticker)
    _mkt_tip = (
        f"Se asume que el mercado abre a las {t_start:%H:%M} "
        f"y cierra a las {t_end:%H:%M}"
    )
    st.markdown(
        "<p style='text-align:left; font-weight:bold; margin: 0.3rem 0 0.5rem 0;'>"
        "Parámetros de sesión "
        f"<span title='{_mkt_tip}' style='cursor:help; color:#888; font-weight:normal;'>ⓘ</span>"
        "</p>",
        unsafe_allow_html=True,
    )
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
    # resetear la hora/minuto del orden a 09:30. El step se respeta porque 30
    # es divisible por 1, 5, 10, 15 (siempre cae en la grilla).
    def _reset_hora_on_date_change():
        st.session_state["hora_orden_hour"] = 9
        st.session_state["hora_orden_minute"] = 30

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
            value=default_start_date,
            format="YYYY-MM-DD",
            key="sel_fecha_inicial",
            on_change=_reset_hora_on_date_change,
            help=f"Fecha de inicio del rango (inclusiva). Default: 10 meses atrás ({default_start_date}).",
        )
        sel_end = c_fecha_fin.date_input(
            "Fecha final",
            value=default_date,
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

    if not is_range:
        # Hora de orden — Step + verificación de ROI arriba; Hora + Minuto abajo.
        # El Step define la granularidad del dropdown de minutos.
        st.markdown(
            "<p style='font-weight:bold; margin: 0.4rem 0 0.2rem 0;'>Hora de orden</p>",
            unsafe_allow_html=True,
        )

        _step_options = [1, 5, 10, 15]
        if "hora_orden_step" not in st.session_state:
            st.session_state["hora_orden_step"] = 5  # default 5 minutos
        if "sell_check_min" not in st.session_state:
            st.session_state["sell_check_min"] = 1  # default verificación venta (min)

        # Aplicar sync pendiente desde la iteración anterior — DEBE ocurrir ANTES
        # de instanciar CUALQUIER widget cuyo key se vaya a modificar (selectboxes
        # step, hora y minuto). Streamlit prohíbe escribir session_state[key] de un
        # widget ya creado.
        _pending_sync = st.session_state.pop("_pending_hora_sync", None)
        if _pending_sync is not None:
            if len(_pending_sync) == 3:
                _ph, _pm, _pstep = _pending_sync
                st.session_state["hora_orden_step"] = int(_pstep)
            else:
                _ph, _pm = _pending_sync
            st.session_state["hora_orden_hour"] = int(_ph)
            st.session_state["hora_orden_minute"] = int(_pm)

        # ----- Fila 1: Step (min) | Sell verification (min) -----
        c_step, c_roi = st.columns(2)
        _step = int(c_step.selectbox(
            "Step (min)",
            options=_step_options,
            key="hora_orden_step",
            format_func=lambda s: f"{s} min",
            help="Granularidad del dropdown de minutos (1, 5, 10 ó 15).",
        ))
        sell_check_min = int(c_roi.selectbox(
            "Sell verification (min)",
            options=[1, 2, 3, 4, 5],
            key="sell_check_min",
            format_func=lambda s: f"{s} min",
            help=(
                "Cada cuántos MINUTOS se verifica el Umbral de ROI (%). Simula el "
                "período de llamada al API: la tabla minuto a minuto avanza de a "
                "esta cantidad de minutos y la venta automática se evalúa en cada "
                "uno de esos pasos (1 = cada minuto)."
            ),
        ))

        # Computar opciones válidas de hora dentro del rango [t_start, t_end].
        _start_hour, _end_hour = t_start.hour, t_end.hour
        _valid_hours = list(range(_start_hour, _end_hour + 1))

        # Clamp hora actual al rango válido (si el usuario cambió t_start/t_end).
        if "hora_orden_hour" not in st.session_state:
            st.session_state["hora_orden_hour"] = 9 if 9 in _valid_hours else _valid_hours[0]
        if st.session_state["hora_orden_hour"] not in _valid_hours:
            st.session_state["hora_orden_hour"] = _valid_hours[0]

        # ----- Fila 2: Hora | Minuto -----
        c_h, c_m = st.columns(2)
        selected_hour = c_h.selectbox(
            "Hora",
            options=_valid_hours,
            key="hora_orden_hour",
            format_func=lambda h: f"{h:02d}",
        )

        # Generar opciones de minuto según step + restricciones por hora límite.
        def _minute_options_for(hour: int, step: int) -> list[int]:
            if hour == t_start.hour and hour == t_end.hour:
                min_m, max_m = t_start.minute, t_end.minute
            elif hour == t_start.hour:
                min_m, max_m = t_start.minute, 59
            elif hour == t_end.hour:
                min_m, max_m = 0, t_end.minute
            else:
                min_m, max_m = 0, 59
            opts = [m for m in range(0, 60, step) if min_m <= m <= max_m]
            # Garantizar al menos un valor — sumar el borde si quedó vacío
            if not opts:
                opts = [min_m]
            return opts

        _minute_opts = _minute_options_for(selected_hour, _step)

        # Clamp minuto al set válido del step actual.
        if "hora_orden_minute" not in st.session_state:
            st.session_state["hora_orden_minute"] = 30 if 30 in _minute_opts else _minute_opts[0]
        if st.session_state["hora_orden_minute"] not in _minute_opts:
            # Buscar el más cercano hacia abajo (preserva intención del usuario).
            cur = st.session_state["hora_orden_minute"]
            closest = min(_minute_opts, key=lambda m: abs(m - cur))
            st.session_state["hora_orden_minute"] = closest

        selected_minute = c_m.selectbox(
            "Minuto",
            options=_minute_opts,
            key="hora_orden_minute",
            format_func=lambda m: f"{m:02d}",
            help=(
                f"Opciones generadas con step={_step} min, restringidas al rango "
                f"{t_start:%H:%M}–{t_end:%H:%M}."
            ),
        )

        hora_orden = time_cls(int(selected_hour), int(selected_minute))

    else:
        # En modo rango usamos 09:30 fijo (apertura) como hora de orden.
        hora_orden = time_cls(9, 30)
        sell_check_min = int(st.session_state.get("sell_check_min", 1))

    # Cargar config del predictor — necesario en ambos modos.
    _predictor_cfg = load_predictor_config()

    # =====================================================================
    # Probabilidad (%) MANUAL — reemplaza la predicción k-NN. El usuario la
    # mueve en el slider (0–100, default 50) y de ahí se derivan los
    # "Parámetros por iteración". Visible en ambos modos.
    # =====================================================================
    def _classify_prob(prob, cfg):
        """Devuelve (label, color, box_bg, box_border) según el rango."""
        for r in cfg.get("classification_ranges", []):
            if r["min"] <= prob <= r["max"]:
                return (r["label"], r["color"],
                        r.get("box_bg", r["color"]), r.get("box_border", r["color"]))
        return "?", "#888888", "#eeeeee", "#888888"

    _mode_map = {"call_only": "Sólo CALL", "put_only": "Sólo PUT", "both": "CALL y PUT"}

    # La "Tendencia del mercado" SOLO aplica en single-day (deriva los parámetros del
    # día). En "Rango de fechas" no se usa → se fija en 50% y se OCULTA todo el bloque.
    if not is_range:
        st.markdown(
            "<p style='font-weight:bold; margin: 0.5rem 0 0.2rem 0;'>Tendencia del mercado</p>",
            unsafe_allow_html=True,
        )
        # Slider (izquierda) + box (derecha) en la misma fila.
        _c_slider, _c_box = st.columns([3, 2], vertical_alignment="center")
        manual_prob = int(_c_slider.slider(
            "Tendencia del mercado", min_value=0, max_value=100, value=50, step=5,
            key="manual_prob", label_visibility="collapsed",
            help=("Movés la tendencia alcista a mano (de 5 en 5). De este valor se derivan "
                  "Modo, CALL%, PUT% y Umbral ROI de los Parámetros por iteración."),
        ))
        _prob_label, _prob_color, _box_bg, _box_border = _classify_prob(manual_prob, _predictor_cfg)
        _params = adjust_trading_parameters(manual_prob, _predictor_cfg)
        _mode_lbl = _mode_map[_params["mode"]]

        # Barra de ZONAS debajo del slider: un segmento por categoría, con el color
        # VIVO (saturado) de cada categoría.
        _zones = [
            (r["min"], r["max"], r["color"])
            for r in _predictor_cfg.get("classification_ranges", [])
        ]
        _segs = "".join(
            f"<div style='flex:1; background:{c}; height:7px;' title='{lo}–{hi}'></div>"
            for lo, hi, c in _zones
        )
        _c_slider.markdown(
            f"<div style='display:flex; gap:1px; border-radius:3px; overflow:hidden; margin-top:-6px;'>{_segs}</div>"
            "<div style='display:flex; justify-content:space-between; font-size:0.6rem; color:#000; font-weight:normal; margin-top:1px;'>"
            "<span>0</span><span>20</span><span>40</span><span>60</span><span>80</span><span>100</span></div>",
            unsafe_allow_html=True,
        )

        # Slider "Tendencia del mercado": relleno = color del rango (_prob_color, stop
        # en manual_prob%); bola y número negro. Selector estructural verificado contra
        # el DOM (en esta versión NO existe stSliderTrack; el relleno es un div con
        # background-image: linear-gradient). Scope al sidebar.
        _fill = (
            f"linear-gradient(to right,{_prob_color} 0%,{_prob_color} {manual_prob}%,"
            f"rgba(151,166,195,0.25) {manual_prob}%,rgba(151,166,195,0.25) 100%)"
        )
        st.markdown(
            "<style>"
            "section[data-testid='stSidebar'] [data-baseweb='slider'] "
            "> div:nth-child(1) > div:nth-child(1) > div:nth-child(2)"
            f"{{background-image:{_fill} !important;}}"
            "section[data-testid='stSidebar'] [data-baseweb='slider'] [role='slider']"
            "{background-color:#000 !important; border-color:#000 !important;}"
            "section[data-testid='stSidebar'] [data-testid='stSliderThumbValue']"
            "{color:#000 !important;}"
            "</style>",
            unsafe_allow_html=True,
        )

        # Box color-coded (fondo claro + borde fino y texto oscuro) a la derecha del slider.
        _c_box.markdown(
            f"<div style='text-align:center; padding:0.55rem 0.6rem; background:{_box_bg}; "
            f"border:1px solid {_box_border}; border-radius:0.5rem;'>"
            f"<div style='font-size:1.6rem; font-weight:700; color:{_box_border}; line-height:1;'>{manual_prob}%</div>"
            f"<div style='font-size:0.7rem; font-weight:600; color:{_box_border}; margin-top:0.2rem;'>{_prob_label}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        # Rango de fechas: tendencia fija en 50% (NEUTRAL) y oculta.
        manual_prob = 50
        _params = adjust_trading_parameters(manual_prob, _predictor_cfg)
        _mode_lbl = _mode_map[_params["mode"]]

    if is_range:
        st.info(
            "🤖 **Modo rango** — los **Parámetros por iteración** de abajo se aplican "
            "a **todos los días** del rango (un solo set de parámetros para el batch)."
        )

    # Parámetros por iteración — VISIBLES EN AMBOS MODOS (single y rango). En rango,
    # estos mismos valores (auto-aplicados desde el slider, editables a mano) se usan
    # para todos los días del batch. Antes esta sección se ocultaba en modo rango, lo
    # que daba la sensación de que "desaparecía todo el panel".
    if True:
        # AUTO-APPLY: la Probabilidad (%) → Parámetros por iteración. Se aplica
        # cuando cambia el slider (o el config). Entre cambios, podés editar
        # CALL%/PUT%/ROI a mano sin que se sobreescriban (key-tracking sobre los
        # params derivados, no sobre los widgets).
        _prob_key = (
            manual_prob, _params["mode"], int(_params["call_allocation"]),
            int(_params["put_allocation"]), int(_params["roi_threshold"]),
        )
        if st.session_state.get("_last_applied_prob_key") != _prob_key:
            _tot = st.session_state.get("invest_total", 1000.0)
            st.session_state["straddle_mode_radio"] = _mode_lbl
            st.session_state["call_pct"] = float(_params["call_allocation"])
            st.session_state["put_pct"] = float(_params["put_allocation"])
            st.session_state["call_dollars"] = (_params["call_allocation"] / 100.0) * _tot
            st.session_state["put_dollars"] = (_params["put_allocation"] / 100.0) * _tot
            st.session_state["umbral_roi_pct"] = float(_params["roi_threshold"])
            st.session_state["_last_applied_prob_key"] = _prob_key
        st.caption(
            f"→ Aplicado: {_mode_lbl} · CALL {_params['call_allocation']}% / "
            f"PUT {_params['put_allocation']}% · ROI {_params['roi_threshold']}%  "
            f"_(podés editarlos a mano)_"
        )

        st.markdown("---")
        st.markdown(
            "<p style='text-align:left; font-weight:bold; margin: 0.3rem 0 0.8rem 0;'>Parámetros por iteración</p>",
            unsafe_allow_html=True,
        )

        # Modo de straddle — radio con 3 opciones (mutuamente excluyente por diseño).
        # El callback fuerza el split CALL%/PUT% según el modo seleccionado:
        #   CALL y PUT → 50/50,  Sólo CALL → 100/0,  Sólo PUT → 0/100.
        def _sync_straddle_mode_changed():
            mode = st.session_state.get("straddle_mode_radio", "CALL y PUT")
            if mode == "Sólo CALL":
                new_call_pct, new_put_pct = 100.0, 0.0
            elif mode == "Sólo PUT":
                new_call_pct, new_put_pct = 0.0, 100.0
            else:  # "CALL y PUT" o "CALL o PUT" → ambas piernas 50/50
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

        _straddle_mode = st.radio(
            "Modo",
            options=["CALL y PUT", "Sólo CALL", "Sólo PUT", "CALL o PUT", "CALL o PUT (plus)"],
            index=0,
            horizontal=True,
            key="straddle_mode_radio",
            on_change=_sync_straddle_mode_changed,
            label_visibility="collapsed",
            help=(
                "CALL y PUT: salida combinada por ROI total. · Sólo CALL / Sólo PUT: "
                "una pierna. · CALL o PUT: se venden AMBAS cuando cualquiera llega a "
                "+100%. · CALL o PUT (plus): la 1ª pierna que alcanza el 'Umbral de "
                "salida (%)' se vende; la otra se vende cuando entre lo bancado y su "
                "valor se recupera la inversión total."
            ),
        )
        only_call_now = _straddle_mode == "Sólo CALL"
        only_put_now = _straddle_mode == "Sólo PUT"
        is_call_or_put = _straddle_mode == "CALL o PUT"
        is_call_or_put_plus = _straddle_mode == "CALL o PUT (plus)"
        if only_call_now:
            engine_mode = "call_only"
        elif only_put_now:
            engine_mode = "put_only"
        elif is_call_or_put:
            engine_mode = "call_or_put"
        elif is_call_or_put_plus:
            engine_mode = "call_or_put_plus"
        else:
            engine_mode = "both"

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

        # Fila 1: Inversión total (ocupa solo la mitad izquierda para mantener consistencia)
        c_inv_total, _c_inv_spacer = st.columns(2)
        c_inv_total.number_input(
            "Inversión ($)",
            key="invest_total",
            step=1000.0,
            min_value=0.0,
            format="%.2f",
            on_change=_sync_total_to_dollars,
        )

        # Fila 2: %-split por leg
        c_call_pct, c_put_pct = st.columns(2)
        c_call_pct.number_input(
            "Inversión en CALL (%)",
            key="call_pct",
            step=5.0,
            min_value=0.0,
            max_value=100.0,
            format="%.1f",
            on_change=_sync_call_pct_changed,
            disabled=only_put_now,
        )
        c_put_pct.number_input(
            "Inversión en PUT (%)",
            key="put_pct",
            step=5.0,
            min_value=0.0,
            max_value=100.0,
            format="%.1f",
            on_change=_sync_put_pct_changed,
            disabled=only_call_now,
        )

        # Fila 3: $-split por leg (bidireccional con %)
        c_call_d, c_put_d = st.columns(2)
        invest_call = c_call_d.number_input(
            "Inversión en CALL ($)",
            key="call_dollars",
            step=100.0,
            min_value=0.0,
            format="%.2f",
            on_change=_sync_call_dollars_changed,
            disabled=only_put_now,
        )
        invest_put = c_put_d.number_input(
            "Inversión en PUT ($)",
            key="put_dollars",
            step=100.0,
            min_value=0.0,
            format="%.2f",
            on_change=_sync_put_dollars_changed,
            disabled=only_call_now,
        )

        # Default del Umbral si nunca se sembró (caso primera carga).
        # El auto-apply de la Predicción Apertura ya escribe a este key cuando
        # cambia la predicción — no necesitamos el messenger _pending_roi_threshold.
        if "umbral_roi_pct" not in st.session_state:
            st.session_state["umbral_roi_pct"] = 10.0
        # Defaults de params por pierna ("CALL o PUT") y del umbral de salida ("plus").
        for _k, _v in (("call_roi_pct", 10.0), ("call_stop_pct", -100.0),
                       ("put_roi_pct", 10.0), ("put_stop_pct", -100.0),
                       ("exit_plus_pct", 50.0)):
            if _k not in st.session_state:
                st.session_state[_k] = _v
        if "exit_plus_time" not in st.session_state:
            st.session_state["exit_plus_time"] = time_cls(15, 30)

        # El cierre por umbral siempre se evalúa sobre el ROI (%) total.
        exit_metric = "total"
        exit_plus_threshold_pct = 0.50  # default; solo en "CALL o PUT (plus)"
        exit_plus_time = None           # default; solo en "CALL o PUT (plus)"

        if is_call_or_put:
            # CALL o PUT: salida COMBINADA al +100%. Se venden AMBAS piernas cuando
            # CUALQUIERA alcanza +100% (se duplica). NO usa Umbral de ROI ni Stop
            # loss — por eso no se muestran esos inputs. Termina al +100% o al cierre.
            st.info(
                "🎯 **CALL o PUT** — se compran ambas piernas y se **venden las dos** "
                "en cuanto **cualquiera alcanza +100%** (se duplica). No depende de "
                "Umbral de ROI ni Stop loss. Termina al +100% o al **cierre del día**."
            )
            exit_threshold_pct = 1.0          # +100% (solo referencia de estilo)
            stop_loss_pct = -1.0
            call_exit_threshold_pct = put_exit_threshold_pct = 1.0
            call_stop_loss_pct = put_stop_loss_pct = -1.0
        elif is_call_or_put_plus:
            # CALL o PUT (plus): la 1ª pierna que alcanza el "Umbral de salida (%)" se
            # vende y banca su ganancia; la otra se vende cuando, sumando lo bancado +
            # su valor, se recupera la inversión TOTAL. Si no, cierran al fin del día.
            st.info(
                "🎯 **CALL o PUT (plus)** — la **1ª pierna** que alcanza el **Umbral de "
                "salida (%)** se vende y banca su ganancia. La **otra** se vende cuando, "
                "sumando lo bancado + su valor, se **recupera la inversión total** "
                "(CALL + PUT). Si no se cumple antes de la **Hora de salida**, ambas se "
                "venden a esa hora."
            )
            cps, chs = st.columns(2)
            exit_plus_threshold_pct = cps.number_input(
                "Umbral de salida (%)", key="exit_plus_pct",
                step=5.0, min_value=1.0, format="%.2f",
                help="ROI% al que se vende la PRIMERA pierna (la que llegue primero al umbral).",
            ) / 100.0
            exit_plus_time = chs.time_input(
                "Hora de salida", key="exit_plus_time", step=300,
                help=("Hora MÁXIMA de venta de AMBAS piernas. Si la estrategia no se "
                      "cumple antes, se liquidan a esta hora en vez de esperar al "
                      "cierre (16:00)."),
            )
            exit_threshold_pct = exit_plus_threshold_pct   # referencia de estilo
            stop_loss_pct = -1.0
            call_exit_threshold_pct = put_exit_threshold_pct = exit_plus_threshold_pct
            call_stop_loss_pct = put_stop_loss_pct = -1.0
        else:
            c7, c8 = st.columns(2)
            exit_threshold_pct = c7.number_input(
                "Umbral de ROI (%)",
                key="umbral_roi_pct",
                step=5.0, min_value=1.0,
            ) / 100.0
            stop_loss_pct = c8.number_input(
                "Stop loss (%)",
                value=-100.0, step=10.0, max_value=0.0, format="%.2f",
            ) / 100.0
            # Per-leg no usados en estos modos: defaults inocuos para el engine.
            call_exit_threshold_pct = put_exit_threshold_pct = exit_threshold_pct
            call_stop_loss_pct = put_stop_loss_pct = stop_loss_pct

    st.markdown("---")
    btn_iniciar = st.button(
        "Iniciar nueva simulación",
        type="primary",
        use_container_width=True,
        key="btn_iniciar",
        help="Resetea el estado y corre la primera iteración",
    )
    btn_proxima = st.button(
        "Próxima iteración",
        type="secondary",
        use_container_width=True,
        disabled=(not has_session) or session_exhausted,
        key="btn_proxima",
        help="Usa los parámetros actuales del sidebar para la siguiente iteración",
    )


def _validate_form() -> bool:
    if premium_min >= premium_max:
        st.error("Premium `Min` debe ser menor que `Max`.")
        return False
    if t_start >= t_end:
        st.error("La hora de inicio debe ser menor que la de fin.")
        return False
    if not (t_start <= hora_orden <= t_end):
        st.error(
            f"La 'Hora de orden' ({hora_orden:%H:%M}) debe estar dentro de la "
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
    return True


if btn_iniciar:
    if _validate_form():
        dl = get_downloader(api_key)
        if not is_range:
            # ---------- Single-day mode (comportamiento original) ----------
            with st.spinner(f"Bajando datos de Polygon para {ticker} {sel_date} y corriendo iteración 1..."):
                try:
                    expiry = validate_0dte_session(dl, ticker, sel_date.isoformat())
                    day_end_ts = _to_ts(sel_date.isoformat(), t_end)
                    order_ts = _to_ts(sel_date.isoformat(), hora_orden)
                    it1 = run_next_iteration(
                        dl, ticker, sel_date.isoformat(),
                        float(premium_min), float(premium_max),
                        float(invest_call), float(invest_put),
                        order_ts, day_end_ts,
                        exit_threshold_pct=float(exit_threshold_pct),
                        exit_metric=exit_metric,
                        stop_loss_pct=float(stop_loss_pct),
                        iteration_idx=1,
                        mode=engine_mode,
                        ext_min=float(ext_premium_min), ext_max=float(ext_premium_max),
                        check_step_min=int(sell_check_min),
                        call_exit_threshold_pct=float(call_exit_threshold_pct),
                        call_stop_loss_pct=float(call_stop_loss_pct),
                        put_exit_threshold_pct=float(put_exit_threshold_pct),
                        put_stop_loss_pct=float(put_stop_loss_pct),
                        exit_plus_threshold_pct=float(exit_plus_threshold_pct),
                        exit_plus_time=exit_plus_time,
                    )
                except NoMatchError as e:
                    st.error(str(e))
                    st.info("Probá ampliar el rango de premium.")
                    st.stop()
                except Exception as e:
                    st.error(f"Error: {e}")
                    st.stop()
            st.session_state["replay"] = {
                "ticker": ticker,
                "mode": "single",
                "date": sel_date.isoformat(),
                "expiry": expiry,
                "time_start": t_start,
                "time_end": t_end,
                "order_time": hora_orden,
                "day_start_ts": order_ts,
                "day_end_ts": day_end_ts,
                "iterations": [it1],
            }
            _schedule_hora_orden_sync(st.session_state["replay"], t_start, t_end)
            st.rerun()
        else:
            # ---------- Date range batch mode: 1 iteración por día hábil ----------
            # `freq="B"` = business days; salta sábados/domingos. Días sin chain
            # 0 DTE o sin datos se reportan como errores por día (no rompen el batch).
            day_list = [
                pd.Timestamp(d).date()
                for d in pd.date_range(sel_start, sel_end, freq="B")
            ]
            if not day_list:
                st.error("El rango no contiene ningún día hábil.")
                st.stop()
            day_runs: list[dict] = []
            # Placeholders para actualización en vivo: primero el panel de
            # totales preliminares, luego la barra de progreso.
            totals_placeholder = st.empty()
            progress = st.progress(0.0, text=f"Backtest {ticker} sobre {len(day_list)} días...")
            # Modo rango: usamos los MISMOS Parámetros por iteración VISIBLES del
            # panel (Inversión $, CALL%/PUT%, Modo, Umbral ROI, Stop loss) para TODOS
            # los días. Son editables a mano: lo que se ve es lo que se aplica.
            _b_call_alloc = int(round(float(st.session_state.get("call_pct", 50.0))))
            _b_put_alloc = int(round(float(st.session_state.get("put_pct", 50.0))))
            _b_roi_pct_int = int(round(float(exit_threshold_pct) * 100))
            _label_m, _color_m, _bg_m, _border_m = _classify_prob(manual_prob, _predictor_cfg)
            _pred_info = {
                "probability": manual_prob, "label": _label_m, "color": _color_m,
                "sample_size": 0, "reason": "",
            }
            _params_info = {
                "mode": engine_mode, "call_alloc": _b_call_alloc,
                "put_alloc": _b_put_alloc, "roi_threshold": _b_roi_pct_int,
            }

            _skipped_no0dte = 0
            for i, d in enumerate(day_list):
                date_str = d.isoformat()
                day_end_ts = _to_ts(date_str, t_end)
                order_ts = _to_ts(date_str, hora_orden)

                _was_skip = False
                try:
                    expiry = validate_0dte_session(dl, ticker, date_str)
                    it = run_next_iteration(
                        dl, ticker, date_str,
                        float(premium_min), float(premium_max),
                        float(invest_call), float(invest_put),
                        order_ts, day_end_ts,
                        exit_threshold_pct=float(exit_threshold_pct),
                        exit_metric="total",
                        stop_loss_pct=float(stop_loss_pct),
                        iteration_idx=1,
                        mode=engine_mode,
                        ext_min=float(ext_premium_min), ext_max=float(ext_premium_max),
                        check_step_min=int(sell_check_min),
                        call_exit_threshold_pct=float(call_exit_threshold_pct),
                        call_stop_loss_pct=float(call_stop_loss_pct),
                        put_exit_threshold_pct=float(put_exit_threshold_pct),
                        put_stop_loss_pct=float(put_stop_loss_pct),
                        exit_plus_threshold_pct=float(exit_plus_threshold_pct),
                        exit_plus_time=exit_plus_time,
                    )
                    day_runs.append({
                        "date": date_str,
                        "expiry": expiry,
                        "day_start_ts": order_ts,
                        "day_end_ts": day_end_ts,
                        "iteration": it,
                        "error": None,
                        "prediction": _pred_info,
                        "day_params": _params_info,
                    })
                except NoMatchError as e:
                    day_runs.append({
                        "date": date_str, "expiry": None,
                        "day_start_ts": order_ts, "day_end_ts": day_end_ts,
                        "iteration": None, "error": f"NoMatch: {e}",
                        "prediction": _pred_info, "day_params": _params_info,
                    })
                except ValueError as e:
                    # Día sin 0DTE (tickers weekly como SOXL/MSTR: solo viernes tienen
                    # 0DTE) → SALTAR silenciosamente, NO listarlo como error. Otros
                    # ValueError (sin data, etc.) sí se reportan como error real.
                    if "No 0 DTE option" in str(e):
                        _skipped_no0dte += 1
                        _was_skip = True
                    else:
                        day_runs.append({
                            "date": date_str, "expiry": None,
                            "day_start_ts": order_ts, "day_end_ts": day_end_ts,
                            "iteration": None, "error": str(e),
                            "prediction": _pred_info, "day_params": _params_info,
                        })
                except Exception as e:
                    day_runs.append({
                        "date": date_str, "expiry": None,
                        "day_start_ts": order_ts, "day_end_ts": day_end_ts,
                        "iteration": None, "error": str(e),
                        "prediction": _pred_info, "day_params": _params_info,
                    })
                # Refresca el panel de totales (solo si el día NO fue saltado: en un
                # skip los day_runs no cambian, así que no hace falta re-renderizar).
                if not _was_skip:
                    with totals_placeholder.container():
                        render_batch_totals(
                            day_runs,
                            total_days=len(day_list),
                            title="💼 Totales del backtest (preliminar)",
                        )
                progress.progress(
                    (i + 1) / len(day_list),
                    text=(f"Backtest {ticker}: {i + 1}/{len(day_list)} días"
                          + (f"  ·  {_skipped_no0dte} sin 0DTE saltados"
                             if _skipped_no0dte else f" procesados ({date_str})")),
                )
            progress.empty()
            totals_placeholder.empty()
            st.session_state["replay"] = {
                "ticker": ticker,
                "mode": "range",
                "date_start": sel_start.isoformat(),
                "date_end": sel_end.isoformat(),
                "time_start": t_start,
                "time_end": t_end,
                "order_time": hora_orden,
                "day_runs": day_runs,
                "skipped_no0dte": _skipped_no0dte,
            }
            st.rerun()

elif btn_proxima:
    if not has_session:
        st.error("Iniciá una sesión primero.")
        st.stop()
    if replay_state.get("mode") == "range":
        st.error("La 'Próxima iteración' no aplica en modo rango — iniciá una nueva simulación.")
        st.stop()
    if _validate_form():
        # Próxima iteración: usar la Hora/Minuto que el usuario tiene en el
        # widget — no el auto-calculado end_dt+1min. El auto-reset post-iteración
        # SUGIERE next_ts en el widget, pero el usuario puede sobreescribirlo
        # antes de clickear "Próxima iteración".
        next_ts = _to_ts(replay_state["date"], hora_orden)
        if next_ts >= replay_state["day_end_ts"]:
            st.warning(
                f"La 'Hora de orden' ({hora_orden:%H:%M}) está fuera de la "
                f"ventana de sesión — ajustá el valor y reintentá."
            )
        else:
            with st.spinner(f"Corriendo iteración {len(replay_state['iterations']) + 1} desde las {next_ts:%H:%M}..."):
                try:
                    dl = get_downloader(api_key)
                    it_next = run_next_iteration(
                        dl,
                        replay_state["ticker"],
                        replay_state["date"],
                        float(premium_min), float(premium_max),
                        float(invest_call), float(invest_put),
                        next_ts,
                        replay_state["day_end_ts"],
                        exit_threshold_pct=float(exit_threshold_pct),
                        exit_metric=exit_metric,
                        stop_loss_pct=float(stop_loss_pct),
                        iteration_idx=len(replay_state["iterations"]) + 1,
                        mode=engine_mode,
                        ext_min=float(ext_premium_min), ext_max=float(ext_premium_max),
                        check_step_min=int(sell_check_min),
                        call_exit_threshold_pct=float(call_exit_threshold_pct),
                        call_stop_loss_pct=float(call_stop_loss_pct),
                        put_exit_threshold_pct=float(put_exit_threshold_pct),
                        put_stop_loss_pct=float(put_stop_loss_pct),
                        exit_plus_threshold_pct=float(exit_plus_threshold_pct),
                        exit_plus_time=exit_plus_time,
                    )
                except NoMatchError as e:
                    st.error(str(e))
                    st.info("Probá ampliar el rango de premium para esta iteración.")
                    st.stop()
                except Exception as e:
                    st.error(f"Error: {e}")
                    st.stop()
            replay_state["iterations"].append(it_next)
            st.session_state["replay"] = replay_state
            _schedule_hora_orden_sync(replay_state, t_start, t_end)
            st.rerun()

replay_state = st.session_state.get("replay")
if replay_state is None:
    st.info("Configurá los parámetros en la barra lateral y pulsá **Iniciar nueva simulación**.")
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
    # Minutos transcurridos desde la APERTURA del mercado (09:30 ET) hasta cada
    # fila — útil para saber a qué minuto de la sesión ocurrió cada paso. Se
    # calcula con el timestamp aún en datetime (antes de pasarlo a HH:MM string).
    _open = tdf["timestamp"].dt.normalize() + pd.Timedelta(hours=9, minutes=30)
    tdf["Min mercado"] = ((tdf["timestamp"] - _open).dt.total_seconds() / 60.0).round().astype(int)
    # Timestamp → solo hora:minuto (HH:MM). Cada iteración es de un día, así que
    # el string ordena bien al clickear el header.
    tdf["timestamp"] = tdf["timestamp"].dt.strftime("%H:%M")
    return tdf.rename(columns={
        "timestamp": "Timestamp",
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
    })[["Timestamp", "Min mercado", "Spot",
         "Px Call", "ROI (%) CALL", "ROI ($) CALL", "Capital CALL", "Capital acum CALL",
         "Px Put", "ROI (%) PUT", "ROI ($) PUT", "Capital PUT", "Capital acum PUT",
         "ROI (%)", "ROI ($)", "$ Total"]]


METRIC_COLUMN = {
    "total": "ROI (%)",
    "call": "ROI (%) CALL",
    "put": "ROI (%) PUT",
}

DARK_GREEN_STYLE = "background-color: #2e7d32; color: white; font-weight: bold"


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

    # Modos "CALL o PUT" / "(plus)": cada pierna marca su propia celda de venta
    # (verde oscuro). La columna ROI combinada NO marca umbral.
    if getattr(it, "mode", "") in ("call_or_put", "call_or_put_plus"):
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
            .format(fmt)
        )
        return styler.set_properties(**{"text-align": "center"})

    if exit_metric == "total":
        styler = (
            display_df.style
            .map(_color_leg_pct, subset=["ROI (%) CALL", "ROI (%) PUT",
                                          "ROI ($) CALL", "ROI ($) PUT", "ROI ($)"])
            .apply(total_styler, subset=["ROI (%)"])
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

    md = ["**🟢 Compra (apertura)**", *buy_lines,
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

    # Bid/Ask/Spread + tier de rango por pierna (si hay NBBO disponible).
    def _leg_quote_line(label, bid, ask, spread, tier):
        if bid is None or ask is None:
            return f"<b>{label}:</b> <span style='color:#999'>sin quote</span>"
        _tier_badge = {
            "optimo": "<span style='color:#2e7d32'>óptimo</span>",
            "extended": "<span style='color:#e65100'>extendido</span>",
            "fallback": "<span style='color:#b71c1c'>fallback</span>",
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
        f"""
        <div style='
            border: 1px solid rgba(49, 51, 63, 0.2);
            border-radius: 0.5rem;
            padding: 0.85rem 1rem;
            background-color: {it_bg};
        '>
            <div style='font-size: 0.85rem; font-weight: bold; color: rgba(49, 51, 63, 0.65); margin-bottom: 0.35rem;'>
                Ganancia total
            </div>
            <div style='font-size: 1.5rem; font-weight: 600; line-height: 1.15;'>
                ${it.gain_total:+,.2f}
            </div>
            <div style='font-size: 0.85rem; color: {it_dc}; margin-top: 0.25rem;'>
                {it_delta_txt}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    mc[4].metric("Combined % exit", f"{it.pnl_pct_combined:+.1%}")
    mc[5].metric("Capital acumulado", f"${it.invest_total + it.gain_total:,.2f}")

    with st.expander("📑 Operaciones (contratos · comisión · ROI neto)", expanded=False):
        render_ops_report(it)

    with st.expander(
        f"Strikes probados (CALL: {len(it.call_probes)} · PUT: {len(it.put_probes)})",
        expanded=False,
    ):
        _it_mode = getattr(it, "mode", "both")
        _show_call_probes = _it_mode != "put_only"
        _show_put_probes = _it_mode != "call_only"
        if _show_call_probes and _show_put_probes:
            pc1, pc2 = st.columns(2)
        elif _show_call_probes:
            pc1 = st.container()
            pc2 = None
        else:
            pc1 = None
            pc2 = st.container()
        def _probe_row(p):
            return {
                "strike": p.strike,
                "open premium": p.opening_premium,
                "bid": getattr(p, "bid", None),
                "ask": getattr(p, "ask", None),
                "spread": getattr(p, "spread", None),
                "spread ok": getattr(p, "spread_ok", None),
                "óptimo": p.in_range,
                "extendido": getattr(p, "in_extended", None),
            }
        def _probes_styled(probes, selected_strike):
            """DataFrame de probes con la fila del strike SELECCIONADO resaltada
            en azul claro."""
            pdf = pd.DataFrame([_probe_row(p) for p in probes])
            if pdf.empty:
                return pdf
            def _hl_selected(row):
                try:
                    is_sel = abs(float(row["strike"]) - float(selected_strike)) < 1e-9
                except (TypeError, ValueError):
                    is_sel = False
                return ["background-color: #cce5ff; font-weight: bold" if is_sel else "" for _ in row]
            return pdf.style.apply(_hl_selected, axis=1)

        if pc1 is not None:
            pc1.markdown("**CALL probes**")
            pc1.dataframe(_probes_styled(it.call_probes, it.call_strike),
                          use_container_width=True, height=200)
        if pc2 is not None:
            pc2.markdown("**PUT probes**")
            pc2.dataframe(_probes_styled(it.put_probes, it.put_strike),
                          use_container_width=True, height=200)

    # Key único por (fecha, iteración) — en range mode todos los días tienen
    # iteration=1, así que necesitamos la fecha para evitar colisiones.
    _key_suffix = f"{date}_{it.iteration}"
    with st.expander("📈 Gráfico", expanded=False):
        st.plotly_chart(build_chart(it), use_container_width=True, key=f"chart_iter_{_key_suffix}")

    display_df = _build_display_df(it)
    styled_df = _style_display_df(
        display_df,
        it.exit_threshold_pct,
        getattr(it, "exit_metric", "total"),
        getattr(it, "stop_loss_pct", 1.0),
        it=it,
    )

    # Paso real entre filas (en minutos), derivado de los timestamps. Con
    # "Sell verification (min)" > 1 la tabla avanza de a N minutos.
    _step_min = 1
    if getattr(it, "df", None) is not None and len(it.df) > 1:
        _d = it.df["timestamp"].diff().dropna().dt.total_seconds().div(60).round()
        if len(_d):
            _step_min = int(_d.median())
    _tbl_title = (
        "📋 Tabla minuto a minuto" if _step_min <= 1
        else f"📋 Tabla (paso {_step_min} min)"
    )
    with st.expander(_tbl_title, expanded=False):
        total_rows = len(display_df)
        row_options = sorted(set([n for n in [15, 30, 60, 120, 240, 390] if n < total_rows] + [total_rows]))
        if len(row_options) > 1:
            default_n = 30 if 30 in row_options else row_options[0]
            rows_to_show = st.select_slider(
                f"Filas visibles  (total: {total_rows})",
                options=row_options,
                value=default_n,
                format_func=lambda n: f"{n} filas" if n < total_rows else f"todas ({total_rows})",
                key=f"rows_iter_{_key_suffix}",
            )
        else:
            st.caption(f"Filas: {total_rows}")
            rows_to_show = total_rows
        table_height = 38 + 35 * max(min(rows_to_show, total_rows), 1)
        # st.dataframe: mantiene ordenamiento al click en el header + búsqueda.
        # El Styler centra las CELDAS (el grid no centra los headers, trade-off
        # aceptado a cambio de conservar la interactividad).
        st.dataframe(styled_df, use_container_width=True, height=table_height)

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
    if pd.api.types.is_datetime64_any_dtype(excel_df["Timestamp"]) and excel_df["Timestamp"].dt.tz is not None:
        excel_df["Timestamp"] = excel_df["Timestamp"].dt.tz_localize(None)
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
ticker_str = replay_state["ticker"]
_mode = replay_state.get("mode", "single")

_REASON_LABELS = {
    "100%_threshold": "Exit por umbral de profit",
    "stop_loss": "Exit por STOP LOSS",
    "session_end": "Sin trigger — corre hasta cierre",
}
_REASON_ICONS = {
    "100%_threshold": "🎯",
    "stop_loss": "🛑",
    "session_end": "🕓",
}

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

    # Panel de totales — usa el mismo helper que se llama en vivo durante el
    # batch, así garantizamos que la vista preliminar y la final sean idénticas.
    render_batch_totals(
        day_runs,
        total_days=len(day_runs),
        title="💼 Totales del backtest",
    )
    _n_skipped = int(replay_state.get("skipped_no0dte", 0))
    if _n_skipped:
        st.caption(
            f"ℹ️ Se saltaron **{_n_skipped}** días sin 0DTE (ticker weekly: solo los "
            f"viernes vencen el mismo día). No cuentan como error ni en los totales."
        )

    # ------------------------------------------------------------------
    # Días con resultados — tabla + descargas dentro de un expander
    # ------------------------------------------------------------------
    selected_dates: list[str] = []
    with st.expander(f"📋 Días con resultados ({len(successful)})", expanded=True):
        st.markdown("### Resumen por día")
        if successful:
            rows = []
            cum_gain = 0.0
            for r in successful:
                it = r["iteration"]
                day_roi = (it.gain_total / it.invest_total) if it.invest_total else 0.0
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
                _mode_str = {
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
                    "Fecha": r["date"],
                    "Entrada": it.start_dt.strftime("%H:%M"),
                    "Salida": it.end_dt.strftime("%H:%M"),
                    "Razón": _reason_cell,
                    # === Columnas nuevas de Predicción Apertura ===
                    "Predicción": _pred_str,
                    "Mode": _mode_str,
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

            # Filtro por ROI — mantiene "ROI ($) acumulado" sobre la vista
            # original (no se recalcula sobre el subset, para que siga reflejando
            # el backtest real).
            _n_pos = int((summary_df["ROI (%)"] >= 0).sum())
            _n_neg = int((summary_df["ROI (%)"] < 0).sum())
            _roi_filter = st.radio(
                "Filtrar filas",
                options=["Todas", "ROI ≥ 0", "ROI < 0"],
                index=0,
                horizontal=True,
                key="batch_roi_filter",
                format_func=lambda o: {
                    "Todas": f"Todas ({len(summary_df)})",
                    "ROI ≥ 0": f"ROI ≥ 0 ({_n_pos})",
                    "ROI < 0": f"ROI < 0 ({_n_neg})",
                }[o],
            )
            if _roi_filter == "ROI ≥ 0":
                view_df = summary_df[summary_df["ROI (%)"] >= 0].copy()
            elif _roi_filter == "ROI < 0":
                view_df = summary_df[summary_df["ROI (%)"] < 0].copy()
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
                # Insertar la columna "Ver detalles" (bool) como primera columna
                # — será un CheckboxColumn editable en el data_editor.
                editor_df = view_df.copy().reset_index(drop=True)
                editor_df.insert(0, "Ver detalles", False)

                styled_editor = (
                    editor_df.style
                    .map(_color_ganancia, subset=["ROI (%)", "ROI ($)"])
                    .format({
                        "Spot @ entrada": "${:,.2f}",
                        "Inversión": "${:,.2f}",
                        "ROI (%)": "{:+.1%}",
                        "ROI ($)": "${:+,.2f}",
                        "ROI ($) acumulado": "${:+,.2f}",
                        "Strike Call": "{:g}",
                        "Strike Put": "{:g}",
                        # Columnas nuevas de predicción
                        "CALL %": "{:.0f}",
                        "PUT %": "{:.0f}",
                        "Umbral ROI": "{:.0f}",
                    })
                )

                # Todas las columnas excepto "Ver detalles" son read-only.
                _disabled_cols = [c for c in editor_df.columns if c != "Ver detalles"]
                # Incluyo el filtro en el key para que al cambiar de filtro el
                # editor resetee su estado interno (evita "checks fantasmas" en
                # índices de fila que cambiaron de día).
                _editor_key = f"batch_summary_editor_{_roi_filter}"

                edited = st.data_editor(
                    styled_editor,
                    use_container_width=True,
                    height=min(38 + 35 * len(editor_df), 600),
                    hide_index=True,
                    disabled=_disabled_cols,
                    key=_editor_key,
                    column_config={
                        "Ver detalles": st.column_config.CheckboxColumn(
                            "Ver detalles",
                            help=(
                                "Marcá las filas cuyo detalle (gráfico + tabla "
                                "minuto a minuto) quieras ver abajo, en paneles "
                                "colapsables. Sólo se renderiza lo seleccionado."
                            ),
                            default=False,
                        ),
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

                selected_dates = list(edited.loc[edited["Ver detalles"], "Fecha"])

                # Leyenda visible de los iconos de la columna "Razón".
                st.caption(
                    "**Razón:**  🎯 Exit por umbral de profit  ·  "
                    "🛑 Exit por STOP LOSS  ·  🕓 Sin trigger (cierre de sesión)"
                )

            # Descarga CSV / Excel del resumen — exporta SOLO las filas que pasan
            # el filtro de ROI (Todas / ROI ≥ 0 / ROI < 0), es decir `view_df`.
            _filter_tag = {"ROI ≥ 0": "ROIpos", "ROI < 0": "ROIneg"}.get(_roi_filter, "todas")
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

    # Render lazy: 1 expander colapsado por cada día marcado en "Ver detalles".
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
            _fb_tag = "  ·  ⚠ fallback" if (it.call_fallback or it.put_fallback) else ""
            _exp_title = (
                f"{_icon} {sel_fecha}  ·  {it.start_dt:%H:%M} → {it.end_dt:%H:%M}  ·  "
                f"{_reason}  ·  Ganancia: {_gain_part}{_fb_tag}"
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
    f"""
    <div style='
        border: 1px solid rgba(49, 51, 63, 0.2);
        border-radius: 0.5rem;
        padding: 0.85rem 1rem;
        background-color: {gain_bg};
    '>
        <div style='font-size: 0.85rem; font-weight: bold; color: rgba(49, 51, 63, 0.65); margin-bottom: 0.35rem;'>
            Ganancia total
        </div>
        <div style='font-size: 1.75rem; font-weight: 600; line-height: 1.15;'>
            ${total_gain:+,.2f}
        </div>
        <div style='font-size: 0.85rem; color: {gain_delta_color}; margin-top: 0.25rem;'>
            {gain_delta_txt}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)
tc[3].metric("Capital final", f"${final_capital:,.2f}")
tc[4].metric("Triggers ejecutados", f"{n_trig} / {len(iterations)}")

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
    _fb_tag = ""
    if getattr(it, "call_fallback", False) or getattr(it, "put_fallback", False):
        _fb_tag = "  ·  ⚠ fallback"
    _exp_title = (
        f"{_icon} Iteración {it.iteration}  ·  {it.start_dt:%H:%M} → {it.end_dt:%H:%M}  ·  "
        f"{_reason}  ·  Ganancia: {_gain_part}{_fb_tag}"
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
    # Snap el next_ts a la grilla del sidebar para que el cartel y los
    # selectboxes Hora/Minuto muestren exactamente el mismo valor.
    _msg_step = int(st.session_state.get("hora_orden_step", 5))
    _msg_h, _msg_m = _snap_to_orden_grid(
        next_ts, replay_state["time_start"], replay_state["time_end"], _msg_step
    )
    _next_display = f"{_msg_h:02d}:{_msg_m:02d}"
    st.success(
        f"**Próxima iteración listo a las {_next_display}** — "
        f"ajustá los parámetros en la barra lateral si querés y pulsá **Próxima iteración**."
    )

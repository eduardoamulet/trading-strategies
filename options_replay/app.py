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
from engine import NoMatchError, ReplayResult, replay_session  # noqa: E402
from analytics import session_stats  # noqa: E402

DATA_DIR = HERE / "data"


def load_api_key() -> str:
    try:
        import config  # type: ignore
        return getattr(config, "POLYGON_API_KEY", "")
    except Exception:
        return ""


@st.cache_resource
def get_downloader(api_key: str) -> Downloader:
    return Downloader(PolygonAdapter(api_key), DATA_DIR)


@st.cache_data(show_spinner=False)
def run(api_key: str, ticker: str, date_str: str,
        premium_min: float, premium_max: float,
        t_start_str: str, t_end_str: str) -> dict:
    dl = get_downloader(api_key)
    t_start = time_cls.fromisoformat(t_start_str)
    t_end = time_cls.fromisoformat(t_end_str)
    res = replay_session(dl, ticker, date_str, premium_min, premium_max, t_start, t_end)
    return {"result": res}


def build_chart(res: ReplayResult) -> go.Figure:
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

    # Marcador único: primera vez que (% Call + % Put) > 100%
    pct_call = (df["call_px"] - res.call_entry_premium) / res.call_entry_premium
    pct_put = (df["put_px"] - res.put_entry_premium) / res.put_entry_premium
    pct_total = pct_call + pct_put
    over_mask = pct_total > 1.0
    if over_mask.any():
        first_idx = over_mask.idxmax()
        fig.add_trace(go.Scatter(
            x=[df.loc[first_idx, "timestamp"]],
            y=[df.loc[first_idx, "total"]],
            mode="markers",
            name="Primera vez > 100%",
            marker=dict(
                symbol="star",
                size=18,
                color="#2e7d32",
                line=dict(color="white", width=2),
            ),
            yaxis="y2",
            hovertemplate=(
                "<b>Primera vez > 100%</b><br>"
                "%{x|%H:%M}<br>"
                "Total prima: $%{y:.2f}<br>"
                "% combinado: %{customdata:+.1%}<extra></extra>"
            ),
            customdata=[pct_total.loc[first_idx]],
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


# ============================================================================
# App
# ============================================================================
st.set_page_config(page_title="Options Replay — 0 DTE", layout="wide")
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

with st.sidebar.form("params"):
    ticker = st.text_input("Ticker", value="SPY").strip().upper()

    default_date = date_cls.today() - timedelta(days=1)
    sel_date = st.date_input("Fecha", value=default_date, format="YYYY-MM-DD")

    st.markdown(
        "**Rango óptimo del premium del contrato (USD por contrato)**  \n"
        "_Es el costo de la opción, no el precio del subyacente. "
        "Para SPY 0 DTE cerca de ATM suele estar entre $0.40 y $5.00._"
    )
    c1, c2 = st.columns(2)
    premium_min = c1.number_input("Min (USD)", value=1.50, step=0.25, min_value=0.0, format="%.2f")
    premium_max = c2.number_input("Max (USD)", value=4.00, step=0.25, min_value=0.0, format="%.2f")

    st.markdown("**Ventana horaria (ET)**")
    c3, c4 = st.columns(2)
    t_start = c3.time_input("Inicio", value=time_cls(9, 30))
    t_end = c4.time_input("Fin", value=time_cls(16, 0))

    st.markdown("**Inversión por pierna (USD)**")
    c5, c6 = st.columns(2)
    invest_call = c5.number_input("CALL ($)", value=1000.0, step=100.0, min_value=0.0, format="%.2f")
    invest_put = c6.number_input("PUT ($)", value=1000.0, step=100.0, min_value=0.0, format="%.2f")

    submit = st.form_submit_button("Ejecutar replay", type="primary", use_container_width=True)

if submit:
    if premium_min >= premium_max:
        st.error("Premium `Min` debe ser menor que `Max`.")
        st.stop()
    if t_start >= t_end:
        st.error("La hora de inicio debe ser menor que la de fin.")
        st.stop()
    with st.spinner(f"Bajando datos de Polygon y buscando contratos 0 DTE para {ticker} {sel_date}..."):
        try:
            packed = run(api_key, ticker, sel_date.isoformat(),
                         float(premium_min), float(premium_max),
                         t_start.isoformat(), t_end.isoformat())
        except NoMatchError as e:
            st.error(str(e))
            st.info("Probá ampliar el rango de premium o cambiar la fecha.")
            st.stop()
        except Exception as e:
            st.error(f"Error: {e}")
            st.stop()
    packed["invest_call"] = float(invest_call)
    packed["invest_put"] = float(invest_put)
    st.session_state["packed"] = packed

packed = st.session_state.get("packed")
if not packed:
    st.info("Configurá los parámetros en la barra lateral y pulsá **Ejecutar replay**.")
    st.stop()

res: ReplayResult = packed["result"]
invest_call = packed.get("invest_call", 1000.0)
invest_put = packed.get("invest_put", 1000.0)
stats = session_stats(res)

# ----- Header card -----
st.subheader(f"{res.ticker} — {res.date}  ·  0 DTE (expiry {res.expiry})")
hc = st.columns(4)
hc[0].markdown(f"**Spot al inicio**\n\n${res.spot_at_start:.2f}")
hc[1].markdown(
    f"**CALL elegido**\n\nstrike {res.call_strike:g}\n\nentry premium ${res.call_entry_premium:.2f}"
)
hc[2].markdown(
    f"**PUT elegido**\n\nstrike {res.put_strike:g}\n\nentry premium ${res.put_entry_premium:.2f}"
)
hc[3].markdown(f"**Rango premium / ventana**\n\n"
               f"${res.premium_min:.2f}–${res.premium_max:.2f}\n\n"
               f"{res.time_start.strftime('%H:%M')}–{res.time_end.strftime('%H:%M')}")

st.markdown(f"`{res.call_occ}`  ·  `{res.put_occ}`")

with st.expander(f"Strikes probados (CALL: {len(res.call_probes)} · PUT: {len(res.put_probes)})", expanded=False):
    pc1, pc2 = st.columns(2)
    pc1.markdown("**CALL probes**")
    pc1.dataframe(pd.DataFrame([
        {"strike": p.strike, "open premium": p.opening_premium, "in range": p.in_range}
        for p in res.call_probes
    ]), use_container_width=True, height=200)
    pc2.markdown("**PUT probes**")
    pc2.dataframe(pd.DataFrame([
        {"strike": p.strike, "open premium": p.opening_premium, "in range": p.in_range}
        for p in res.put_probes
    ]), use_container_width=True, height=200)

# ----- Metrics -----
st.subheader("Resultados de la sesión")
mc = st.columns(5)
mc[0].metric("Costo inicial (C+P)", f"${stats['initial_total']:.2f}")
mc[1].metric("Final", f"${stats['final_total']:.2f}",
             delta=f"{stats['pnl_final_pct']:+.1%}")
mc[2].metric("Max alcanzado", f"${stats['max_total']:.2f}",
             delta=f"{stats['pnl_max_pct']:+.1%}")
mc[3].metric("Min alcanzado", f"${stats['min_total']:.2f}",
             delta=f"{stats['pnl_min_pct']:+.1%}")
mc[4].metric("Rango spot", f"${stats['spot_min']:.2f} – ${stats['spot_max']:.2f}",
             delta=f"{stats['spot_range_pct']:+.2%}")

mc2 = st.columns(3)
mc2[0].metric("Minutos al pico", f"{stats['minutes_to_peak']} min")
mc2[1].metric("Minutos al fondo", f"{stats['minutes_to_trough']} min")
mc2[2].metric("# barras", stats["n_minutes"])

# ----- Chart -----
st.plotly_chart(build_chart(res), use_container_width=True)

# ----- Table -----
st.subheader("Tabla minuto a minuto")
st.markdown(
    f"<div style='padding:8px 12px; background:#f0f2f6; border-radius:6px; "
    f"display:inline-block; margin-bottom:8px;'>"
    f"<b>Strike Call:</b> {res.call_strike:g}  ·  "
    f"<b>Strike Put:</b> {res.put_strike:g}"
    f"</div>",
    unsafe_allow_html=True,
)

table_df = res.df.copy()
table_df["pct_call"] = (table_df["call_px"] - res.call_entry_premium) / res.call_entry_premium
table_df["pct_put"] = (table_df["put_px"] - res.put_entry_premium) / res.put_entry_premium
table_df["pct_total"] = table_df["pct_call"] + table_df["pct_put"]
table_df["val_call"] = invest_call * (1.0 + table_df["pct_call"])
table_df["val_put"] = invest_put * (1.0 + table_df["pct_put"])
table_df["val_total"] = table_df["val_call"] + table_df["val_put"]
# Suma corrida del delta minuto a minuto por leg (PnL acumulado en USD)
table_df["cum_delta_call"] = table_df["val_call"].diff().fillna(0.0).cumsum()
table_df["cum_delta_put"] = table_df["val_put"].diff().fillna(0.0).cumsum()

display_df = table_df.rename(columns={
    "timestamp": "Timestamp",
    "spot": "Spot",
    "call_px": "Px Call",
    "pct_call": "% Call",
    "val_call": "Capital CALL",
    "cum_delta_call": "Capital acum CALL",
    "put_px": "Px Put",
    "pct_put": "% Put",
    "val_put": "Capital PUT",
    "cum_delta_put": "Capital acum PUT",
    "pct_total": "Total % Put + % Call",
    "val_total": "$ Total",
})[["Timestamp", "Spot",
     "Px Call", "% Call", "Capital CALL", "Capital acum CALL",
     "Px Put", "% Put", "Capital PUT", "Capital acum PUT",
     "Total % Put + % Call", "$ Total"]]


def _color_leg_pct(v):
    if pd.isna(v):
        return ""
    if v > 1.0:   # > 100%
        return "background-color: #c8e6c9"  # verde claro
    if v < 0.5:   # < 50%
        return "background-color: #ffcdd2"  # rojo claro
    return ""


def _style_total_pct_column(col):
    styles = []
    first_over_seen = False
    for v in col:
        if pd.isna(v):
            styles.append("")
            continue
        if v > 1.0:
            if not first_over_seen:
                styles.append("background-color: #2e7d32; color: white; font-weight: bold")
                first_over_seen = True
            else:
                styles.append("background-color: #c8e6c9")
        elif v < 0.5:
            styles.append("background-color: #ffcdd2")
        else:
            styles.append("")
    return styles


styled_df = (
    display_df.style
    .map(_color_leg_pct, subset=["% Call", "% Put"])
    .apply(_style_total_pct_column, subset=["Total % Put + % Call"])
    .format({"% Call": "{:+.1%}", "% Put": "{:+.1%}",
             "Total % Put + % Call": "{:+.1%}",
             "Spot": "{:.2f}", "Px Call": "{:.2f}", "Px Put": "{:.2f}",
             "Capital CALL": "${:,.2f}", "Capital PUT": "${:,.2f}", "$ Total": "${:,.2f}",
             "Capital acum CALL": "{:+,.2f}",
             "Capital acum PUT": "{:+,.2f}"})
)

total_rows = len(display_df)
row_options = [n for n in [15, 30, 60, 120, 240, 390] if n < total_rows] + [total_rows]
default_idx = row_options.index(30) if 30 in row_options else 0
rows_to_show = st.select_slider(
    f"Filas visibles  (total: {total_rows})",
    options=row_options,
    value=row_options[default_idx],
    format_func=lambda n: f"{n} filas" if n < total_rows else f"todas ({total_rows})",
)
table_height = 38 + 35 * min(rows_to_show, total_rows)
st.dataframe(styled_df, use_container_width=True, height=table_height)

# ----- Downloads -----
st.subheader("Descargas")
dc = st.columns(2)

csv_bytes = display_df.to_csv(index=False).encode("utf-8")
dc[0].download_button(
    "CSV — tabla minuto a minuto",
    csv_bytes,
    file_name=f"{res.ticker}_{res.date}_{res.time_start:%H%M}-{res.time_end:%H%M}.csv",
    mime="text/csv",
)

buf = io.BytesIO()
excel_df = display_df.copy()
if pd.api.types.is_datetime64_any_dtype(excel_df["Timestamp"]) and excel_df["Timestamp"].dt.tz is not None:
    excel_df["Timestamp"] = excel_df["Timestamp"].dt.tz_localize(None)
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    pd.DataFrame([{
        "ticker": res.ticker, "date": res.date, "expiry": res.expiry,
        "premium_min": res.premium_min, "premium_max": res.premium_max,
        "spot_at_start": res.spot_at_start,
        "call_strike": res.call_strike, "call_occ": res.call_occ,
        "call_entry_premium": res.call_entry_premium,
        "put_strike": res.put_strike, "put_occ": res.put_occ,
        "put_entry_premium": res.put_entry_premium,
        **stats,
    }]).to_excel(xw, sheet_name="summary", index=False)
    excel_df.to_excel(xw, sheet_name="minute_table", index=False)
dc[1].download_button(
    "Excel — resumen + tabla",
    buf.getvalue(),
    file_name=f"{res.ticker}_{res.date}_replay.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

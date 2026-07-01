"""Componentes de gráfico compartidos de SignalForge.

`_render_lwc_chart` es el gráfico de velas (TradingView Lightweight Charts, datos propios de Polygon)
que usa el **Backtesting** — extraído acá para que otras vistas (p.ej. «Simulación Intradía») reusen
EXACTAMENTE el mismo componente sin duplicar código. Candlestick + Bandas de Bollinger (20,2σ) + SMA
central + marcador en la hora. Datos embebidos: no salen a ningún servicio externo.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

_LWC_TF = {"1m": None, "5m": "5min", "15m": "15min", "30m": "30min", "1h": "60min"}


def _render_lwc_chart(dl, ticker: str, date: str, hora: str, tf: str, key: str) -> None:
    """Velas del subyacente (TradingView Lightweight Charts, datos propios de Polygon) a la
    temporalidad `tf`, con **Bandas de Bollinger (20, 2σ) + media móvil central**, una vista
    AÉREA (toda la sesión del día → muchas velas) y un marcador en `hora` (HH:MM ET). Para
    que las BB tengan lookback se traen también días hábiles previos (solo para el cálculo).
    Datos embebidos: no salen a ningún servicio externo."""
    import json as _j

    def _utc(ts) -> int:   # wall-clock ET de ts como UTC → LWC (muestra UTC) dibuja hora ET
        et = ts.tz_convert("America/New_York") if getattr(ts, "tzinfo", None) else ts
        return int(pd.Timestamp(et.strftime("%Y-%m-%d %H:%M:%S"), tz="UTC").timestamp())

    try:
        _d0 = pd.Timestamp(date).normalize()
    except Exception:  # noqa: BLE001
        st.warning(f"Fecha inválida: {date}")
        return
    # Día del trade + hasta 4 hábiles previos (lookback de las BB / más contexto).
    _days, _d = [], _d0
    while len(_days) < 5:
        if _d.weekday() < 5:
            _days.append(_d.strftime("%Y-%m-%d"))
        _d = _d - pd.Timedelta(days=1)
    frames = []
    for _ds in sorted(_days):
        try:
            u = dl.underlying(ticker, _ds)
        except Exception:  # noqa: BLE001
            u = None
        if u is not None and not u.empty and "open" in u.columns:
            frames.append(u)
    if not frames:
        st.warning(f"Sin barras de subyacente para {ticker} {date} (¿ya están en cache?).")
        return
    df = pd.concat(frames, ignore_index=True)
    df = df.set_index(pd.DatetimeIndex(df["timestamp"])).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    rule = _LWC_TF.get(tf)
    if rule:
        ohlc = df.resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    else:
        ohlc = df[["open", "high", "low", "close"]].dropna()
    if ohlc.empty:
        st.warning("Sin barras para esa temporalidad.")
        return
    # Bandas de Bollinger (20, 2σ) sobre la serie continua (con lookback de días previos).
    _mid = ohlc["close"].rolling(20).mean()
    _sd = ohlc["close"].rolling(20).std(ddof=0)
    _up, _lo = _mid + 2 * _sd, _mid - 2 * _sd
    # Mostrar SOLO las barras del día del trade (las BB ya quedan pobladas por el lookback).
    _d0d = _d0.date()
    bars, mid_l, up_l, lo_l = [], [], [], []
    for ts, r in ohlc[pd.Index(ohlc.index.date) == _d0d].iterrows():
        u = _utc(ts)
        bars.append({"time": u, "open": round(float(r["open"]), 4), "high": round(float(r["high"]), 4),
                     "low": round(float(r["low"]), 4), "close": round(float(r["close"]), 4)})
        if pd.notna(_mid.loc[ts]):
            mid_l.append({"time": u, "value": round(float(_mid.loc[ts]), 4)})
            up_l.append({"time": u, "value": round(float(_up.loc[ts]), 4)})
            lo_l.append({"time": u, "value": round(float(_lo.loc[ts]), 4)})
    if not bars:
        st.warning("Sin barras del día para graficar.")
        return
    try:
        hh, mm = str(hora).split(":")[:2]
        _center = pd.Timestamp(f"{date} {int(hh):02d}:{int(mm):02d}:00", tz="UTC")
    except Exception:  # noqa: BLE001
        _center = pd.Timestamp(f"{date} 12:00:00", tz="UTC")
    _c = int(_center.timestamp())
    marker_ts = min(bars, key=lambda b: abs(b["time"] - _c))["time"]
    # Mostrar TODAS las velas del día (fitContent en el HTML); la flecha marca la hora.
    _html = f"""
    <div id="lwc_{key}" style="height:460px;width:100%"></div>
    <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <script>
      const el = document.getElementById('lwc_{key}');
      const chart = LightweightCharts.createChart(el, {{
        autoSize: true, height: 460,
        layout: {{ background: {{ color: '#0e1117' }}, textColor: '#d1d4dc' }},
        grid: {{ vertLines: {{ color: '#1e222d' }}, horzLines: {{ color: '#1e222d' }} }},
        timeScale: {{ timeVisible: true, secondsVisible: false, borderColor: '#2a2e39' }},
        rightPriceScale: {{ borderColor: '#2a2e39' }},
      }});
      const candle = chart.addCandlestickSeries({{ upColor: '#26a69a', downColor: '#ef5350',
        borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350' }});
      candle.setData({_j.dumps(bars)});
      candle.setMarkers([{{ time: {marker_ts}, position: 'aboveBar', color: '#facc15',
                            shape: 'arrowDown', text: '{hora}' }}]);
      const mid = chart.addLineSeries({{ color: '#f59e0b', lineWidth: 2 }});      // media móvil (SMA20)
      mid.setData({_j.dumps(mid_l)});
      const bbUp = chart.addLineSeries({{ color: '#3b82f6', lineWidth: 1, lineStyle: 2 }});
      bbUp.setData({_j.dumps(up_l)});
      const bbLo = chart.addLineSeries({{ color: '#3b82f6', lineWidth: 1, lineStyle: 2 }});
      bbLo.setData({_j.dumps(lo_l)});
      chart.timeScale().fitContent();
    </script>
    """
    components.html(_html, height=480)

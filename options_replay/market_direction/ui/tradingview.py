"""Widget de TradingView (Advanced Chart) para la página «Tendencia del mercado».

Mapea el ticker del universo → símbolo TradingView (EXCHANGE:TICKER) y arma el HTML embebible.

Limitación conocida del widget gratuito: NO permite saltar a una fecha/hora histórica puntual — abre
en los datos MÁS RECIENTES. Por eso la página muestra la fecha/hora elegida como REFERENCIA y ofrece
un link profundo para abrir el símbolo en TradingView. El intervalo/zona horaria sí se parametrizan.
"""
from __future__ import annotations

import json
from urllib.parse import quote

# EXCHANGE:TICKER para el universo. Se mapean EXPLÍCITO los índices/ETFs (donde el exchange importa) y
# las megacaps de más uso (set MWF, todas NASDAQ). El resto de acciones cae al ticker "pelado":
# TradingView resuelve los tickers US de forma fiable, y `allow_symbol_change` deja corregirlo en vivo.
_TV_SYMBOL = {
    # Índice
    "SPX": "SP:SPX",
    # ETFs (NYSE Arca = «AMEX» en TradingView; QQQ cotiza en NASDAQ)
    "SPY": "AMEX:SPY", "QQQ": "NASDAQ:QQQ", "IWM": "AMEX:IWM", "DIA": "AMEX:DIA",
    "GLD": "AMEX:GLD", "SLV": "AMEX:SLV", "USO": "AMEX:USO",
    "SOXL": "AMEX:SOXL", "TNA": "AMEX:TNA", "URA": "AMEX:URA",
    # Megacaps NASDAQ (set MWF — alto uso)
    "AAPL": "NASDAQ:AAPL", "MSFT": "NASDAQ:MSFT", "NVDA": "NASDAQ:NVDA", "AMZN": "NASDAQ:AMZN",
    "META": "NASDAQ:META", "GOOG": "NASDAQ:GOOG", "TSLA": "NASDAQ:TSLA", "AVGO": "NASDAQ:AVGO",
}


def tradingview_symbol(ticker: str) -> str:
    """Ticker del universo → símbolo TradingView. Cae al ticker pelado si no está mapeado."""
    tk = (ticker or "").upper().strip()
    return _TV_SYMBOL.get(tk, tk)


def build_tradingview_html(symbol: str, *, interval: str = "5", theme: str = "dark",
                           locale: str = "es", height: int = 500) -> str:
    """HTML del widget «Advanced Real-Time Chart». Zona horaria fija en America/New_York (ET) para que
    las velas intradía alineen con la hora del motor. `interval` = 1/5/15/60/D."""
    cfg = {
        "autosize": True,
        "symbol": symbol,
        "interval": interval,
        "timezone": "America/New_York",
        "theme": theme if theme in ("dark", "light") else "dark",
        "style": "1",                 # velas
        "locale": locale,
        "allow_symbol_change": True,  # buscador del widget → red de seguridad si el símbolo no resuelve
        "hide_side_toolbar": False,
        "container_id": "tv_chart_md",
    }
    return (
        f'<div class="tradingview-widget-container" style="height:{height}px">'
        f'<div id="tv_chart_md" style="height:{height}px"></div>'
        f'<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>'
        f'<script type="text/javascript">new TradingView.widget({json.dumps(cfg)});</script>'
        f'</div>'
    )


def tradingview_url(symbol: str, interval: str = "5") -> str:
    """Link profundo al chart del símbolo en tradingview.com (para navegar a la fecha a mano)."""
    return f"https://www.tradingview.com/chart/?symbol={quote(symbol)}&interval={quote(interval)}"

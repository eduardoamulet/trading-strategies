"""Runner en VIVO (paper) — Fase D: el sistema en vivo usa el MISMO `execution.run_refuerzo`
que el backtest, cableando los adapters REALES de Tradier sobre los puertos.

⚠ SEGURIDAD (no negociable): este runner es SOLO sandbox/paper. Antes de construir nada,
chequea `live_trader.settings.LIVE_TRADING_ENABLED` y se NIEGA a correr si está en True. No
mueve dinero real, no ingresa credenciales (el token de sandbox lo pone el usuario en
live_trader/secrets.py, gitignored).

Migración de entornos (la idea de toda la arquitectura), en una línea:
    backtest : run_refuerzo(PolygonBacktestData(dl), SimulatedBroker(...), BacktestClock(), ...)
    paper    : run_refuerzo(*build_tradier_ports(), LiveClock(), ...)        ← esto
La lógica de negocio (run_refuerzo) es IDÉNTICA; solo cambian los adapters inyectados.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

import pandas as pd

from .adapters.clocks import LiveClock
from .adapters.tradier_live import TradierBroker, TradierMarketData
from .execution import run_refuerzo
from .ports import Broker, MarketData
from .selection import SelectionParams, make_range_gate

_TZ = "America/New_York"


class LiveTradingBlocked(RuntimeError):
    """Se intentó correr el runner en vivo con LIVE_TRADING_ENABLED=True."""


def assert_sandbox(settings: Any) -> None:
    """Guard de seguridad: sandbox/paper únicamente. Lanza si el modo real está habilitado."""
    if getattr(settings, "LIVE_TRADING_ENABLED", False):
        raise LiveTradingBlocked(
            "BLOQUEADO: LIVE_TRADING_ENABLED=True. Este runner es SOLO sandbox/paper — "
            "no ejecuta dinero real. Poné LIVE_TRADING_ENABLED=False en live_trader/settings.py.")


def build_tradier_ports() -> Tuple[MarketData, Broker]:
    """Construye (MarketData, Broker) de Tradier SANDBOX a partir de live_trader. Lanza
    LiveTradingBlocked si el modo real está habilitado. Requiere el token de sandbox en
    live_trader/secrets.py (el usuario lo configura; acá no hay credenciales)."""
    import live_trader.settings as settings
    assert_sandbox(settings)
    from live_trader.brokers.tradier import TradierAdapter
    adapter = TradierAdapter()   # lee el token de SANDBOX desde settings/secrets.py
    return TradierMarketData(adapter), TradierBroker(adapter)


def build_alpaca_ports() -> Tuple[MarketData, Broker]:
    """Construye (MarketData, Broker) de Alpaca PAPER con alpaca-py. ⚠ `paper=True` va
    FIJO (hardcodeado): desde este builder es IMPOSIBLE operar la cuenta real de Alpaca —
    para real habría que escribir otro builder a propósito. Credenciales: ALPACA_API_KEY/
    SECRET del config.py raíz (acá no se hardcodea nada)."""
    import config
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.trading.client import TradingClient

    from .adapters.alpaca_live import AlpacaBroker, AlpacaMarketData

    key, secret = config.ALPACA_API_KEY, config.ALPACA_API_SECRET
    trading = TradingClient(key, secret, paper=True)          # paper SIEMPRE (no negociable)
    opt_data = OptionHistoricalDataClient(key, secret)
    stk_data = StockHistoricalDataClient(key, secret)
    return (AlpacaMarketData(trading, opt_data, stk_data),
            AlpacaBroker(trading, option_data=opt_data))


def run_paper_refuerzo(*, ticker: str, invest_call: float, invest_put: float,
                       umbral_pct: float, stop_pct: float,
                       params: Optional[SelectionParams] = None,
                       max_spread: float = 0.10, refuerzo_loss_pct: float = 50.0,
                       refuerzo_max: int = 0, expiry: Optional[str] = None,
                       entry_ts: Any = None, session_end: Any = None,
                       poll_sec: float = 3.0):
    """Corre la estrategia (straddle/refuerzo) en VIVO contra Tradier SANDBOX (paper),
    usando exactamente el mismo `run_refuerzo` del backtest. Entra ahora y gestiona hasta
    el cierre (16:00 ET) salvo override. Devuelve un TradeResult."""
    market, broker = build_tradier_ports()       # ← gated a sandbox adentro
    now = pd.Timestamp(entry_ts) if entry_ts is not None else pd.Timestamp.now(tz=_TZ)
    end = (pd.Timestamp(session_end) if session_end is not None
           else now.normalize() + pd.Timedelta(hours=16))
    if expiry is None:
        expiry = market.nearest_expiry(ticker, now.strftime("%Y-%m-%d")) or now.strftime("%Y-%m-%d")
    if params is None:
        params = SelectionParams(premium_min=0.0, premium_max=1e9, window_min=5)
    gate = make_range_gate(params.premium_min, params.premium_max, max_spread)
    return run_refuerzo(
        market, broker, LiveClock(poll_sec=poll_sec, tz=_TZ),
        ticker=ticker, expiry=expiry, entry_ts=now, session_end=end,
        invest_call=invest_call, invest_put=invest_put,
        umbral_pct=umbral_pct, stop_pct=stop_pct,
        params=params, gate=gate,
        refuerzo_loss_pct=refuerzo_loss_pct, refuerzo_max=refuerzo_max)

"""Factory de broker: UN punto de decisión para la capa live_trader (daemon/ui).

`make_broker()` lee settings.BROKER ("tradier" | "alpaca") — cambiar de broker es editar
UNA línea de settings.py (o pasar el nombre explícito).

IMPORTANTE: este __init__ NO importa nada a nivel módulo — trading_core importa
`live_trader.brokers.tradier` como paquete (sin live_trader/ en sys.path), y cualquier
import top-level acá (`import settings`, `from brokers...`) reventaría ese camino. Los
imports viven DENTRO de make_broker(), que solo se llama en contexto daemon/ui (donde
live_trader/ sí está en sys.path).
"""
from __future__ import annotations


def make_broker(name: str | None = None):
    """Instancia el BrokerAdapter según settings.BROKER (default 'tradier')."""
    import settings
    from brokers.base import BrokerError
    _n = (name or getattr(settings, "BROKER", "tradier")).strip().lower()
    if _n == "tradier":
        from brokers.tradier import TradierAdapter
        return TradierAdapter()
    if _n == "alpaca":
        from brokers.alpaca import AlpacaAdapter
        return AlpacaAdapter()
    raise BrokerError(f"Broker desconocido: {_n!r} (opciones: tradier, alpaca)")

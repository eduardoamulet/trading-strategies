"""Adapters concretos de los puertos. El dominio NO importa de acá; el caller (la app)
inyecta el adapter que corresponda al entorno (backtest / paper / vivo)."""
from .clocks import BacktestClock
from .polygon_backtest import PolygonBacktestData
from .simulated_broker import SimulatedBroker

__all__ = ["BacktestClock", "PolygonBacktestData", "SimulatedBroker"]

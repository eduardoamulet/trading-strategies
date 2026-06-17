"""trading_core — lógica de negocio de trading desacoplada del proveedor de datos.

Arquitectura de PUERTOS Y ADAPTADORES (hexagonal):
  - domain.py     : value objects puros (Contract, Quote, Order, Fill, Leg, TradeResult).
  - ports.py      : interfaces MarketData / Broker / Clock (lo que el dominio necesita).
  - selection.py  : selección de contrato (Opción 1 + ventana de búsqueda).
  - execution.py  : ciclo de la operación (abrir/gestionar/cerrar) — decide con strategy_core.
  - adapters/     : implementaciones concretas de los puertos (Polygon backtest, broker
                    simulado, clocks; en el futuro Tradier/Schwab para vivo).

La MISMA lógica (selection + execution) corre en backtest, paper y vivo: solo cambian los
adapters inyectados. Ver README.md."""
from .domain import (Contract, Fill, Leg, NoContractError, OrderRequest, OrderSide,
                     Quote, Right, TradeResult)
from .execution import run_straddle
from .ports import Broker, Clock, MarketData
from .selection import (Gate, SelectionParams, make_range_gate, select_single,
                        select_straddle)

__all__ = [
    "Contract", "Quote", "Right", "OrderSide", "OrderRequest", "Fill", "Leg",
    "TradeResult", "NoContractError",
    "MarketData", "Broker", "Clock",
    "SelectionParams", "Gate", "make_range_gate", "select_straddle", "select_single",
    "run_straddle",
]

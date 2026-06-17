"""SimulatedBroker — adapter del puerto Broker para backtest y paper trading.

Llena contra el NBBO del puerto MarketData: COMPRA al ask, VENDE al bid (el modelo de
fills realista "Fase 2"). Sin red, sin estado de cuenta real → determinista y testeable.

Para vivo, en su lugar se inyecta TradierBroker/SchwabBroker (mismo puerto Broker); la
lógica de negocio (execution.run_straddle) no cambia."""
from __future__ import annotations

from typing import Any

from ..domain import Fill, OrderRequest, OrderSide
from ..ports import Broker, MarketData


class SimulatedBroker(Broker):
    """Ejecución simulada contra un MarketData. `slippage` (por acción) opcional empeora
    el fill (suma al ask en compras, resta al bid en ventas) para modelar profundidad."""

    def __init__(self, market: MarketData, slippage: float = 0.0):
        self._market = market
        self._slippage = float(slippage or 0.0)

    def execute(self, order: OrderRequest, at: Any) -> Fill:
        q = self._market.quote(order.occ, at)
        if order.side == OrderSide.BUY:
            base = q.ask if (q is not None and q.ask is not None) else order.limit
            price = (base + self._slippage) if base is not None else 0.0
        else:
            base = q.bid if (q is not None and q.bid is not None) else order.limit
            price = max(0.0, (base - self._slippage)) if base is not None else 0.0
        return Fill(occ=order.occ, side=order.side, qty=order.qty,
                    price=float(price or 0.0), ts=at)

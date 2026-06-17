"""Adapters de Tradier (paper/vivo) para los puertos `MarketData` y `Broker`.

Reusan un broker de Tradier ya existente (live_trader/brokers/tradier.py) que se INYECTA
(duck typing: get_underlying_price, get_option_chain, get_quote, nearest_expiry, place_order,
get_order). Traducen los modelos del broker ↔ los del dominio (trading_core.domain). Así la
MISMA lógica (selection/execution/run_refuerzo) corre contra Tradier sandbox sin tocar el
dominio: solo se inyectan estos adapters en vez de PolygonBacktestData/SimulatedBroker.

⚠ Seguridad: estos adapters NO eligen el entorno. El broker inyectado decide sandbox vs live
(settings.LIVE_TRADING_ENABLED, que debe quedar en False). No hay credenciales acá.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, List, Optional

from ..domain import Contract, Fill, OrderRequest, OrderSide, Quote, Right
from ..ports import Broker, MarketData


def _right_of(v: Any) -> Right:
    s = str(getattr(v, "value", v) or "").upper()
    return Right.CALL if s in ("C", "CALL") else Right.PUT


class TradierMarketData(MarketData):
    """Datos de mercado en VIVO vía un broker de Tradier inyectado. `at` se ignora para
    traer el dato (siempre devuelve lo último real) y se usa solo como timestamp del quote."""

    def __init__(self, broker_adapter: Any):
        self._b = broker_adapter

    def underlying_price(self, ticker: str, at: Any) -> Optional[float]:
        try:
            return float(self._b.get_underlying_price(ticker))
        except Exception:
            return None

    def chain(self, ticker: str, expiry: str, at: Any) -> List[Contract]:
        out: List[Contract] = []
        try:
            for c in self._b.get_option_chain(ticker, expiry):
                out.append(Contract(
                    occ=str(getattr(c, "occ", "")),
                    underlying=ticker,
                    expiry=str(getattr(c, "expiry", expiry)),
                    strike=float(getattr(c, "strike", 0.0) or 0.0),
                    right=_right_of(getattr(c, "right", "C")),
                    open_interest=int(getattr(c, "open_interest", 0) or 0),
                    volume=int(getattr(c, "volume", 0) or 0)))
        except Exception:
            pass
        return out

    def quote(self, occ: str, at: Any) -> Quote:
        try:
            q = self._b.get_quote(occ)
            return Quote(bid=getattr(q, "bid", None), ask=getattr(q, "ask", None), ts=at,
                         bid_size=getattr(q, "bid_size", None), ask_size=getattr(q, "ask_size", None))
        except Exception:
            return Quote(bid=None, ask=None, ts=at)

    def nearest_expiry(self, ticker: str, on_or_after: str) -> Optional[str]:
        try:
            return self._b.nearest_expiry(ticker)
        except Exception:
            return None


class TradierBroker(Broker):
    """Ejecución en VIVO vía un broker de Tradier inyectado. Traduce la orden del dominio a
    una orden LIMIT (compra → marketable al ask, venta → al bid si no se da limit), la envía
    y POLLEA hasta el fill (o timeout). Devuelve el Fill del dominio."""

    _SIDE = {OrderSide.BUY: "buy_to_open", OrderSide.SELL: "sell_to_close"}

    def __init__(self, broker_adapter: Any, max_wait_sec: float = 10.0, poll_sec: float = 0.5):
        self._b = broker_adapter
        self._max_wait = float(max_wait_sec)
        self._poll = float(poll_sec)

    @staticmethod
    def _underlying_of(occ: str) -> str:
        """Root del subyacente desde el OCC (O:QQQ260609C00728000 → QQQ)."""
        s = occ[2:] if occ.startswith("O:") else occ
        root = ""
        for ch in s:
            if ch.isalpha():
                root += ch
            else:
                break
        return root

    def execute(self, order: OrderRequest, at: Any) -> Fill:
        q = self._b.get_quote(order.occ)
        if order.side == OrderSide.BUY:
            limit = order.limit if order.limit is not None else getattr(q, "ask", None)
        else:
            limit = order.limit if order.limit is not None else getattr(q, "bid", None)
        live_order = SimpleNamespace(
            occ=order.occ, underlying=self._underlying_of(order.occ),
            side=self._SIDE[order.side], qty=int(order.qty), type="limit",
            limit_price=float(limit or 0.0), duration="day",
            tag=f"tc-{getattr(order, 'ts', '')}")
        res = self._b.place_order(live_order)
        order_id = getattr(res, "order_id", None)
        return self._await_fill(order, order_id, at, fallback_price=float(limit or 0.0))

    def _await_fill(self, order: OrderRequest, order_id: Optional[str], at: Any,
                    fallback_price: float) -> Fill:
        deadline = self._max_wait
        waited = 0.0
        last_price, last_qty = fallback_price, order.qty
        while order_id is not None and waited <= deadline:
            f = self._b.get_order(order_id)
            status = str(getattr(f, "status", "")).lower()
            last_price = float(getattr(f, "avg_price", 0.0) or last_price)
            last_qty = int(getattr(f, "filled_qty", 0) or last_qty)
            if status in ("filled", "ok"):
                break
            if status in ("rejected", "canceled", "cancelled", "expired"):
                last_qty = 0
                break
            time.sleep(self._poll)
            waited += self._poll
        return Fill(occ=order.occ, side=order.side, qty=last_qty, price=last_price, ts=at)

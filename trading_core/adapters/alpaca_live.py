"""Adapters de Alpaca (paper) para los puertos `MarketData` y `Broker`.

Mismo rol que tradier_live.py pero contra el SDK oficial alpaca-py: se INYECTAN los
clientes ya construidos (TradingClient + OptionHistoricalDataClient + StockHistoricalDataClient)
y estos adapters traducen sus modelos ↔ los del dominio (trading_core.domain). Así la MISMA
lógica (selection/execution/run_refuerzo) corre contra Alpaca paper sin tocar el dominio.

⚠ Seguridad: estos adapters NO eligen el entorno. El TradingClient inyectado decide
paper vs live — el builder oficial (live_runner.build_alpaca_ports) lo construye con
paper=True FIJO. No hay credenciales acá.

Notas de traducción:
  · OCC: el dominio puede traer el prefijo de Polygon («O:QQQ…»); Alpaca usa el OCC plano
    → se normaliza con _plain_occ() en cada llamada.
  · El SDK devuelve numéricos como strings y enums (status/type) → se toleran ambos.
  · `at` se ignora para traer datos (siempre lo último real) y se usa como ts del quote.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional

from ..domain import Contract, Fill, OrderRequest, OrderSide, Quote, Right
from ..ports import Broker, MarketData


def _plain_occ(occ: str) -> str:
    """«O:QQQ260611C00700000» (estilo Polygon) → «QQQ260611C00700000» (estilo Alpaca)."""
    s = str(occ or "")
    return s[2:] if s.startswith("O:") else s


def _f(v) -> Optional[float]:
    """Numérico del SDK (float/str/None) → float | None."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _enum_str(v) -> str:
    """Enum del SDK o string → string en minúsculas ('call', 'filled', …)."""
    return str(getattr(v, "value", v) or "").lower()


class AlpacaMarketData(MarketData):
    """Datos de mercado en VIVO vía los clientes de alpaca-py inyectados."""

    def __init__(self, trading_client: Any, option_data: Any, stock_data: Any):
        self._trade = trading_client       # contratos (chain / vencimientos)
        self._opt = option_data            # NBBO de opciones
        self._stk = stock_data             # último precio del subyacente

    def underlying_price(self, ticker: str, at: Any) -> Optional[float]:
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            res = self._stk.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
            return _f(getattr(res.get(ticker), "price", None))
        except Exception:
            return None

    def chain(self, ticker: str, expiry: str, at: Any) -> List[Contract]:
        out: List[Contract] = []
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            token = None
            for _page in range(20):                      # paginado (limit 10k por página)
                req = GetOptionContractsRequest(underlying_symbols=[ticker],
                                                expiration_date=expiry, page_token=token)
                res = self._trade.get_option_contracts(req)
                for c in getattr(res, "option_contracts", None) or []:
                    right = Right.CALL if _enum_str(getattr(c, "type", "call")) == "call" else Right.PUT
                    out.append(Contract(
                        occ=str(getattr(c, "symbol", "")),
                        underlying=ticker,
                        expiry=str(getattr(c, "expiration_date", expiry)),
                        strike=_f(getattr(c, "strike_price", 0.0)) or 0.0,
                        right=right,
                        open_interest=int(_f(getattr(c, "open_interest", 0)) or 0),
                        volume=0))                        # Alpaca no trae volumen en el contrato
                token = getattr(res, "next_page_token", None)
                if not token:
                    break
        except Exception:
            pass
        return out

    def quote(self, occ: str, at: Any) -> Quote:
        try:
            from alpaca.data.requests import OptionLatestQuoteRequest
            sym = _plain_occ(occ)
            res = self._opt.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=sym))
            q = res.get(sym)
            return Quote(bid=_f(getattr(q, "bid_price", None)), ask=_f(getattr(q, "ask_price", None)),
                         ts=at, bid_size=_f(getattr(q, "bid_size", None)),
                         ask_size=_f(getattr(q, "ask_size", None)))
        except Exception:
            return Quote(bid=None, ask=None, ts=at)

    def nearest_expiry(self, ticker: str, on_or_after: str) -> Optional[str]:
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            req = GetOptionContractsRequest(underlying_symbols=[ticker],
                                            expiration_date_gte=on_or_after, limit=100)
            res = self._trade.get_option_contracts(req)
            exps = sorted({str(getattr(c, "expiration_date", ""))
                           for c in getattr(res, "option_contracts", None) or []} - {""})
            return exps[0] if exps else None
        except Exception:
            return None


class AlpacaBroker(Broker):
    """Ejecución en PAPER vía el TradingClient de alpaca-py inyectado. Compra → orden LIMIT
    marketable al ask (venta → al bid) si no viene limit — mismo modelo Fase 2 que el resto
    del stack; sin quote disponible cae a orden MARKET. Pollea hasta el fill (o timeout)."""

    def __init__(self, trading_client: Any, option_data: Any = None,
                 max_wait_sec: float = 10.0, poll_sec: float = 0.5):
        self._trade = trading_client
        self._opt = option_data            # para el limit marketable (opcional)
        self._max_wait = float(max_wait_sec)
        self._poll = float(poll_sec)

    def _marketable_limit(self, occ: str, side: OrderSide) -> Optional[float]:
        if self._opt is None:
            return None
        try:
            from alpaca.data.requests import OptionLatestQuoteRequest
            sym = _plain_occ(occ)
            q = self._opt.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=sym)).get(sym)
            return _f(getattr(q, "ask_price" if side == OrderSide.BUY else "bid_price", None))
        except Exception:
            return None

    def execute(self, order: OrderRequest, at: Any) -> Fill:
        from alpaca.trading.enums import OrderSide as AlpSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        sym = _plain_occ(order.occ)
        side = AlpSide.BUY if order.side == OrderSide.BUY else AlpSide.SELL
        limit = order.limit if order.limit is not None else self._marketable_limit(order.occ, order.side)
        if limit is not None:
            req = LimitOrderRequest(symbol=sym, qty=int(order.qty), side=side,
                                    time_in_force=TimeInForce.DAY, limit_price=round(float(limit), 2))
        else:
            req = MarketOrderRequest(symbol=sym, qty=int(order.qty), side=side,
                                     time_in_force=TimeInForce.DAY)
        res = self._trade.submit_order(req)
        order_id = getattr(res, "id", None)
        return self._await_fill(order, order_id, at, fallback_price=float(limit or 0.0))

    def _await_fill(self, order: OrderRequest, order_id, at: Any, fallback_price: float) -> Fill:
        waited = 0.0
        last_price, last_qty = fallback_price, order.qty
        while order_id is not None and waited <= self._max_wait:
            o = self._trade.get_order_by_id(order_id)
            status = _enum_str(getattr(o, "status", ""))
            last_price = _f(getattr(o, "filled_avg_price", None)) or last_price
            last_qty = int(_f(getattr(o, "filled_qty", None)) or last_qty)
            if status == "filled":
                break
            if status in ("rejected", "canceled", "cancelled", "expired"):
                last_qty = 0
                break
            time.sleep(self._poll)
            waited += self._poll
        return Fill(occ=order.occ, side=order.side, qty=last_qty, price=last_price, ts=at)

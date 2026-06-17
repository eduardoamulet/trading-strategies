"""Ejecución y gestión de posición — el ciclo de vida de una operación, por los puertos.

Una sola lógica para backtest/paper/vivo: cambian los adapters inyectados, no el código.
  - run_refuerzo : straddle con MARTINGALA por pierna (refuerzo_max=0 → straddle simple).
  - run_straddle : alias de run_refuerzo(refuerzo_max=0).
  - run_single   : una pierna (Sólo CALL / Sólo PUT).

Decide con strategy_core.exit_decision (umbral/stop sobre el ROI TOTAL). El SimulatedBroker
compra al ask y vende al bid (Fase 2); en vivo, el broker real — mismo puerto.

PENDIENTE (mismas piezas): variantes 'plus' (salida forzada a una hora), Opción 2/3 de
selección. La estructura (Position multi-tranche, gate inyectable) ya lo soporta.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import strategy_core

from .domain import (Contract, Leg, NoContractError, OrderRequest, OrderSide,
                     Position, Right, TradeResult)
from .ports import Broker, Clock, MarketData
from .selection import Gate, SelectionParams, select_single, select_straddle


def _qty_for(invest: float, premium: float) -> int:
    """Contratos enteros que entran en `invest` a `premium` por acción (mín 1)."""
    cost1 = (premium or 0.0) * 100.0
    return max(1, int(invest // cost1)) if cost1 > 0 else 0


def _buy(broker: Broker, contract: Contract, qty: int, at: Any) -> Leg:
    f = broker.execute(OrderRequest(contract.occ, OrderSide.BUY, qty, ts=at), at)
    return Leg(contract=contract, qty=qty, entry_price=f.price, entry_ts=at)


def _bid_mark(market: MarketData, contract: Contract, at: Any, fallback: float) -> float:
    q = market.quote(contract.occ, at)
    return q.bid if (q is not None and q.bid is not None) else fallback


def _close_all(broker: Broker, pos: Position, at: Any) -> float:
    """Vende TODAS las tranches de cada pierna al bid. Devuelve proceeds totales ($)."""
    proceeds = 0.0
    for right in pos.rights():
        c = pos.contract_of(right)
        qty = pos.qty_of(right)
        f = broker.execute(OrderRequest(c.occ, OrderSide.SELL, qty, ts=at), at)
        proceeds += qty * f.price * 100.0
    return proceeds


def run_refuerzo(market: MarketData, broker: Broker, clock: Clock, *,
                 ticker: str, expiry: str, entry_ts: Any, session_end: Any,
                 invest_call: float, invest_put: float,
                 umbral_pct: float, stop_pct: float,
                 params: SelectionParams, gate: Gate,
                 refuerzo_loss_pct: float = 50.0, refuerzo_max: int = 0) -> TradeResult:
    """Straddle CALL+PUT con martingala por pierna. `refuerzo_max=0` = straddle simple.

    Loop por tick: marca al BID → si ROI TOTAL >= umbral (take_profit) o <= stop (stop_loss),
    sale. Si no: refuerza la pierna que MÁS pierde entre las que tienen ROI PROPIO <=
    -refuerzo_loss_pct (compra otra tranche del MISMO contrato al ask, re-invirtiendo el
    capital original de esa pierna), con tope TOTAL `refuerzo_max`."""
    call_c, put_c, entry = select_straddle(market, clock, ticker, expiry, entry_ts, params, gate)
    if call_c is None or put_c is None:
        raise NoContractError(f"{ticker}: sin contrato CALL+PUT en la ventana de búsqueda")

    contracts: Dict[Right, Contract] = {Right.CALL: call_c, Right.PUT: put_c}
    invest: Dict[Right, float] = {Right.CALL: float(invest_call), Right.PUT: float(invest_put)}
    pos = Position()
    pos.add(_buy(broker, call_c, _qty_for(invest_call, call_c.quote.ask), entry))
    pos.add(_buy(broker, put_c, _qty_for(invest_put, put_c.quote.ask), entry))
    entry_price = {Right.CALL: pos.legs[0].entry_price, Right.PUT: pos.legs[1].entry_price}

    exit_ts: Any = session_end
    reason = "session_end"
    marks: List[float] = []
    for t in clock.ticks(entry, session_end):
        m = {r: _bid_mark(market, contracts[r], t, entry_price[r]) for r in (Right.CALL, Right.PUT)}
        cost = pos.cost()
        value = sum(pos.value_of(r, m[r]) for r in (Right.CALL, Right.PUT))
        roi = ((value - cost) / cost * 100.0) if cost else 0.0
        marks.append(roi)

        decision = strategy_core.exit_decision(roi, umbral_pct, stop_pct)   # umbral/stop sobre TOTAL
        if decision is not None:
            exit_ts, reason = t, decision
            break

        # REFUERZO: pierna que MÁS pierde entre las elegibles (ROI propio <= -loss, mark > penny).
        if len(pos.reinforcements) < int(refuerzo_max):
            cands = []
            for r in (Right.CALL, Right.PUT):
                cr = pos.cost_of(r)
                if cr <= 0 or m[r] <= 0.01:
                    continue
                roi_r = (pos.value_of(r, m[r]) - cr) / cr * 100.0
                if roi_r <= -float(refuerzo_loss_pct):
                    cands.append((r, roi_r))
            if cands:
                r = min(cands, key=lambda x: x[1])[0]              # la que más pierde
                ask = market.quote(contracts[r].occ, t).ask
                if ask and ask > 0:
                    leg = _buy(broker, contracts[r], _qty_for(invest[r], ask), t)  # re-invierte al ask
                    pos.add(leg)
                    pos.reinforcements.append({"ts": t, "right": r.value,
                                               "price": leg.entry_price, "qty": leg.qty})
        exit_ts = t

    proceeds = _close_all(broker, pos, exit_ts)
    return TradeResult(underlying=ticker, entry_ts=entry, exit_ts=exit_ts, legs=pos.legs,
                       entry_cost=pos.cost(), exit_proceeds=proceeds, exit_reason=reason,
                       marks=marks, reinforcements=pos.reinforcements)


def run_straddle(market: MarketData, broker: Broker, clock: Clock, *,
                 ticker: str, expiry: str, entry_ts: Any, session_end: Any,
                 invest_call: float, invest_put: float,
                 umbral_pct: float, stop_pct: float,
                 params: SelectionParams, gate: Gate) -> TradeResult:
    """Straddle simple (sin refuerzo) = run_refuerzo con refuerzo_max=0."""
    return run_refuerzo(market, broker, clock, ticker=ticker, expiry=expiry, entry_ts=entry_ts,
                        session_end=session_end, invest_call=invest_call, invest_put=invest_put,
                        umbral_pct=umbral_pct, stop_pct=stop_pct, params=params, gate=gate,
                        refuerzo_loss_pct=100.0, refuerzo_max=0)


def run_single(market: MarketData, broker: Broker, clock: Clock, *,
               ticker: str, expiry: str, right: Right, entry_ts: Any, session_end: Any,
               invest: float, umbral_pct: float, stop_pct: float,
               params: SelectionParams, gate: Gate) -> TradeResult:
    """Una sola pierna (Sólo CALL / Sólo PUT) con ventana de búsqueda. Sale por su ROI vs
    umbral/stop o al cierre."""
    leg_c, entry = select_single(market, clock, ticker, expiry, right, entry_ts, params, gate)
    if leg_c is None:
        raise NoContractError(f"{ticker}: sin contrato {right.value} en la ventana de búsqueda")
    pos = Position()
    pos.add(_buy(broker, leg_c, _qty_for(invest, leg_c.quote.ask), entry))
    entry_price = pos.legs[0].entry_price

    exit_ts: Any = session_end
    reason = "session_end"
    marks: List[float] = []
    for t in clock.ticks(entry, session_end):
        mark = _bid_mark(market, leg_c, t, entry_price)
        cost = pos.cost()
        roi = ((pos.value_of(right, mark) - cost) / cost * 100.0) if cost else 0.0
        marks.append(roi)
        decision = strategy_core.exit_decision(roi, umbral_pct, stop_pct)
        if decision is not None:
            exit_ts, reason = t, decision
            break
        exit_ts = t

    proceeds = _close_all(broker, pos, exit_ts)
    return TradeResult(underlying=ticker, entry_ts=entry, exit_ts=exit_ts, legs=pos.legs,
                       entry_cost=pos.cost(), exit_proceeds=proceeds, exit_reason=reason,
                       marks=marks)

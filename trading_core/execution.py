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

import pandas as pd

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
        proceeds += _close_right(broker, pos, right, at)
    return proceeds


def _close_right(broker: Broker, pos: Position, right: Right, at: Any) -> float:
    """Vende TODA una pierna (todas sus tranches) al bid. Devuelve sus proceeds ($)."""
    c = pos.contract_of(right)
    qty = pos.qty_of(right)
    if c is None or qty <= 0:
        return 0.0
    f = broker.execute(OrderRequest(c.occ, OrderSide.SELL, qty, ts=at), at)
    return qty * f.price * 100.0


def _leg_roi_pct(pos: Position, right: Right, mark: float) -> float:
    """ROI(%) de UNA pierna a la prima `mark`."""
    cost = pos.cost_of(right)
    return ((pos.value_of(right, mark) - cost) / cost * 100.0) if cost else 0.0


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


# ───────────────────────── variantes 'plus' / 'CALL o PUT' ─────────────────────────

def _open_straddle(market, broker, clock, ticker, expiry, entry_ts, invest_call, invest_put,
                   params, gate):
    """Selecciona (con ventana) y ABRE CALL+PUT. Helper compartido por las variantes.
    Devuelve (pos, contracts, entry_ts, entry_price)."""
    call_c, put_c, entry = select_straddle(market, clock, ticker, expiry, entry_ts, params, gate)
    if call_c is None or put_c is None:
        raise NoContractError(f"{ticker}: sin contrato CALL+PUT en la ventana de búsqueda")
    pos = Position()
    pos.add(_buy(broker, call_c, _qty_for(invest_call, call_c.quote.ask), entry))
    pos.add(_buy(broker, put_c, _qty_for(invest_put, put_c.quote.ask), entry))
    contracts = {Right.CALL: call_c, Right.PUT: put_c}
    entry_price = {Right.CALL: pos.legs[0].entry_price, Right.PUT: pos.legs[1].entry_price}
    return pos, contracts, entry, entry_price


def run_both_plus(market: MarketData, broker: Broker, clock: Clock, *,
                  ticker: str, expiry: str, entry_ts: Any, exit_time: Any, session_end: Any,
                  invest_call: float, invest_put: float, params: SelectionParams,
                  gate: Gate) -> TradeResult:
    """CALL y PUT (plus): compra ambas y las vende a la HORA DE SALIDA forzada (`exit_time`),
    sin umbral ni stop. `exit_time` se capa a `session_end`."""
    pos, contracts, entry, entry_price = _open_straddle(
        market, broker, clock, ticker, expiry, entry_ts, invest_call, invest_put, params, gate)
    target = min(pd.Timestamp(exit_time), pd.Timestamp(session_end))
    if target < pd.Timestamp(entry):
        target = pd.Timestamp(entry)
    exit_ts, marks = entry, []
    cost = pos.cost()
    for t in clock.ticks(entry, target):
        value = sum(pos.value_of(r, _bid_mark(market, contracts[r], t, entry_price[r]))
                    for r in (Right.CALL, Right.PUT))
        marks.append(((value - cost) / cost * 100.0) if cost else 0.0)
        exit_ts = t
    proceeds = _close_all(broker, pos, exit_ts)
    return TradeResult(underlying=ticker, entry_ts=entry, exit_ts=exit_ts, legs=pos.legs,
                       entry_cost=cost, exit_proceeds=proceeds, exit_reason="forced_exit",
                       marks=marks)


def run_call_or_put(market: MarketData, broker: Broker, clock: Clock, *,
                    ticker: str, expiry: str, entry_ts: Any, session_end: Any,
                    invest_call: float, invest_put: float, params: SelectionParams, gate: Gate,
                    target_pct: float = 100.0) -> TradeResult:
    """CALL o PUT: compra ambas y vende LAS DOS en cuanto CUALQUIER pierna alcanza
    `target_pct` (+100% = la prima se duplica). Si ninguna llega, cierra al EOD."""
    pos, contracts, entry, entry_price = _open_straddle(
        market, broker, clock, ticker, expiry, entry_ts, invest_call, invest_put, params, gate)
    cost = pos.cost()
    exit_ts, reason, marks = session_end, "session_end", []
    for t in clock.ticks(entry, session_end):
        mk = {r: _bid_mark(market, contracts[r], t, entry_price[r]) for r in (Right.CALL, Right.PUT)}
        value = sum(pos.value_of(r, mk[r]) for r in (Right.CALL, Right.PUT))
        marks.append(((value - cost) / cost * 100.0) if cost else 0.0)
        if (_leg_roi_pct(pos, Right.CALL, mk[Right.CALL]) >= target_pct or
                _leg_roi_pct(pos, Right.PUT, mk[Right.PUT]) >= target_pct):
            exit_ts, reason = t, "take_profit"
            break
        exit_ts = t
    proceeds = _close_all(broker, pos, exit_ts)
    return TradeResult(underlying=ticker, entry_ts=entry, exit_ts=exit_ts, legs=pos.legs,
                       entry_cost=cost, exit_proceeds=proceeds, exit_reason=reason, marks=marks)


def run_call_or_put_plus(market: MarketData, broker: Broker, clock: Clock, *,
                         ticker: str, expiry: str, entry_ts: Any, session_end: Any,
                         invest_call: float, invest_put: float, params: SelectionParams,
                         gate: Gate, target_pct: float = 50.0) -> TradeResult:
    """CALL o PUT (plus): la PRIMERA pierna que alcanza `target_pct` se vende y BANCA su
    valor; la otra se vende cuando (bancado + su valor) recupera la INVERSIÓN TOTAL inicial.
    Si la primera nunca llega o la segunda nunca recupera, cierran al EOD."""
    pos, contracts, entry, entry_price = _open_straddle(
        market, broker, clock, ticker, expiry, entry_ts, invest_call, invest_put, params, gate)
    total_invest = pos.cost()
    banked, sold, a_right = 0.0, set(), None
    exit_ts, reason, marks = session_end, "session_end", []
    for t in clock.ticks(entry, session_end):
        mk = {r: _bid_mark(market, contracts[r], t, entry_price[r]) for r in (Right.CALL, Right.PUT)}
        open_val = sum(pos.value_of(r, mk[r]) for r in (Right.CALL, Right.PUT) if r not in sold)
        marks.append(((banked + open_val - total_invest) / total_invest * 100.0) if total_invest else 0.0)
        if a_right is None:
            rc = _leg_roi_pct(pos, Right.CALL, mk[Right.CALL])
            rp = _leg_roi_pct(pos, Right.PUT, mk[Right.PUT])
            hit = (Right.CALL if (rc >= target_pct and (rp < target_pct or rc >= rp))
                   else Right.PUT if rp >= target_pct else None)
            if hit is not None:
                banked += _close_right(broker, pos, hit, t)
                sold.add(hit)
                a_right = hit
        else:
            b = Right.PUT if a_right == Right.CALL else Right.CALL
            if b not in sold and (banked + pos.value_of(b, mk[b])) >= total_invest:
                banked += _close_right(broker, pos, b, t)
                sold.add(b)
                exit_ts, reason = t, "recovered"
                break
        exit_ts = t
    for r in (Right.CALL, Right.PUT):       # cerrar lo que quede al EOD
        if r not in sold:
            banked += _close_right(broker, pos, r, exit_ts)
            sold.add(r)
    return TradeResult(underlying=ticker, entry_ts=entry, exit_ts=exit_ts, legs=pos.legs,
                       entry_cost=total_invest, exit_proceeds=banked, exit_reason=reason,
                       marks=marks)

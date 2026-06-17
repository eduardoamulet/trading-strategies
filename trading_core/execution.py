"""Ejecución y gestión de posición — el ciclo de vida de una operación, por los puertos.

run_straddle() = selecciona (con ventana) → ABRE (broker.execute, compra) → GESTIONA
(marca al bid por tick, decide salida con strategy_core.exit_decision) → CIERRA (vende).

Es la MISMA lógica de negocio para backtest, paper y vivo: solo cambian los adapters
inyectados (SimulatedBroker+PolygonBacktestData vs TradierBroker+TradierMarketData). No
hay un solo `if backtest:` acá — esa es la idea (la 'D' y la 'S' de SOLID).

PENDIENTE (migración por fases, misma capa de puertos): martingala/refuerzo (otro tranche
al mismo occ cuando la pierna cae bajo un umbral), variantes 'plus', salida por pierna.
La estructura (Leg.qty mutable, marca por tick) ya lo soporta sin re-arquitecturar.
"""
from __future__ import annotations

from typing import Any, List, Optional

import pandas as pd

import strategy_core

from .domain import (Fill, Leg, NoContractError, OrderRequest, OrderSide,
                     TradeResult)
from .ports import Broker, Clock, MarketData
from .selection import Gate, SelectionParams, select_straddle


def _qty_for(invest: float, premium: float) -> int:
    """Cantidad de contratos enteros que entran en `invest` a `premium` por acción."""
    cost1 = premium * 100.0
    return max(1, int(invest // cost1)) if cost1 > 0 else 0


def run_straddle(market: MarketData, broker: Broker, clock: Clock, *,
                 ticker: str, expiry: str, entry_ts: Any, session_end: Any,
                 invest_call: float, invest_put: float,
                 umbral_pct: float, stop_pct: float,
                 params: SelectionParams, gate: Gate) -> TradeResult:
    """Corre un straddle (CALL+PUT) de punta a punta. Lanza NoContractError si no se
    encuentra contrato dentro de la ventana de búsqueda (→ "Sin resultado")."""
    call_c, put_c, entry = select_straddle(market, clock, ticker, expiry, entry_ts, params, gate)
    if call_c is None or put_c is None:
        raise NoContractError(f"{ticker}: sin contrato CALL+PUT en la ventana de búsqueda")

    # ABRIR: comprar ambas piernas. El broker decide el precio efectivo (sim: ask).
    qc = _qty_for(invest_call, call_c.quote.ask)
    qp = _qty_for(invest_put, put_c.quote.ask)
    fc: Fill = broker.execute(OrderRequest(call_c.occ, OrderSide.BUY, qc, ts=entry), entry)
    fp: Fill = broker.execute(OrderRequest(put_c.occ, OrderSide.BUY, qp, ts=entry), entry)
    legs: List[Leg] = [Leg(call_c, qc, fc.price, entry), Leg(put_c, qp, fp.price, entry)]
    entry_cost = sum(l.cost() for l in legs)

    # GESTIONAR: marcar al BID por tick; salir por umbral/stop (strategy_core) o cierre.
    exit_ts: Any = session_end
    reason = "session_end"
    marks: List[float] = []
    for t in clock.ticks(entry, session_end):
        value = 0.0
        for l in legs:
            q = market.quote(l.contract.occ, t)
            mark = q.bid if (q is not None and q.bid is not None) else l.entry_price
            value += l.value_at(mark)
        roi_pct = ((value - entry_cost) / entry_cost * 100.0) if entry_cost else 0.0
        marks.append(roi_pct)
        decision = strategy_core.exit_decision(roi_pct, umbral_pct, stop_pct)
        if decision is not None:
            exit_ts, reason = t, decision
            break
        exit_ts = t   # última marca vista = cierre si no dispara

    # CERRAR: vender ambas piernas. El broker decide el precio (sim: bid).
    proceeds = 0.0
    for l in legs:
        f = broker.execute(OrderRequest(l.contract.occ, OrderSide.SELL, l.qty, ts=exit_ts), exit_ts)
        proceeds += l.qty * f.price * 100.0

    return TradeResult(underlying=ticker, entry_ts=entry, exit_ts=exit_ts, legs=legs,
                       entry_cost=entry_cost, exit_proceeds=proceeds, exit_reason=reason,
                       marks=marks)

"""Selección de contrato — "Opción 1" (menor spread en rango) con VENTANA DE BÚSQUEDA.

Lógica de negocio reusable: pide datos por el puerto `MarketData` y decide con reglas
puras (`strategy_core`). NO conoce el proveedor → corre igual en backtest y en vivo.

La compuerta de spread se INYECTA (`gate`): el caller decide la política (rango por ASK,
máximo plano, etc.) sin que el selector dependa de una config concreta. Eso lo hace
abierto a extensión y cerrado a modificación (la 'O' de SOLID) y testeable con un gate fake.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

import pandas as pd

import strategy_core  # reglas puras compartidas (repo root)

from .domain import Contract, Right
from .ports import Clock, MarketData

# Una compuerta = función pura (contrato cotizado) -> ¿pasa?  Inyectable.
Gate = Callable[[Contract], bool]


@dataclass(frozen=True)
class SelectionParams:
    """Parámetros de selección. `premium_min/max` = rango de prima por acción (ASK).
    `window_min` = ventana de búsqueda en minutos (0 = un solo intento). `max_strikes` =
    cuántos strikes cercanos a ATM probar. `mode` = 'itm' (1-ITM, Opción 1) o 'atm'."""
    premium_min: float
    premium_max: float
    window_min: float = 0.0
    max_strikes: int = 25
    mode: str = "itm"


def make_range_gate(premium_min: float, premium_max: float,
                    max_spread: float) -> Gate:
    """Compuerta por defecto: ASK dentro del rango de prima y spread <= max_spread."""
    def _gate(c: Contract) -> bool:
        q = c.quote
        if q is None or q.ask is None:
            return False
        if not (premium_min <= q.ask <= premium_max):
            return False
        return q.spread is not None and q.spread <= max_spread
    return _gate


def _best_leg_at(market: MarketData, ticker: str, expiry: str, right: Right,
                 at: Any, params: SelectionParams, gate: Gate) -> Optional[Contract]:
    """Mejor contrato del lado `right` AS-OF `at`: entre los `max_strikes` más cercanos a
    ATM que pasan `gate`, el de menor spread → 1-ITM → mayor liquidez (Opción 1 canónica
    de strategy_core.selection_key). None si ninguno pasa."""
    spot = market.underlying_price(ticker, at)
    if spot is None:
        return None
    chain = [c for c in market.chain(ticker, expiry, at) if c.right == right]
    chain.sort(key=lambda c: abs(c.strike - spot))
    cands: List[Contract] = []
    for c in chain[: params.max_strikes]:
        cq = dataclasses.replace(c, quote=market.quote(c.occ, at))
        if gate(cq):
            cands.append(cq)
    if not cands:
        return None
    return min(cands, key=lambda c: strategy_core.selection_key(
        c.quote.spread,
        strategy_core.itm_depth(c.strike, spot, right.value),
        c.volume, mode=params.mode))


def select_straddle(market: MarketData, clock: Clock, ticker: str, expiry: str,
                    entry_ts: Any, params: SelectionParams, gate: Gate
                    ) -> Tuple[Optional[Contract], Optional[Contract], Optional[Any]]:
    """Elige las DOS piernas (CALL+PUT) que entran juntas. VENTANA DE BÚSQUEDA: recorre
    los ticks desde `entry_ts` hasta `entry_ts + window_min`; devuelve el PRIMER tick donde
    AMBAS piernas tienen candidato (espera a que el spread de la subasta se cierre, sin
    forzar contratos ilíquidos). (None, None, None) si la ventana se agota sin match."""
    end = entry_ts + pd.Timedelta(minutes=float(params.window_min or 0))
    for t in clock.ticks(entry_ts, end):
        call = _best_leg_at(market, ticker, expiry, Right.CALL, t, params, gate)
        put = _best_leg_at(market, ticker, expiry, Right.PUT, t, params, gate)
        if call is not None and put is not None:
            return call, put, t
    return None, None, None


def select_single(market: MarketData, clock: Clock, ticker: str, expiry: str,
                  right: Right, entry_ts: Any, params: SelectionParams, gate: Gate
                  ) -> Tuple[Optional[Contract], Optional[Any]]:
    """Versión de una pierna (Sólo CALL / Sólo PUT) con la misma ventana de búsqueda."""
    end = entry_ts + pd.Timedelta(minutes=float(params.window_min or 0))
    for t in clock.ticks(entry_ts, end):
        leg = _best_leg_at(market, ticker, expiry, right, t, params, gate)
        if leg is not None:
            return leg, t
    return None, None

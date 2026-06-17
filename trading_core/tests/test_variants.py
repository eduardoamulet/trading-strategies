"""Pruebas de las variantes de Fase B (adapters FAKE, deterministas, sin Polygon):
  - Opción 2 (itm_first): elige el 1-ITM ignorando compuerta y rango.
  - both_plus: vende ambas a la hora de salida forzada.
  - call_or_put: vende ambas cuando CUALQUIER pierna toca +100%.
  - call_or_put_plus: banca la 1ª que llega al umbral; la 2ª sale al recuperar la inversión.

Corré:  python trading_core/tests/test_variants.py   (o pytest)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from trading_core.adapters.clocks import BacktestClock
from trading_core.adapters.simulated_broker import SimulatedBroker
from trading_core.domain import Contract, Quote, Right
from trading_core.execution import (run_both_plus, run_call_or_put,
                                     run_call_or_put_plus)
from trading_core.ports import MarketData
from trading_core.selection import SelectionParams, make_range_gate, select_single

ENTRY = pd.Timestamp("2026-06-11 09:30", tz="America/New_York")
EXP = "2026-06-11"


def _occ(r: str, k: int) -> str:
    return f"O:QQQ260611{r}00{k:03d}000"


class FakeMD(MarketData):
    """Datos en memoria; `qfn(occ, minuto)->(bid,ask)` define el escenario."""

    def __init__(self, qfn):
        self._qfn = qfn

    def underlying_price(self, ticker, at):
        return 700.0

    def chain(self, ticker, expiry, at):
        out = []
        for k in (695, 700, 705):
            out.append(Contract(_occ("C", k), "QQQ", expiry, k, Right.CALL))
            out.append(Contract(_occ("P", k), "QQQ", expiry, k, Right.PUT))
        return out

    def quote(self, occ, at):
        m = int((pd.Timestamp(at) - ENTRY).total_seconds() // 60)
        b, a = self._qfn(occ, m)
        return Quote(b, a, at)

    def nearest_expiry(self, ticker, on_or_after):
        return EXP


def _sim(md):
    return md, SimulatedBroker(md), BacktestClock(1)


def test_opcion2_itm_first_ignores_gate():
    # CALL 700 (ATM) con spread ANCHO; 695/705 tight. Opción 1 rechaza el ancho; Opción 2 lo elige.
    def q(occ, m):
        return {_occ("C", 700): (0.40, 0.60),   # spread 0.20 (ancho)
                _occ("C", 705): (0.28, 0.30),   # OTM tight
                _occ("C", 695): (0.78, 0.80)}.get(occ, (None, None))
    md, broker, clock = _sim(FakeMD(q))
    gate = make_range_gate(0.25, 0.85, max_spread=0.05)
    c1, _ = select_single(md, clock, "QQQ", EXP, Right.CALL, ENTRY,
                          SelectionParams(0.25, 0.85, criterion="spread"), gate)
    c2, _ = select_single(md, clock, "QQQ", EXP, Right.CALL, ENTRY,
                          SelectionParams(0.25, 0.85, criterion="itm_first"), gate)
    assert c2.strike == 700, c2.strike            # Opción 2 = ATM/1-ITM (ignora el spread ancho)
    assert c1.strike != 700, c1.strike            # Opción 1 = otro (la compuerta rechazó el ancho)


def test_both_plus_holds_to_exit_time():
    q = lambda occ, m: ((0.48, 0.50) if m == 0 else (0.55, 0.57)) \
        if occ in (_occ("C", 700), _occ("P", 700)) else (None, None)
    md, broker, clock = _sim(FakeMD(q))
    res = run_both_plus(md, broker, clock, ticker="QQQ", expiry=EXP, entry_ts=ENTRY,
                        exit_time=ENTRY + pd.Timedelta(minutes=5),
                        session_end=ENTRY + pd.Timedelta(minutes=10),
                        invest_call=1000, invest_put=1000,
                        params=SelectionParams(0.30, 0.70),
                        gate=make_range_gate(0.30, 0.70, max_spread=0.05))
    assert res.exit_reason == "forced_exit", res.exit_reason
    assert res.exit_ts == ENTRY + pd.Timedelta(minutes=5), res.exit_ts


def test_call_or_put_exits_on_either_100():
    def q(occ, m):
        if occ == _occ("C", 700):
            return (0.48, 0.50) if m == 0 else (0.60, 0.62) if m == 1 else (1.10, 1.12)
        if occ == _occ("P", 700):
            return (0.48, 0.50) if m == 0 else (0.40, 0.42) if m == 1 else (0.30, 0.32)
        return (None, None)
    md, broker, clock = _sim(FakeMD(q))
    res = run_call_or_put(md, broker, clock, ticker="QQQ", expiry=EXP, entry_ts=ENTRY,
                          session_end=ENTRY + pd.Timedelta(minutes=10),
                          invest_call=1000, invest_put=1000,
                          params=SelectionParams(0.30, 0.70),
                          gate=make_range_gate(0.30, 0.70, max_spread=0.05), target_pct=100.0)
    assert res.exit_reason == "take_profit", res.exit_reason
    assert res.exit_ts == ENTRY + pd.Timedelta(minutes=2), res.exit_ts   # CALL +120% en m2


def test_call_or_put_plus_banks_and_recovers():
    def q(occ, m):
        if occ == _occ("C", 700):
            return (0.48, 0.50) if m == 0 else (0.80, 0.82)   # CALL +60% desde m1
        if occ == _occ("P", 700):
            return (0.48, 0.50) if m == 0 else (0.30, 0.32)
        return (None, None)
    md, broker, clock = _sim(FakeMD(q))
    res = run_call_or_put_plus(md, broker, clock, ticker="QQQ", expiry=EXP, entry_ts=ENTRY,
                               session_end=ENTRY + pd.Timedelta(minutes=10),
                               invest_call=1000, invest_put=1000,
                               params=SelectionParams(0.30, 0.70),
                               gate=make_range_gate(0.30, 0.70, max_spread=0.05), target_pct=50.0)
    assert res.exit_reason == "recovered", res.exit_reason
    assert res.pnl > 0, res.pnl


if __name__ == "__main__":
    test_opcion2_itm_first_ignores_gate()
    test_both_plus_holds_to_exit_time()
    test_call_or_put_exits_on_either_100()
    test_call_or_put_plus_banks_and_recovers()
    print("✓ Opción 2 (itm_first), both_plus, call_or_put y call_or_put_plus — todos verdes (adapters FAKE).")

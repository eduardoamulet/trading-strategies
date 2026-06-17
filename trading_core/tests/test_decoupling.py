"""Prueba de DESACOPLE: el dominio (selección + ejecución + decisión) corre de punta a
punta con adapters FAKE en memoria — sin Polygon, sin red, sin archivos. Eso demuestra que
la lógica de negocio depende SOLO de los puertos (testeable y reusable).

Escenario: a las 09:30 el spread es ancho (subasta) → ningún contrato pasa; a las 09:31 se
cierra → entra; a las 09:32 la CALL sube y el straddle toca +10% → take_profit.

Corré:  python trading_core/tests/test_decoupling.py   (o pytest)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # repo root (Traiding/)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from trading_core.adapters.clocks import BacktestClock
from trading_core.adapters.simulated_broker import SimulatedBroker
from trading_core.domain import Contract, NoContractError, Quote, Right
from trading_core.execution import run_straddle
from trading_core.ports import MarketData
from trading_core.selection import SelectionParams, make_range_gate

ENTRY = pd.Timestamp("2026-06-11 09:30", tz="America/New_York")
EXP = "2026-06-11"


def _occ(right: str, strike: int) -> str:
    return f"O:QQQ260611{right}00{strike:03d}000"


CALL700, PUT700 = _occ("C", 700), _occ("P", 700)


class FakeMarketData(MarketData):
    """Datos scripteados en memoria — cero Polygon, cero red, deterministas."""

    def underlying_price(self, ticker, at):
        return 700.0

    def chain(self, ticker, expiry, at):
        return [Contract(_occ("C", 700), "QQQ", expiry, 700, Right.CALL),
                Contract(_occ("C", 705), "QQQ", expiry, 705, Right.CALL),
                Contract(_occ("P", 700), "QQQ", expiry, 700, Right.PUT),
                Contract(_occ("P", 695), "QQQ", expiry, 695, Right.PUT)]

    def quote(self, occ, at):
        m = int((pd.Timestamp(at) - ENTRY).total_seconds() // 60)
        bid = ask = None
        if occ == CALL700:
            if m <= 0:    bid, ask = 0.30, 0.50   # subasta: spread 0.20 (ANCHO → no pasa)
            elif m == 1:  bid, ask = 0.48, 0.50   # 09:31: spread 0.02 (tight → entra)
            else:         bid, ask = 0.80, 0.82   # sube
        elif occ == PUT700:
            if m <= 0:    bid, ask = 0.30, 0.50
            elif m == 1:  bid, ask = 0.48, 0.50
            else:         bid, ask = 0.30, 0.32
        return Quote(bid, ask, at)   # otros strikes → (None, None) → filtrados

    def nearest_expiry(self, ticker, on_or_after):
        return EXP


def _run(window: float):
    md = FakeMarketData()
    return run_straddle(
        md, SimulatedBroker(md), BacktestClock(step_min=1),
        ticker="QQQ", expiry=EXP, entry_ts=ENTRY,
        session_end=ENTRY + pd.Timedelta(minutes=15),
        invest_call=1000.0, invest_put=1000.0, umbral_pct=10.0, stop_pct=-100.0,
        params=SelectionParams(premium_min=0.30, premium_max=0.70, window_min=window),
        gate=make_range_gate(0.30, 0.70, max_spread=0.05))


def test_search_window_rescues_and_takes_profit():
    res = _run(window=5)
    assert res.entry_ts == ENTRY + pd.Timedelta(minutes=1), res.entry_ts        # entró 09:31
    assert res.exit_reason == "take_profit", res.exit_reason
    assert res.exit_ts == ENTRY + pd.Timedelta(minutes=2), res.exit_ts          # salió 09:32
    assert abs(res.roi - 0.10) < 1e-9, res.roi                                  # +10%
    assert abs(res.pnl - 200.0) < 1e-6, res.pnl


def test_no_window_rejects_auction_spread():
    try:
        _run(window=0)
    except NoContractError:
        return
    raise AssertionError("sin ventana, 09:30 (spread ancho) debería dar NoContractError")


if __name__ == "__main__":
    test_search_window_rescues_and_takes_profit()
    test_no_window_rejects_auction_spread()
    r = _run(5)
    print(f"OK · entró {r.entry_ts:%H:%M} · salió {r.exit_ts:%H:%M} ({r.exit_reason}) · "
          f"ROI {r.roi * 100:+.1f}% · PnL ${r.pnl:+.0f}")
    print("✓ Todos los tests pasaron — el dominio corrió con adapters FAKE (sin Polygon ni red).")

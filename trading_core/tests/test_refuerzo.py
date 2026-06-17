"""Prueba de la MARTINGALA (refuerzo) en la capa de puertos, con adapters FAKE en memoria.

Escenario: entran CALL+PUT a las 09:30. A las 09:31 la PUT se desploma a −64% (la CALL
queda plana) → se refuerza la PUT (compra más al ask). A las 09:32 la PUT se recupera; con
las tranches extra el ROI total dispara take_profit. El straddle SIN refuerzo, con los
mismos datos, NO alcanza el umbral → demuestra el mecanismo del refuerzo.

Corré:  python trading_core/tests/test_refuerzo.py   (o pytest)
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
from trading_core.execution import run_refuerzo, run_straddle
from trading_core.ports import MarketData
from trading_core.selection import SelectionParams, make_range_gate

ENTRY = pd.Timestamp("2026-06-11 09:30", tz="America/New_York")
END = ENTRY + pd.Timedelta(minutes=5)
EXP = "2026-06-11"
CALL, PUT = f"O:QQQ260611C00700000", f"O:QQQ260611P00700000"


class FakeMarketData(MarketData):
    def underlying_price(self, ticker, at):
        return 700.0

    def chain(self, ticker, expiry, at):
        return [Contract(CALL, "QQQ", expiry, 700, Right.CALL),
                Contract(PUT, "QQQ", expiry, 700, Right.PUT)]

    def quote(self, occ, at):
        m = int((pd.Timestamp(at) - ENTRY).total_seconds() // 60)
        if occ == CALL:
            b, a = (0.48, 0.50) if m <= 0 else (0.46, 0.48) if m == 1 else (0.50, 0.52)
        elif occ == PUT:
            b, a = (0.48, 0.50) if m <= 0 else (0.18, 0.20) if m == 1 else (0.55, 0.57)
        else:
            b = a = None
        return Quote(b, a, at)

    def nearest_expiry(self, ticker, on_or_after):
        return EXP


def _common():
    md = FakeMarketData()
    return dict(market=md, broker=SimulatedBroker(md), clock=BacktestClock(1),
                ticker="QQQ", expiry=EXP, entry_ts=ENTRY, session_end=END,
                invest_call=1000.0, invest_put=1000.0, umbral_pct=10.0, stop_pct=-100.0,
                params=SelectionParams(0.30, 0.70, window_min=0),
                gate=make_range_gate(0.30, 0.70, max_spread=0.05))


def test_refuerzo_rescues_to_take_profit():
    res = run_refuerzo(**_common(), refuerzo_loss_pct=50.0, refuerzo_max=2)
    assert len(res.reinforcements) == 1, res.reinforcements              # 1 refuerzo
    assert res.reinforcements[0]["right"] == "P", res.reinforcements     # reforzó la PUT (la que más pierde)
    assert res.exit_reason == "take_profit", res.exit_reason
    assert res.roi > 0.5, res.roi                                        # el rescate lo deja muy positivo


def test_plain_straddle_does_not_trigger_on_same_data():
    res = run_straddle(**_common())
    # Sin refuerzo, con los mismos datos, el ROI total no alcanza el umbral de +10% → no take_profit.
    assert res.exit_reason != "take_profit", res.exit_reason
    assert not res.reinforcements


if __name__ == "__main__":
    test_refuerzo_rescues_to_take_profit()
    test_plain_straddle_does_not_trigger_on_same_data()
    r = run_refuerzo(**_common(), refuerzo_loss_pct=50.0, refuerzo_max=2)
    s = run_straddle(**_common())
    print(f"REFUERZO   : {len(r.reinforcements)} refuerzo(s) ({r.reinforcements[0]['right']}) · "
          f"sale {r.exit_reason} · ROI {r.roi * 100:+.1f}%")
    print(f"STRADDLE   : sin refuerzo · sale {s.exit_reason} · ROI {s.roi * 100:+.1f}%")
    print("✓ El refuerzo rescató una PUT perdedora a take_profit; el straddle simple no disparó.")

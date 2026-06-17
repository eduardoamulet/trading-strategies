"""Prueba de los adapters de Tradier (Fase C): la MISMA lógica (run_refuerzo) corre por
`TradierMarketData` + `TradierBroker` contra un broker de Tradier FALSO en memoria — sin
red, sin credenciales, sin sandbox. Demuestra que migrar de backtest a paper = inyectar
estos adapters, sin tocar el dominio.

El FakeLiveBroker mimetiza la interfaz de live_trader/brokers (get_underlying_price,
get_option_chain, get_quote, nearest_expiry, place_order, get_order).

Corré:  python trading_core/tests/test_tradier_adapter.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from trading_core.adapters.clocks import LiveClock
from trading_core.adapters.tradier_live import TradierBroker, TradierMarketData
from trading_core.execution import run_refuerzo
from trading_core.selection import SelectionParams, make_range_gate

ENTRY = pd.Timestamp("2026-06-11 09:30", tz="America/New_York")
EXP = "2026-06-11"
CALL, PUT = "O:QQQ260611C00700000", "O:QQQ260611P00700000"


class FakeLiveBroker:
    """Imita un broker de Tradier (live_trader/brokers) — en memoria, determinista."""

    def __init__(self):
        self._t = 0
        self._orders = {}

    # --- datos ---
    def get_underlying_price(self, ticker):
        return 700.0

    def get_option_chain(self, ticker, expiry):
        return [SimpleNamespace(occ=CALL, strike=700.0, expiry=expiry, right="C", open_interest=5000, volume=100),
                SimpleNamespace(occ=PUT, strike=700.0, expiry=expiry, right="P", open_interest=5000, volume=100)]

    def get_quote(self, occ):
        # CALL sube, PUT plana → el straddle toca +umbral
        m = self._t
        if occ == CALL:
            b, a = (0.48, 0.50) if m <= 0 else (1.20, 1.22)
        else:
            b, a = (0.48, 0.50) if m <= 0 else (0.45, 0.47)
        return SimpleNamespace(occ=occ, bid=b, ask=a, last=(b + a) / 2, open_interest=5000, volume=100)

    def nearest_expiry(self, ticker):
        return EXP

    # --- ejecución ---
    def place_order(self, req):
        oid = f"ord-{len(self._orders) + 1}"
        # fill al limit pedido (compra al ask / venta al bid que pasó el adapter)
        self._orders[oid] = SimpleNamespace(order_id=oid, status="filled",
                                            filled_qty=req.qty, avg_price=req.limit_price)
        return SimpleNamespace(order_id=oid, status="ok", raw={})

    def get_order(self, order_id):
        return self._orders[order_id]


def _run():
    fake = FakeLiveBroker()
    md = TradierMarketData(fake)
    broker = TradierBroker(fake, max_wait_sec=1.0, poll_sec=0.0)
    # Clock "vivo" con reloj inyectado: 09:30 (entrada/selección), luego 09:31 (sube) y fin.
    seq = iter([ENTRY, ENTRY + pd.Timedelta(minutes=1), ENTRY + pd.Timedelta(minutes=2)])
    last = {"v": ENTRY}

    def now_fn():
        try:
            last["v"] = next(seq)
        except StopIteration:
            pass
        fake._t = int((pd.Timestamp(last["v"]) - ENTRY).total_seconds() // 60)
        return last["v"]

    clock = LiveClock(poll_sec=0.0, now_fn=now_fn)
    return run_refuerzo(
        md, broker, clock, ticker="QQQ", expiry=EXP, entry_ts=ENTRY,
        session_end=ENTRY + pd.Timedelta(minutes=2),
        invest_call=1000.0, invest_put=1000.0, umbral_pct=10.0, stop_pct=-100.0,
        params=SelectionParams(0.30, 0.70, window_min=0), gate=make_range_gate(0.30, 0.70, 0.05),
        refuerzo_loss_pct=50.0, refuerzo_max=2)


def test_same_strategy_runs_through_tradier_adapters():
    res = _run()
    assert res.exit_reason == "take_profit", res.exit_reason
    assert res.roi > 0, res.roi
    assert len(res.legs) >= 2          # entró CALL+PUT
    assert res.entry_ts == ENTRY


def test_sandbox_security_gate():
    """El runner en vivo (Fase D) DEBE negarse a correr si LIVE_TRADING_ENABLED=True."""
    from trading_core.live_runner import LiveTradingBlocked, assert_sandbox
    assert_sandbox(SimpleNamespace(LIVE_TRADING_ENABLED=False))   # sandbox → OK, no lanza
    try:
        assert_sandbox(SimpleNamespace(LIVE_TRADING_ENABLED=True))   # real → debe bloquear
    except LiveTradingBlocked:
        return
    raise AssertionError("assert_sandbox NO bloqueó con LIVE_TRADING_ENABLED=True")


if __name__ == "__main__":
    test_same_strategy_runs_through_tradier_adapters()
    test_sandbox_security_gate()
    r = _run()
    print(f"OK · run_refuerzo por adapters de Tradier (broker FAKE) · "
          f"sale {r.exit_reason} · ROI {r.roi * 100:+.1f}% · {len(r.legs)} pierna(s)")
    print("✓ Fase C: la MISMA estrategia corre contra Tradier sin tocar el dominio.")
    print("✓ Fase D: el guard de sandbox bloquea LIVE_TRADING_ENABLED=True.")

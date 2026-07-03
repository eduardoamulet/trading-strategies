"""Prueba de los adapters de Alpaca (paper): la MISMA lógica (run_refuerzo) corre por
`AlpacaMarketData` + `AlpacaBroker` contra clientes de alpaca-py FALSOS en memoria — sin
red, sin credenciales. Demuestra que migrar a Alpaca paper = inyectar estos adapters,
sin tocar el dominio (idéntico al test de Tradier).

Los fakes mimetizan la superficie del SDK: TradingClient (get_option_contracts,
submit_order, get_order_by_id), OptionHistoricalDataClient (get_option_latest_quote) y
StockHistoricalDataClient (get_stock_latest_trade) — con numéricos como STRING y enums,
como los devuelve alpaca-py de verdad.

Corré:  python trading_core/tests/test_alpaca_adapter.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from trading_core.adapters.alpaca_live import AlpacaBroker, AlpacaMarketData, _plain_occ
from trading_core.adapters.clocks import LiveClock
from trading_core.execution import run_refuerzo
from trading_core.selection import SelectionParams, make_range_gate

ENTRY = pd.Timestamp("2026-06-11 09:30", tz="America/New_York")
EXP = "2026-06-11"
CALL, PUT = "QQQ260611C00700000", "QQQ260611P00700000"    # OCC plano (estilo Alpaca)


class FakeTradingClient:
    """Imita alpaca.trading.TradingClient — en memoria, determinista."""

    def __init__(self, quotes):
        self._orders = {}
        self._quotes = quotes                    # comparte los precios con el data client

    def get_option_contracts(self, req):
        cons = [SimpleNamespace(symbol=CALL, strike_price="700", expiration_date=EXP,
                                type=SimpleNamespace(value="call"), open_interest="5000"),
                SimpleNamespace(symbol=PUT, strike_price="700", expiration_date=EXP,
                                type=SimpleNamespace(value="put"), open_interest="5000")]
        return SimpleNamespace(option_contracts=cons, next_page_token=None)

    def submit_order(self, req):
        oid = f"alp-{len(self._orders) + 1}"
        # paper: fill al limit pedido (marketable) — numéricos como string, como el SDK real
        self._orders[oid] = SimpleNamespace(
            id=oid, status=SimpleNamespace(value="filled"),
            filled_avg_price=str(req.limit_price), filled_qty=str(req.qty))
        return SimpleNamespace(id=oid, status=SimpleNamespace(value="accepted"))

    def get_order_by_id(self, oid):
        return self._orders[oid]


class FakeQuotes:
    """Estado compartido de precios: la CALL sube tras la entrada, la PUT queda plana."""

    def __init__(self):
        self.t = 0

    def bidask(self, sym):
        if sym == CALL:
            return (0.48, 0.50) if self.t <= 0 else (1.20, 1.22)
        return (0.48, 0.50) if self.t <= 0 else (0.45, 0.47)


class FakeOptionData:
    def __init__(self, quotes):
        self._q = quotes

    def get_option_latest_quote(self, req):
        sym = req.symbol_or_symbols
        b, a = self._q.bidask(sym)
        return {sym: SimpleNamespace(bid_price=b, ask_price=a, bid_size=10, ask_size=10)}


class FakeStockData:
    def get_stock_latest_trade(self, req):
        sym = req.symbol_or_symbols
        return {sym: SimpleNamespace(price=700.0)}


def _run():
    quotes = FakeQuotes()
    trading = FakeTradingClient(quotes)
    md = AlpacaMarketData(trading, FakeOptionData(quotes), FakeStockData())
    broker = AlpacaBroker(trading, option_data=FakeOptionData(quotes),
                          max_wait_sec=1.0, poll_sec=0.0)
    seq = iter([ENTRY, ENTRY + pd.Timedelta(minutes=1), ENTRY + pd.Timedelta(minutes=2)])
    last = {"v": ENTRY}

    def now_fn():
        try:
            last["v"] = next(seq)
        except StopIteration:
            pass
        quotes.t = int((pd.Timestamp(last["v"]) - ENTRY).total_seconds() // 60)
        return last["v"]

    clock = LiveClock(poll_sec=0.0, now_fn=now_fn)
    return run_refuerzo(
        md, broker, clock, ticker="QQQ", expiry=EXP, entry_ts=ENTRY,
        session_end=ENTRY + pd.Timedelta(minutes=2),
        invest_call=1000.0, invest_put=1000.0, umbral_pct=10.0, stop_pct=-100.0,
        params=SelectionParams(0.30, 0.70, window_min=0), gate=make_range_gate(0.30, 0.70, 0.05),
        refuerzo_loss_pct=50.0, refuerzo_max=2)


def test_same_strategy_runs_through_alpaca_adapters():
    res = _run()
    assert res.exit_reason == "take_profit", res.exit_reason
    assert res.roi > 0, res.roi
    assert len(res.legs) >= 2          # entró CALL+PUT
    assert res.entry_ts == ENTRY


def test_occ_normalization():
    """El dominio puede traer el OCC estilo Polygon («O:…») — el adapter lo normaliza."""
    assert _plain_occ("O:QQQ260611C00700000") == CALL
    assert _plain_occ(CALL) == CALL


def test_builder_es_paper_por_construccion():
    """build_alpaca_ports construye el TradingClient con paper=True FIJO (no configurable):
    se verifica sobre el código fuente para que un refactor no lo afloje en silencio."""
    import inspect

    from trading_core import live_runner
    src = inspect.getsource(live_runner.build_alpaca_ports)
    assert "paper=True" in src
    assert "paper=False" not in src


if __name__ == "__main__":
    test_same_strategy_runs_through_alpaca_adapters()
    test_occ_normalization()
    test_builder_es_paper_por_construccion()
    r = _run()
    print(f"OK · run_refuerzo por adapters de Alpaca (clientes FAKE) · "
          f"exit={r.exit_reason} · roi={r.roi:.1f}% · legs={len(r.legs)}")

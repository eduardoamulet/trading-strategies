"""Unit tests de la captura de snapshots de la cadena (chain_snapshots) — headless, sin red.

Corré:  pytest tests/test_chain_snapshots.py -q
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
import chain_snapshots as cs  # noqa: E402


def _item(occ, strike, *, price=None, value=None, iv=0.25, con_greeks=True):
    it = {"details": {"ticker": occ, "strike_price": strike, "expiration_date": "2026-07-06",
                      "contract_type": "call"},
          "day": {"volume": 123},
          "open_interest": 456,
          "implied_volatility": iv,
          "last_quote": {"bid": 1.20, "ask": 1.30},
          "last_trade": {"price": 1.24},
          "underlying_asset": ({"price": price} if price is not None else
                               ({"value": value} if value is not None else {}))}
    if con_greeks:
        it["greeks"] = {"delta": 0.51, "gamma": 0.02, "theta": -0.30, "vega": 0.05}
    return it


class _FakeAdapter:
    def __init__(self, por_ticker):
        self.por_ticker = por_ticker
        self.llamadas = []

    def option_chain_snapshot(self, tk, **kw):
        self.llamadas.append(tk)
        return self.por_ticker.get(tk, [])


def test_captura_banda_dedupe_y_campos(tmp_path):
    db = tmp_path / "snap.db"
    fake = _FakeAdapter({
        "SPY": [_item("O:SPY1", 100.0, price=100.0),          # dentro de banda
                _item("O:SPY2", 108.0, price=100.0),          # borde: |108-100| <= 10 ✓
                _item("O:SPY3", 250.0, price=100.0),          # fuera de banda ±10 % → excluido
                {"details": {}}],                             # malformado → ignorado
        "SPX": [_item("O:SPX1", 7500.0, value=7480.0)],       # índice: spot viene en 'value'
    })
    n = cs.capture("apertura", tickers=["SPY", "SPX"], db_path=db, force=True, adapter=fake)
    assert n == 3                                             # SPY1, SPY2, SPX1
    con = sqlite3.connect(db)
    filas = {r[0]: r for r in con.execute(
        "SELECT occ, mid, spot, oi, delta FROM chain_snapshot")}
    assert set(filas) == {"O:SPY1", "O:SPY2", "O:SPX1"}
    assert filas["O:SPY1"][1] == 1.25                         # mid = (bid+ask)/2
    assert filas["O:SPX1"][2] == 7480.0                       # spot del índice via 'value'
    assert filas["O:SPY1"][3] == 456 and filas["O:SPY1"][4] == 0.51
    con.close()
    # re-captura del mismo momento → dedupe total por PK
    assert cs.capture("apertura", tickers=["SPY", "SPX"], db_path=db,
                      force=True, adapter=fake) == 0
    # otro momento del mismo día → espacio nuevo
    assert cs.capture("cierre", tickers=["SPX"], db_path=db, force=True, adapter=fake) == 1


def test_sin_greeks_no_rompe(tmp_path):
    db = tmp_path / "snap.db"
    fake = _FakeAdapter({"QQQ": [_item("O:QQQ1", 500.0, price=500.0, con_greeks=False)]})
    assert cs.capture("apertura", tickers=["QQQ"], db_path=db, force=True, adapter=fake) == 1
    con = sqlite3.connect(db)
    d, = con.execute("SELECT delta FROM chain_snapshot").fetchone()
    assert d is None                                          # greeks ausentes → NULL, no crash
    con.close()


def test_guard_dia_habil():
    assert cs._es_dia_habil("2026-07-06") is True             # lunes
    assert cs._es_dia_habil("2026-07-04") is False            # sábado
    assert cs._es_dia_habil("2026-07-03") is False            # feriado NYSE (4 jul observado)

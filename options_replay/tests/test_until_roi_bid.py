"""Unit tests del ajuste-al-bid Fase 1 (engine.exit_at_bid) sobre piernas YA VENDIDAS por su
propia gestión en "call_or_put_until_roi" y "call_or_put_plus".

Regresión: la pierna que vende ANTES por umbral queda CONGELADA a su precio de venta; el
ajuste-al-bid del minuto de salida GLOBAL no debe pisar esa última fila congelada (alteraba
exit premium y gain de una pierna ya bancada). La pierna que sale EN el minuto global (EOD o
hit en la última fila) SÍ se ajusta a su bid. Solo Fase 1 (exit_at_bid=True, sin nbbo_timeline).

Headless: FakeDownloader con series sintéticas (sin Polygon, sin cache), estilo test_leg_stop.
Corré:  pytest tests/test_until_roi_bid.py   (o  python tests/test_until_roi_bid.py)
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
from engine import run_next_iteration  # noqa: E402

TZ = "America/New_York"
DATE = "2026-04-27"
T0 = pd.Timestamp(f"{DATE} 09:30", tz=TZ)
CALL_OCC, PUT_OCC = "O:TST260427C00099000", "O:TST260427P00101000"
NO_SPREAD = {"enable_spread_filter": False, "buckets": []}


def _bars(closes, open0=1.0):
    """Barras 1-min: open constante (solo la fila 0 se usa como prima de entrada) y
    close = la serie a testear (la valuación del engine usa el close)."""
    n = len(closes)
    return pd.DataFrame({
        "timestamp": [T0 + pd.Timedelta(minutes=i) for i in range(n)],
        "open": [float(open0)] * n, "high": [10.0] * n, "low": [0.0] * n,
        "close": [float(c) for c in closes], "volume": [100] * n,
    })


class FakeDownloader:
    """Solo lo que toca run_next_iteration en modo precio-de-barra: underlying, chain,
    option y (para Fase 1) option_quote. `quotes` = {(occ, "HH:MM"): bid}."""

    def __init__(self, call_closes, put_closes, quotes=None):
        n = len(call_closes)
        self._under = _bars([100.0] * n, open0=100.0)
        self._opts = {CALL_OCC: _bars(call_closes), PUT_OCC: _bars(put_closes)}
        self._quotes = quotes or {}

    def underlying(self, ticker, date, force=False, resolution=None):
        return self._under.copy()

    def chain(self, ticker, expiry, force=False):
        return pd.DataFrame({"contract_type": ["call", "put"],
                             "strike_price": [99.0, 101.0],
                             "ticker": [CALL_OCC, PUT_OCC]})

    def option(self, occ, date, force=False, resolution=None):
        return self._opts[occ].copy()

    def option_quote(self, occ, date, entry_ts, force=False):
        bid = self._quotes.get((occ, pd.Timestamp(entry_ts).strftime("%H:%M")))
        return {"bid": bid, "ask": None, "bid_size": None, "ask_size": None, "spread": None}


def _run(dl, mode="call_or_put_until_roi", umbral=1.0):
    n = len(dl._under)
    return run_next_iteration(
        dl, "TST", DATE, 0.5, 5.0, 500.0, 500.0,
        T0, T0 + pd.Timedelta(minutes=n - 1),
        exit_threshold_pct=umbral, exit_plus_threshold_pct=0.5, mode=mode,
        selection_criterion="itm_first", spread_cfg=NO_SPREAD, exit_at_bid=True,
    )


# until_roi (+100%): la CALL cruza el umbral en idx3 (2.00) y luego cae; la PUT nunca llega.
CALL_U = [1.00, 1.20, 1.50, 2.00, 1.80, 1.50, 1.20, 1.00, 0.90, 0.80]
PUT_U = [1.00, 0.98, 0.95, 0.93, 0.92, 0.91, 0.90, 0.90, 0.90, 0.90]


def test_until_roi_fase1_no_pisa_la_pierna_vendida_por_umbral():
    # El bid trampa de la CALL al cierre (1.20) NO debe pisar su 2.00 congelado; la PUT
    # viva (EOD) SÍ se ajusta al bid del cierre (0.85).
    quotes = {(CALL_OCC, "09:39"): 1.20, (PUT_OCC, "09:39"): 0.85}
    it = _run(FakeDownloader(CALL_U, PUT_U, quotes=quotes))
    assert it.call_exit_reason == "100%_threshold" and it.call_exit_idx == 3
    assert it.put_exit_reason == "session_end" and it.put_exit_idx == 9
    assert it.call_exit_premium == 2.00                      # bancada al umbral, no al bid EOD
    assert (it.df["call_px"].iloc[3:] == 2.00).all()         # congelada desde la venta
    assert it.put_exit_premium == 0.85                       # la viva sí vende al bid
    assert it.df["total"].iloc[-1] == 2.00 + 0.85
    assert abs(it.gain_total - (500 * (2.00 - 1) + 500 * (0.85 - 1))) < 1e-6   # +500 − 75


def test_until_roi_fase1_ajusta_al_bid_el_hit_en_la_ultima_fila():
    # La PUT alcanza el umbral EN la última fila (su venta ES el minuto de salida global)
    # → SÍ se ajusta a su bid (2.05); la CALL congelada en idx3 no se toca.
    put = [1.00, 1.05, 1.10, 1.15, 1.20, 1.30, 1.40, 1.55, 1.66, 2.10]
    quotes = {(CALL_OCC, "09:39"): 1.20, (PUT_OCC, "09:39"): 2.05}
    it = _run(FakeDownloader(CALL_U, put, quotes=quotes))
    assert it.exit_reason == "100%_threshold"
    assert it.put_exit_reason == "100%_threshold" and it.put_exit_idx == 9
    assert it.put_exit_premium == 2.05
    assert it.call_exit_premium == 2.00


def test_plus_fase1_no_pisa_la_pierna_A():
    # plus (umbral A 50%): la CALL (A) vende en idx2 (1.60, bancada 800); la PUT (B) recupera
    # T_total en idx7 (0.40×500 + 800 >= 1000). El bid trampa de la CALL al minuto de B (1.00)
    # NO debe pisar el 1.60 congelado; B sí vende a su bid (0.39).
    call = [1.00, 1.30, 1.60, 1.20, 1.00, 0.90, 0.80, 0.75, 0.70, 0.65]
    put = [1.00, 0.50, 0.30, 0.30, 0.32, 0.35, 0.38, 0.40, 0.42, 0.45]
    quotes = {(CALL_OCC, "09:37"): 1.00, (PUT_OCC, "09:37"): 0.39}
    it = _run(FakeDownloader(call, put, quotes=quotes), mode="call_or_put_plus")
    assert it.call_exit_reason == "100%_threshold" and it.call_exit_idx == 2
    assert it.put_exit_reason == "100%_threshold" and it.put_exit_idx == 7
    assert it.call_exit_premium == 1.60                      # bancada al umbral, no al bid de B
    assert (it.df["call_px"].iloc[2:] == 1.60).all()         # congelada desde la venta
    assert it.put_exit_premium == 0.39                       # B sí vende al bid
    assert abs(it.gain_total - (500 * (1.60 - 1) + 500 * (0.39 - 1))) < 1e-6   # +300 − 305


if __name__ == "__main__":
    for _n, _f in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        _f()
        print(f"OK {_n}")
    print("TODO OK")

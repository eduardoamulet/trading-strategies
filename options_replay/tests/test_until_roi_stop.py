"""Unit tests del STOP POR PIERNA en "call_or_put_until_roi" (2026-07-08).

Antes el modo IGNORABA stop_loss_pct: una pierna sin umbral alcanzado sangraba hasta el
cierre aunque el stop estuviera armado (QQQ·PUT −98% con stop −40 fue el caso reportado).
Ahora cada pierna sale por SU umbral O SU stop (el primero que dispare; empate → umbral),
y con el stop en −100%/centinela el modo queda BIT-EXACTO al comportamiento previo.

Headless: FakeDownloader sintético (estilo test_until_roi_bid).
Corré:  pytest tests/test_until_roi_stop.py
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
    n = len(closes)
    return pd.DataFrame({
        "timestamp": [T0 + pd.Timedelta(minutes=i) for i in range(n)],
        "open": [float(open0)] * n, "high": [10.0] * n, "low": [0.0] * n,
        "close": [float(c) for c in closes], "volume": [100] * n,
    })


class FakeDownloader:
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


def _run(dl, umbral=1.0, stop=-1.0):
    n = len(dl._under)
    return run_next_iteration(
        dl, "TST", DATE, 0.5, 5.0, 500.0, 500.0,
        T0, T0 + pd.Timedelta(minutes=n - 1),
        exit_threshold_pct=umbral, stop_loss_pct=stop, exit_metric="total",
        mode="call_or_put_until_roi",
        selection_criterion="itm_first", spread_cfg=NO_SPREAD, exit_at_bid=True,
    )


# La CALL cobra por umbral en idx3 (2.00 = +100%); la PUT sangra sin tocar umbral.
CALL_OK = [1.00, 1.20, 1.50, 2.00, 1.80, 1.50, 1.20, 1.00, 0.90, 0.80]
PUT_BLEED = [1.00, 0.80, 0.55, 0.50, 0.45, 0.30, 0.20, 0.10, 0.05, 0.02]


def test_stop_armado_corta_la_pierna_sangrante():
    # stop −40%: la PUT toca −45% en idx2 (0.55) → vende AHÍ (stop_loss) y congela; la
    # CALL cobra por umbral en idx3. Sin el fix, la PUT llegaba a 0.02 (−98%) al cierre.
    it = _run(FakeDownloader(CALL_OK, PUT_BLEED), stop=-0.40)
    assert it.put_exit_reason == "stop_loss" and it.put_exit_idx == 2
    assert it.put_exit_premium == 0.55
    assert (it.df["put_px"].iloc[2:] == 0.55).all()          # congelada desde el stop
    assert it.call_exit_reason == "100%_threshold" and it.call_exit_idx == 3
    assert it.exit_reason == "100%_threshold"                # global: umbral > stop
    assert abs(it.gain_total - (500 * (2.00 - 1) + 500 * (0.55 - 1))) < 1e-6   # +500 − 225


def test_stop_fase1_vende_al_bid_puntual():
    # Fase 1: la venta por stop usa el BID puntual del minuto del stop (0.53 < mark 0.55).
    quotes = {(PUT_OCC, "09:32"): 0.53}
    it = _run(FakeDownloader(CALL_OK, PUT_BLEED, quotes=quotes), stop=-0.40)
    assert it.put_exit_reason == "stop_loss" and it.put_exit_premium == 0.53
    assert (it.df["put_px"].iloc[2:] == 0.53).all()


def test_stop_gana_si_dispara_antes_que_el_umbral():
    # La PUT toca −40% en idx2 y DESPUÉS se dispara a +100% en idx6: manda el PRIMER
    # trigger (stop en idx2) — doloroso pero honesto, sin mirar el futuro.
    put = [1.00, 0.80, 0.55, 0.70, 1.20, 1.80, 2.10, 2.10, 2.10, 2.10]
    it = _run(FakeDownloader(CALL_OK, put), stop=-0.40)
    assert it.put_exit_reason == "stop_loss" and it.put_exit_idx == 2


def test_umbral_gana_el_empate_y_el_orden():
    # El umbral de la PUT (idx1, 2.00) llega ANTES que su stop (nunca) → umbral normal;
    # y si ambos existieran, gana el de índice menor (up <= dn).
    put = [1.00, 2.00, 1.50, 0.50, 0.40, 0.30, 0.20, 0.10, 0.05, 0.02]
    it = _run(FakeDownloader(CALL_OK, put), stop=-0.40)
    assert it.put_exit_reason == "100%_threshold" and it.put_exit_idx == 1


def test_stop_apagado_es_bit_exacto_al_comportamiento_previo():
    # Default del panel (−1.0) y centinela del batch (−1000): la PUT sangra hasta el
    # cierre con session_end — el comportamiento de SIEMPRE (verify-safe).
    for stop in (-1.0, -1000.0):
        it = _run(FakeDownloader(CALL_OK, PUT_BLEED), stop=stop)
        assert it.put_exit_reason == "session_end" and it.put_exit_idx == 9
        assert it.exit_reason == "100%_threshold"


def test_ambas_por_stop_reason_global_stop():
    call = [1.00, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15]
    put = [1.00, 0.80, 0.55, 0.50, 0.45, 0.30, 0.20, 0.10, 0.05, 0.02]
    it = _run(FakeDownloader(call, put), stop=-0.40)
    assert it.call_exit_reason == "stop_loss" and it.put_exit_reason == "stop_loss"
    assert it.exit_reason == "stop_loss"


if __name__ == "__main__":
    for _n, _f in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        _f()
        print(f"OK {_n}")
    print("TODO OK")

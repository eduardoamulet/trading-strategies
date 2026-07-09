"""Unit tests del TIME-STOP (2026-07-09, protocolo GEX): a la hora límite, si el ROI
combinado va ≤ umbral, la posición corta AHÍ (sin mirar el futuro); si va por encima,
el día sigue completo. Off → bit-exacto. Headless (FakeDownloader sintético)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import run_next_iteration  # noqa: E402

TZ = "America/New_York"
DATE = "2026-04-27"
T0 = pd.Timestamp(f"{DATE} 09:30", tz=TZ)
CALL_OCC, PUT_OCC = "O:TST260427C00099000", "O:TST260427P00101000"
NO_SPREAD = {"enable_spread_filter": False, "buckets": []}
N = 130                       # 09:30 .. 11:39 (la barra de las 11:30 es la #120)
IDX_1130 = 120


def _bars(closes, open0=1.0):
    n = len(closes)
    return pd.DataFrame({
        "timestamp": [T0 + pd.Timedelta(minutes=i) for i in range(n)],
        "open": [float(open0)] * n, "high": [10.0] * n, "low": [0.0] * n,
        "close": [float(c) for c in closes], "volume": [100] * n,
    })


class FakeDownloader:
    def __init__(self, call_closes, put_closes):
        n = len(call_closes)
        self._under = _bars([100.0] * n, open0=100.0)
        self._opts = {CALL_OCC: _bars(call_closes), PUT_OCC: _bars(put_closes)}

    def underlying(self, ticker, date, force=False, resolution=None):
        return self._under.copy()

    def chain(self, ticker, expiry, force=False):
        return pd.DataFrame({"contract_type": ["call", "put"],
                             "strike_price": [99.0, 101.0],
                             "ticker": [CALL_OCC, PUT_OCC]})

    def option(self, occ, date, force=False, resolution=None):
        return self._opts[occ].copy()

    def option_quote(self, occ, date, entry_ts, force=False):
        return {"bid": None, "ask": None, "bid_size": None, "ask_size": None, "spread": None}


def _run(dl, mode="both", umbral=100.0, tstop=None, tstop_pct=-0.20):
    n = len(dl._under)
    return run_next_iteration(
        dl, "TST", DATE, 0.5, 5.0, 500.0, 500.0,
        T0, T0 + pd.Timedelta(minutes=n - 1),
        exit_threshold_pct=umbral, exit_metric="total", mode=mode,
        selection_criterion="itm_first", spread_cfg=NO_SPREAD,
        time_stop_hora=tstop, time_stop_roi_pct=tstop_pct,
    )


# CALL cayendo linealmente a 0.50 hacia la barra 120 y quieta después; PUT plana en 1.00
# → ROI combinado en la barra de las 11:30 = ((0.50−1)×500 + 0) / 1000 = −25% (≤ −20).
CALL_BLEED = [1.0 - 0.5 * min(i, IDX_1130) / IDX_1130 for i in range(N)]
PUT_FLAT = [1.00] * N


def test_time_stop_corta_a_la_hora_limite():
    it = _run(FakeDownloader(CALL_BLEED, PUT_FLAT), tstop="11:30")
    assert it.exit_reason == "time_stop"
    assert it.end_dt.strftime("%H:%M") == "11:30"
    assert len(it.df) == IDX_1130 + 1                     # truncado en la barra 120
    # En modo "both" clásico las piernas no llevan metadata propia (convención existente:
    # la razón GLOBAL cubre la posición); until_roi sí las etiqueta (test de abajo).
    assert it.call_exit_reason == "" and it.put_exit_reason == ""
    assert abs(it.gain_total - (-250.0)) < 1e-6           # −25% de $1,000


def test_por_encima_del_umbral_no_corta():
    # Combinado a las 11:30 = −2.5% (> −20) → día completo, cierre normal.
    call = [1.0 - 0.05 * min(i, IDX_1130) / IDX_1130 for i in range(N)]
    it = _run(FakeDownloader(call, PUT_FLAT), tstop="11:30")
    assert it.exit_reason == "session_end"
    assert len(it.df) == N
    assert it.end_dt.strftime("%H:%M") == "11:39"


def test_apagado_es_bit_exacto():
    a = _run(FakeDownloader(CALL_BLEED, PUT_FLAT), tstop=None)
    b = _run(FakeDownloader(CALL_BLEED, PUT_FLAT), tstop="")
    assert a.exit_reason == b.exit_reason == "session_end"
    assert abs(a.gain_total - b.gain_total) < 1e-9
    assert len(a.df) == len(b.df) == N


def test_umbral_anterior_gana():
    # La CALL dispara el umbral combinado (+60% ≥ 50%) en la barra 30 — mucho antes de las
    # 11:30: el time-stop ni participa.
    call = [1.0] * 30 + [2.2] * (N - 30)
    it = _run(FakeDownloader(call, PUT_FLAT), umbral=0.5, tstop="11:30")
    assert it.exit_reason == "100%_threshold"
    assert it.end_dt.strftime("%H:%M") < "11:30"


def test_umbral_del_time_stop_configurable():
    # Con umbral −30, un −25% a las 11:30 NO corta; con −20 sí (caso base de arriba).
    it = _run(FakeDownloader(CALL_BLEED, PUT_FLAT), tstop="11:30", tstop_pct=-0.30)
    assert it.exit_reason == "session_end" and len(it.df) == N


def test_until_roi_tambien_respeta_el_time_stop():
    # Ambas piernas vivas y sangrando → a las 11:30 el combinado va −25% → corta y las
    # piernas vivas quedan etiquetadas time_stop (no «session_end»: eran las 11:30).
    put = [1.0 - 0.5 * min(i, IDX_1130) / IDX_1130 for i in range(N)]
    it = _run(FakeDownloader(CALL_BLEED, put), mode="call_or_put_until_roi",
              umbral=1.0, tstop="11:30", tstop_pct=-0.20)
    assert it.exit_reason == "time_stop"
    assert it.end_dt.strftime("%H:%M") == "11:30"
    assert it.call_exit_reason == "time_stop" and it.put_exit_reason == "time_stop"


if __name__ == "__main__":
    for _n, _f in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        _f()
        print(f"OK {_n}")
    print("TODO OK")

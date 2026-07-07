"""Unit tests del STOP POR PIERNA (engine.leg_stop_loss_pct — estudio refuerzo-vs-contra).

Headless: FakeDownloader con series sintéticas (sin Polygon, sin cache). Cubre: venta+congelado
de la pierna que toca −X%, triggers de salida sobre la serie congelada, salida global previa
(marca limpiada), exclusión con refuerzo y en single-leg, y fills Fase 1 (bid puntual).
Corré:  pytest tests/test_leg_stop.py   (o  python tests/test_leg_stop.py)
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


def _run(dl, mode="both", leg_stop=-0.5, umbral=10.0, stop=-1.0, refuerzo=False,
         exit_at_bid=False):
    n = len(dl._under)
    return run_next_iteration(
        dl, "TST", DATE, 0.5, 5.0, 500.0 if mode != "put_only" else 0.0,
        500.0 if mode != "call_only" else 0.0,
        T0, T0 + pd.Timedelta(minutes=n - 1),
        exit_threshold_pct=umbral, stop_loss_pct=stop, mode=mode,
        selection_criterion="itm_first", spread_cfg=NO_SPREAD,
        apply_refuerzo=refuerzo, exit_at_bid=exit_at_bid,
        leg_stop_loss_pct=leg_stop,
    )


# Serie base: la CALL cruza −50% en idx3 (0.45) y sigue cayendo; la PUT sube.
CALL_A = [1.00, 0.80, 0.60, 0.45, 0.40, 0.38, 0.35, 0.33, 0.30, 0.28]
PUT_A = [1.00, 1.05, 1.10, 1.15, 1.20, 1.30, 1.40, 1.55, 1.66, 1.78]


def test_congela_y_banca_la_pierna_stopeada():
    it = _run(FakeDownloader(CALL_A, PUT_A))
    assert it.call_exit_reason == "leg_stop_loss" and it.call_exit_idx == 3
    assert it.put_exit_reason == "" and it.exit_reason == "session_end"
    assert it.call_exit_premium == 0.45                      # vendida al cruce, no al cierre
    assert (it.df["call_px"].iloc[3:] == 0.45).all()         # congelada desde la venta
    assert abs(it.gain_total - (500 * (0.45 - 1) + 500 * (1.78 - 1))) < 1e-6   # −275 + 390


def test_sin_leg_stop_mantiene_comportamiento_previo():
    it = _run(FakeDownloader(CALL_A, PUT_A), leg_stop=None)
    assert it.call_exit_reason == "" and it.call_exit_idx is None
    assert it.call_exit_premium == 0.28                      # la CALL corre hasta el cierre
    assert abs(it.gain_total - (500 * (0.28 - 1) + 500 * (1.78 - 1))) < 1e-6   # −360 + 390


def test_umbral_total_dispara_sobre_la_serie_congelada():
    # Umbral 11%: con la CALL congelada en −55%, la PUT en +78% da total +11.5% → dispara.
    # SIN leg-stop la CALL llega a −72% y el total (+3%) nunca alcanza → session_end.
    it = _run(FakeDownloader(CALL_A, PUT_A), umbral=0.11)
    assert it.exit_reason == "100%_threshold"
    it2 = _run(FakeDownloader(CALL_A, PUT_A), umbral=0.11, leg_stop=None)
    assert it2.exit_reason == "session_end"


def test_salida_global_previa_limpia_la_marca():
    # Stop TOTAL −10% dispara en idx1 (CALL −30%, PUT 0 → total −15%), ANTES del cruce
    # −50% de la CALL (idx2) → el leg-stop nunca se ejecutó: marca limpia, df truncado.
    call = [1.00, 0.70, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40]
    put = [1.00] * 10
    it = _run(FakeDownloader(call, put), stop=-0.10)
    assert it.exit_reason == "stop_loss" and len(it.df) == 2
    assert it.call_exit_reason == "" and it.call_exit_idx is None


def test_refuerzo_activo_ignora_leg_stop():
    # Semántica opuesta (la martingala COMPRA la pierna que el stop vendería) → no combina.
    it = _run(FakeDownloader(CALL_A, PUT_A), refuerzo=True)
    assert it.refuerzo is not None
    assert it.call_exit_reason == "" and it.put_exit_reason == ""


def test_single_leg_no_aplica():
    it = _run(FakeDownloader(CALL_A, PUT_A), mode="call_only")
    assert it.call_exit_reason == "" and it.exit_reason == "session_end"
    assert it.call_exit_premium == 0.28                      # sin stop por pierna


def test_fase1_vende_al_bid_puntual_y_no_pisa_el_congelado():
    # Fase 1 (exit_at_bid): la pierna stopeada se vende al BID del minuto del cruce (0.35,
    # no el close 0.45) y el ajuste-al-bid de la salida global NO la pisa; la PUT viva sí
    # se ajusta al bid del cierre (1.70).
    quotes = {(CALL_OCC, "09:33"): 0.35, (PUT_OCC, "09:39"): 1.70}
    it = _run(FakeDownloader(CALL_A, PUT_A, quotes=quotes), exit_at_bid=True)
    assert it.call_exit_reason == "leg_stop_loss" and it.call_exit_premium == 0.35
    assert it.put_exit_premium == 1.70
    assert abs(it.gain_total - (500 * (0.35 - 1) + 500 * (1.70 - 1))) < 1e-6   # −325 + 350


if __name__ == "__main__":
    for _n, _f in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        _f()
        print(f"OK {_n}")
    print("TODO OK")

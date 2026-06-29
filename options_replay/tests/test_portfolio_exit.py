"""Unit tests para portfolio_exit.apply_collective_exit — salida colectiva (ROI / stop).

Headless: usa un IterationResult mock (duck-typing) con un timeline de ROI de cartera controlado.
Corré:  python tests/test_portfolio_exit.py   (o  pytest tests/test_portfolio_exit.py)
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
from portfolio_exit import apply_collective_exit  # noqa: E402


class MockIt:
    """IterationResult mínimo: solo los atributos que lee apply_collective_exit."""

    def __init__(self, ticker, pxs, entry_call, invest_call):
        base = pd.Timestamp("2026-04-27 09:32:00")
        self.df = pd.DataFrame({
            "timestamp": [base + pd.Timedelta(minutes=i) for i in range(len(pxs))],
            "call_px": [float(x) for x in pxs],
            "put_px": [0.0] * len(pxs),
        })
        self.ticker = ticker
        self.start_dt = self.df["timestamp"].iloc[0]
        self.end_dt = self.df["timestamp"].iloc[-1]
        self.call_entry_premium = float(entry_call)
        self.put_entry_premium = 0.0
        self.invest_call = float(invest_call)
        self.invest_put = 0.0
        self.refuerzo = None
        self.exit_reason = "session_end"
        for _f in ("call_exit_premium", "call_exit_idx", "call_exit_reason",
                   "put_exit_premium", "put_exit_idx", "put_exit_reason"):
            setattr(self, _f, None)
        self.final_total = self.max_total = self.min_total = 0.0


def _res(its):
    return [{"fecha": "2026-04-27", "ticker": it.ticker, "iteration": it} for it in its]


def _spy(pxs):
    return MockIt("SPY", pxs, 1.0, 500)   # gain($) = 500·(px−1)/1


def _qqq(pxs):
    return MockIt("QQQ", pxs, 2.0, 500)   # gain($) = 500·(px−2)/2


def test_stop_dispara_con_mas_de_un_ticker():
    # ROI de cartera: [0, −10%, −20%, −15%] → stop=−15% dispara en idx2.
    spy = _spy([1.0, 0.9, 0.8, 0.85])
    qqq = _qqq([2.0, 1.8, 1.6, 1.7])
    n = apply_collective_exit(_res([spy, qqq]), profit_frac=None, stop_frac=-0.15, stop_require_multi=True)
    assert n == 2
    assert spy.exit_reason == "collective_stop" and qqq.exit_reason == "collective_stop"
    assert len(spy.df) == 3   # truncado en idx2 (3 filas)


def test_gate_un_solo_ticker_no_dispara():
    spy = _spy([1.0, 0.9, 0.8, 0.85])
    n = apply_collective_exit(_res([spy]), profit_frac=None, stop_frac=-0.15, stop_require_multi=True)
    assert n == 0 and spy.exit_reason == "session_end"


def test_gana_el_primer_trigger_profit_antes_que_stop():
    # +12% en idx1 y luego −20%: con profit=+5% y stop=−15%, gana el profit (idx1).
    spy = _spy([1.0, 1.12, 0.8, 0.8])
    qqq = _qqq([2.0, 2.24, 1.6, 1.6])
    n = apply_collective_exit(_res([spy, qqq]), profit_frac=0.05, stop_frac=-0.15, stop_require_multi=True)
    assert n == 2 and spy.exit_reason == "collective_roi" and len(spy.df) == 2


def test_require_multi_false_dispara_con_un_ticker():
    spy = _spy([1.0, 0.9, 0.8, 0.85])
    n = apply_collective_exit(_res([spy]), profit_frac=None, stop_frac=-0.15, stop_require_multi=False)
    assert n == 1 and spy.exit_reason == "collective_stop"


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    _tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    _fail = 0
    for _t in _tests:
        try:
            _t()
            print(f"  OK  {_t.__name__}")
        except AssertionError as _e:
            _fail += 1
            print(f"  FALLO {_t.__name__}: {_e}")
    print(f"\n{len(_tests) - _fail}/{len(_tests)} tests OK")
    sys.exit(1 if _fail else 0)

"""Sweep de re-optimización del modo INTRADÍA de TR-UD-15m.

Pregunta: ¿relajar R3 (la compuerta que más recorta) capta más reversiones
SIN romper el PF? Backtest OTM + Fase 2 NBBO honesto, 3 tickers, 6 meses.
Baseline (intradía actual) = PF ~0.82.
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import pandas as pd
from strategies.run_detect import load_15m
from strategies.trend_reversal_bb_15m import TrendReversalBB15m
from signals_backtest import run_one
from strategies.backtest_compare import _dl

TK = ["QQQ", "SPY", "IWM"]
bars = {t: load_15m(t, "2026-01-01", "2026-06-23") for t in TK}
dl = _dl()


def gen(prm):
    return pd.concat([TrendReversalBB15m(prm).detect_signals(bars[t], t) for t in TK],
                     ignore_index=True)


def bt(sigs):
    pnls = []
    for _, r in sigs.iterrows():
        spec = {"ticker": r["ticker"], "fecha": str(r["fecha"]),
                "hora": r["hora_et"], "tipo": r["direccion"]}
        res = run_one(dl, spec, inversion=1000.0, umbral_pct=1000.0, stop_pct=-100.0,
                      selection_criterion="spread", nbbo_timeline=True,
                      entry_at_ask=True, exit_at_bid=True, dte=0)
        if res.get("status") != "ok":
            continue
        pnls.append(float(res["iteration"].gain_total))
    n = len(pnls)
    if n == 0:
        return "n=0"
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), -sum(losses)
    return (f"N={n:4d}  win%={100*len(wins)/n:4.1f}  total=${sum(pnls):>8.0f}  "
            f"avg=${sum(pnls)/n:6.1f}  PF={gp/gl if gl>0 else 999:.2f}")


VAR = [
    ("base (actual)",   dict(intraday=True)),
    ("R3 OFF",          dict(intraday=True, use_r3=False)),
    ("R3 OFF + cd16",   dict(intraday=True, use_r3=False, cooldown_bars=16)),
    ("R3=basis",        dict(intraday=True, r3_mode="basis")),
    ("R3=basis + cd16", dict(intraday=True, r3_mode="basis", cooldown_bars=16)),
    ("R4 OFF",          dict(intraday=True, use_r4=False)),
]
print("variante           backtest (OTM, Fase 2 NBBO, 3 tickers, ene-jun 2026)", flush=True)
print("-" * 78, flush=True)
for name, prm in VAR:
    print(f"{name:18s} {bt(gen(prm))}", flush=True)
print("FIN", flush=True)

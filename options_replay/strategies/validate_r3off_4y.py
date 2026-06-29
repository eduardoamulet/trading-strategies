"""TEST DE FUEGO — valida el ganador del sweep (INTRADÍA R3-OFF) sobre 4 años.

Si el PF aguanta >1.0 sobre 2022-2026 (y año a año), el edge es real;
si se cae <1.0, el 1.06 de 6 meses era ruido. OTM + Fase 2 NBBO honesto, 3 tickers.
Desglose POR AÑO = la prueba de robustez de verdad.
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

from collections import defaultdict
import pandas as pd
from strategies.run_detect import load_15m
from strategies.trend_reversal_bb_15m import TrendReversalBB15m
from signals_backtest import run_one
from strategies.backtest_compare import _dl

TK = ["QQQ", "SPY", "IWM"]
START, END = "2022-06-01", "2026-06-23"
PRM = dict(intraday=True, use_r3=False)   # el ganador del sweep

bars = {t: load_15m(t, START, END) for t in TK}
dl = _dl()
sigs = pd.concat([TrendReversalBB15m(PRM).detect_signals(bars[t], t) for t in TK],
                 ignore_index=True).sort_values(["fecha", "hora_et"]).reset_index(drop=True)
print(f"R3-OFF intradía 4y: {len(sigs)} señales  {START}..{END}", flush=True)


def stats(p):
    if not p:
        return "n=0"
    w = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    gp, gl = sum(w), -sum(losses)
    return (f"N={len(p):4d}  win%={100*len(w)/len(p):4.1f}  total=${sum(p):>9.0f}  "
            f"PF={gp/gl if gl > 0 else 999:.2f}")


by_year = defaultdict(list)
allp = []
for i, (_, r) in enumerate(sigs.iterrows(), 1):
    spec = {"ticker": r["ticker"], "fecha": str(r["fecha"]),
            "hora": r["hora_et"], "tipo": r["direccion"]}
    res = run_one(dl, spec, inversion=1000.0, umbral_pct=1000.0, stop_pct=-100.0,
                  selection_criterion="spread", nbbo_timeline=True,
                  entry_at_ask=True, exit_at_bid=True, dte=0)
    if res.get("status") != "ok":
        continue
    g = float(res["iteration"].gain_total)
    allp.append(g)
    by_year[str(r["fecha"])[:4]].append(g)
    if i % 250 == 0:
        print(f"  ...{i}/{len(sigs)}  {stats(allp)}", flush=True)

print("\n=== R3-OFF 4 AÑOS (intradía, OTM, Fase 2 NBBO, 3 tickers) ===", flush=True)
print(f"GLOBAL : {stats(allp)}", flush=True)
for y in sorted(by_year):
    print(f"  {y} : {stats(by_year[y])}", flush=True)
print("FIN", flush=True)

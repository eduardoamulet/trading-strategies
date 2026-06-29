"""Validación del FLIP sobre 4 años (apertura, 3 tickers): ¿dar vuelta a la opuesta
cuando la vela de confirmación va en contra rinde más que solo CORTAR?

3 estrategias por señal: SIN filtro (corre al stop) · CORTAR (sale 9:45) · FLIP (entra la
opuesta a 9:45 si la confirmación falla). Guardado INCREMENTAL a CSV (sobrevive a un crash).
Desglose por año + concentración (¿lo domina 1 trade afortunado?). OTM + Fase 2 NBBO honesto.
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import csv
from datetime import time as T
import pandas as pd
from strategies.run_detect import load_15m
from strategies.trend_reversal_bb_15m import TrendReversalBB15m
from strategies.backtest_compare import _dl
import signals_backtest as sbt

TK = ["QQQ", "SPY", "IWM"]
START, END = "2022-06-01", "2026-06-23"
OUT = HERE.parent / "data" / "flip_validation_4y.csv"
KW = dict(inversion=1000.0, umbral_pct=1000.0, stop_pct=-100.0, selection_criterion="spread",
          nbbo_timeline=True, entry_at_ask=True, exit_at_bid=True, dte=0)
dl = _dl()


def confirmed(tk, s):
    und = dl.underlying(tk, str(s.fecha))
    o = sbt.to_ts(str(s.fecha), T(9, 30))
    e = o + pd.Timedelta(minutes=15)
    w = und[(und["timestamp"] >= o) & (und["timestamp"] < e)]
    if w.empty:
        return True
    co, cc = float(w.iloc[0]["open"]), float(w.iloc[-1]["close"])
    return (cc > co) if s.direccion == "CALL" else (cc < co)


def bt(tk, tipo, fecha, hora, cf=False):
    r = sbt.run_one(dl, {"ticker": tk, "fecha": fecha, "hora": hora, "tipo": tipo}, **KW, confirm_candle=cf)
    return r["iteration"].gain_total if r["status"] == "ok" else None


sigs = []
for tk in TK:
    b = load_15m(tk, START, END)
    for _, row in TrendReversalBB15m({"intraday": False}).detect_signals(b, tk).iterrows():
        sigs.append((tk, row))
print(f"FLIP 4y: {len(sigs)} señales apertura ({START}..{END})", flush=True)

rows = []
with open(OUT, "w", newline="", encoding="utf-8") as f:
    wtr = csv.writer(f)
    wtr.writerow(["tk", "fecha", "dir", "confirmed", "no_filter", "cut", "flip"])
    for i, (tk, s) in enumerate(sigs, 1):
        fe = str(s.fecha)
        conf = confirmed(tk, s)
        nf = bt(tk, s.direccion, fe, "09:30")
        ct = bt(tk, s.direccion, fe, "09:30", cf=True)
        fl = nf if conf else bt(tk, "PUT" if s.direccion == "CALL" else "CALL", fe, "09:45")
        wtr.writerow([tk, fe, s.direccion, conf, nf, ct, fl])
        f.flush()
        rows.append(dict(tk=tk, fecha=fe, dir=s.direccion, conf=conf, nf=nf, ct=ct, fl=fl))
        if i % 25 == 0:
            print(f"  ...{i}/{len(sigs)}", flush=True)

df = pd.DataFrame(rows)


def stat(col):
    x = df[col].dropna().tolist()
    if not x:
        return "n=0"
    w = [v for v in x if v > 0]
    losses = [v for v in x if v <= 0]
    pf = sum(w) / -sum(losses) if sum(losses) < 0 else 999
    return f"N={len(x):4d} total=${sum(x):+9.0f} win%={100*len(w)/len(x):4.0f} PF={pf:.2f}"


print("\n=== 4 AÑOS (apertura, 3 tickers, OTM Fase 2) ===", flush=True)
print(f"  SIN filtro: {stat('nf')}", flush=True)
print(f"  CORTAR:     {stat('ct')}", flush=True)
print(f"  FLIP:       {stat('fl')}", flush=True)
df["yr"] = df.fecha.str[:4]
print("\nFLIP por año:", flush=True)
for y in sorted(df.yr.unique()):
    sub = df[df.yr == y].fl.dropna().tolist()
    w = [v for v in sub if v > 0]
    ll = [v for v in sub if v <= 0]
    pf = sum(w) / -sum(ll) if sum(ll) < 0 else 999
    print(f"  {y}: N={len(sub):3d} total=${sum(sub):+8.0f} PF={pf:.2f}", flush=True)
fl = sorted(df.fl.dropna().tolist(), reverse=True)
if fl and sum(fl) != 0:
    print(f"\nConcentración FLIP: mejor trade=${fl[0]:+.0f} ({fl[0]/sum(fl)*100:.0f}% del total) · "
          f"top-3=${sum(fl[:3]):+.0f} ({sum(fl[:3])/sum(fl)*100:.0f}%)", flush=True)
print("FIN", flush=True)

"""Completa la validación del FLIP con IWM (resumible) y agrega los 3 tickers.

Lee el CSV existente (QQQ+SPY ya hechos), corre SOLO las señales de IWM que faltan,
las appendea, y al final imprime la agregación completa de los 3 tickers.
"""
import sys
import csv
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import time as T
import pandas as pd
from strategies.run_detect import load_15m
from strategies.trend_reversal_bb_15m import TrendReversalBB15m
from strategies.backtest_compare import _dl
import signals_backtest as sbt

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


done = set()
if OUT.exists():
    ex = pd.read_csv(OUT)
    done = {(r.tk, str(r.fecha), r.dir) for r in ex.itertuples()}
print(f"CSV existente: {len(done)} señales ya hechas", flush=True)

b = load_15m("IWM", "2022-06-01", "2026-06-23")
sigs = TrendReversalBB15m({"intraday": False}).detect_signals(b, "IWM")
todo = [s for _, s in sigs.iterrows() if ("IWM", str(s.fecha), s.direccion) not in done]
print(f"IWM: {len(sigs)} señales, faltan {len(todo)}", flush=True)

with open(OUT, "a", newline="", encoding="utf-8") as f:
    wtr = csv.writer(f)
    for i, s in enumerate(todo, 1):
        fe = str(s.fecha)
        conf = confirmed("IWM", s)
        nf = bt("IWM", s.direccion, fe, "09:30")
        ct = bt("IWM", s.direccion, fe, "09:30", cf=True)
        fl = nf if conf else bt("IWM", "PUT" if s.direccion == "CALL" else "CALL", fe, "09:45")
        wtr.writerow(["IWM", fe, s.direccion, conf, nf, ct, fl])
        f.flush()
        if i % 25 == 0:
            print(f"  ...IWM {i}/{len(todo)}", flush=True)

df = pd.read_csv(OUT).drop_duplicates(["tk", "fecha", "dir"], keep="first")
df["yr"] = df.fecha.astype(str).str[:4]


def stat(col):
    x = df[col].dropna().tolist()
    w = [v for v in x if v > 0]
    losses = [v for v in x if v <= 0]
    pf = sum(w) / -sum(losses) if sum(losses) < 0 else 999
    return f"N={len(x):4d}  total=${sum(x):+9.0f}  win%={100*len(w)/len(x):4.0f}  PF={pf:.2f}"


print(f"\n=== FLIP 4 AÑOS COMPLETO ({len(df)} señales, 3 tickers: {dict(df.tk.value_counts())}) ===", flush=True)
print(f"  SIN filtro: {stat('no_filter')}", flush=True)
print(f"  CORTAR:     {stat('cut')}", flush=True)
print(f"  FLIP:       {stat('flip')}", flush=True)
print("\nFLIP por año:", flush=True)
for y in sorted(df.yr.unique()):
    s = df[df.yr == y].flip.dropna().tolist()
    w = [v for v in s if v > 0]
    ll = [v for v in s if v <= 0]
    pf = sum(w) / -sum(ll) if sum(ll) < 0 else 999
    print(f"  {y}: N={len(s):3d}  total=${sum(s):+8.0f}  PF={pf:.2f}", flush=True)
fl = sorted(df.flip.dropna().tolist(), reverse=True)
print(f"\nConcentración FLIP: mejor=${fl[0]:+.0f} ({fl[0]/sum(fl)*100:.0f}%) · top-3=${sum(fl[:3]):+.0f} ({sum(fl[:3])/sum(fl)*100:.0f}%)", flush=True)
print("FIN", flush=True)

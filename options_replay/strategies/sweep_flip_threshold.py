"""Sweep del UMBRAL del flip (dead-zone) sobre 2022-26 · QQQ/SPY/IWM.

Por cada señal de APERTURA calcula:
  - body_dir: cuerpo de la vela de confirmación 09:30→09:45 (% en la dirección de la señal).
  - keep:  mantener la señal original, correr al cierre.
  - cut:   cortar a 09:45 (vender la pierna original).
  - flip:  reemplazar por la pierna OPUESTA a 09:45, correr al cierre.

Luego barre el umbral T: por señal → keep si body>=T · flip si body<-T · cortar si -T<=body<T.
Reporta total / win% / PF de cada T. Resumible (CSV incremental) y paralelizado.
"""
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import time as T
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd  # noqa: E402

from strategies.run_detect import load_15m  # noqa: E402
from strategies.trend_reversal_bb_15m import TrendReversalBB15m  # noqa: E402
from strategies.backtest_compare import _dl  # noqa: E402
import signals_backtest as sbt  # noqa: E402

OUT = HERE.parent / "data" / "flip_threshold_sweep.csv"
KW = dict(inversion=1000.0, umbral_pct=1000.0, stop_pct=-100.0, selection_criterion="spread",
          nbbo_timeline=True, entry_at_ask=True, exit_at_bid=True, dte=0)
TICKERS = ["QQQ", "SPY", "IWM"]
START, END = "2022-06-01", "2026-06-23"
THRESHOLDS = [0.0, 0.05, 0.15, 0.30, 0.50]
dl = _dl()


def body_dir(tk, fecha, direccion):
    """Cuerpo de la vela 09:30→09:45 (% en la dirección de la señal). >0 = a favor."""
    und = dl.underlying(tk, str(fecha))
    o = sbt.to_ts(str(fecha), T(9, 30))
    e = o + pd.Timedelta(minutes=15)
    w = und[(und["timestamp"] >= o) & (und["timestamp"] < e)]
    if w.empty:
        return 0.0
    co, cc = float(w.iloc[0]["open"]), float(w.iloc[-1]["close"])
    b = (cc - co) / co * 100.0 if co else 0.0
    return b if direccion == "CALL" else -b


def bt(tk, tipo, fecha, hora, exit_hora="16:00"):
    r = sbt.run_one(dl, {"ticker": tk, "fecha": fecha, "hora": hora, "tipo": tipo},
                    **KW, exit_hora=exit_hora)
    return r["iteration"].gain_total if r["status"] == "ok" else None


def one(tk, s):
    fe = str(s.fecha)
    d = s.direccion
    opp = "PUT" if d == "CALL" else "CALL"
    return (tk, fe, d, body_dir(tk, fe, d),
            bt(tk, d, fe, "09:30"),                       # keep (al cierre)
            bt(tk, d, fe, "09:30", exit_hora="09:45"),    # cut (a 09:45)
            bt(tk, opp, fe, "09:45"))                     # flip (opuesta a 09:45 → cierre)


# --- resumible ---
done = set()
if OUT.exists():
    for r in pd.read_csv(OUT).itertuples():
        done.add((r.tk, str(r.fecha), r.dir))

todo = []
for tk in TICKERS:
    b = load_15m(tk, START, END)
    ss = TrendReversalBB15m({"intraday": False}).detect_signals(b, tk)
    for _, s in ss.iterrows():
        if (tk, str(s.fecha), s.direccion) not in done:
            todo.append((tk, s))
print(f"{len(todo)} señales por correr ({len(done)} ya hechas)", flush=True)

with open(OUT, "a", newline="", encoding="utf-8") as f:
    wtr = csv.writer(f)
    if not done:
        wtr.writerow(["tk", "fecha", "dir", "body", "keep", "cut", "flip"])
        f.flush()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(one, tk, s) for tk, s in todo]
        n = 0
        for fut in as_completed(futs):
            try:
                wtr.writerow(fut.result())
                f.flush()
            except Exception:
                pass
            n += 1
            if n % 25 == 0:
                print(f"  ...{n}/{len(todo)}", flush=True)

# --- SWEEP ---
df = pd.read_csv(OUT).dropna(subset=["keep", "cut", "flip"])


def stat(vals):
    w = [v for v in vals if v > 0]
    losses = [v for v in vals if v <= 0]
    pf = sum(w) / -sum(losses) if sum(losses) < 0 else 999.0
    win = 100.0 * len(w) / len(vals) if vals else 0.0
    return sum(vals), win, pf


print(f"\n=== SWEEP UMBRAL FLIP · {len(df)} señales · {TICKERS} · {START}..{END} ===", flush=True)
print(f"{'umbral':>7} | {'total $':>10} | {'win%':>5} | {'PF':>5} | flips/cuts/keeps", flush=True)
for thr in THRESHOLDS:
    outc, nf, nc, nk = [], 0, 0, 0
    for r in df.itertuples():
        if r.body >= thr:
            outc.append(r.keep); nk += 1
        elif r.body < -thr:
            outc.append(r.flip); nf += 1
        else:
            outc.append(r.cut); nc += 1
    tot, win, pf = stat(outc)
    print(f"{thr:>7.2f} | {tot:>+10.0f} | {win:>4.0f}% | {pf:>5.2f} | {nf}/{nc}/{nk}", flush=True)

# Referencias (sanity vs validación previa: keep~1.27, cut~1.46, flip T0~1.59)
kt, kw, kpf = stat(df.keep.tolist())
# CORTAR (confirm filter, sin flip): keep si body>0, cortar si body<=0
cut_strat = [(r.keep if r.body > 0 else r.cut) for r in df.itertuples()]
ct_, cw, cpf = stat(cut_strat)
print("\nReferencias:", flush=True)
print(f"  KEEP siempre (sin filtro): total={kt:>+9.0f}  win%={kw:.0f}  PF={kpf:.2f}", flush=True)
print(f"  CORTAR (confirm, sin flip): total={ct_:>+9.0f}  win%={cw:.0f}  PF={cpf:.2f}", flush=True)
print("FIN", flush=True)

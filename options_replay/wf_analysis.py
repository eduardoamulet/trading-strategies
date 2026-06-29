"""wf_analysis.py — análisis WALK-FORWARD del CSV de 4 años (walkforward_days.csv).

  1) Por AÑO (régimen): baseline (todos) vs FILTRO FIJO (banda gap 0.18–0.78% + sin Lun/Vie).
     → ¿el edge aguanta en bear 2022 / 2023 / bull 2024-25 / reciente?
  2) Walk-forward expandible, REGLA FIJA: la regla YA elegida aplicada a cada semestre sucesivo
     (out-of-sample puro: la regla se fijó con 2026, acá la corremos sobre 2022-2025).
  3) Walk-forward expandible, RE-DERIVANDO la banda: en cada fold train=pasado → banda = [p25,p75]
     del |gap| del train (los gaps 'moderados'), test=semestre siguiente. ¿El MÉTODO generaliza?

Uso (desde options_replay/):  py wf_analysis.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

df = pd.read_csv(HERE / "data" / "walkforward_days.csv")
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").reset_index(drop=True)
df["half"] = df["date"].dt.year.astype(str) + "-H" + ((df["date"].dt.month > 6).astype(int) + 1).astype(str)


def ex(sub):
    n = len(sub)
    return (n, sub["gain"].sum(), sub["win"].mean() * 100 if n else 0, sub["roi"].mean() if n else 0)


def fixed_mask(d):
    return (d["abs_gap"] >= 0.18) & (d["abs_gap"] <= 0.78) & (~d["dow"].isin(["Lun", "Vie"]))


print(f"== Walk-forward · {len(df)} día-ticker · {df['date'].min().date()} → {df['date'].max().date()} ==\n")

print("─── 1) POR AÑO (régimen): baseline vs filtro fijo ───")
print(f"  {'año':<6} {'n':>4} {'base $':>10} {'base win':>8}   {'filtro n':>8} {'filtro $':>10} {'win':>5} {'roi/d':>7}")
for yr, g in df.groupby(df["date"].dt.year):
    bn, bt, bw, _ = ex(g)
    f = g[fixed_mask(g)]
    fn, ft, fw, fr = ex(f)
    flag = "✅" if ft > 0 else "❌"
    print(f"  {yr:<6} {bn:>4} ${bt:>+9,.0f} {bw:>7.0f}%   {fn:>8} ${ft:>+9,.0f} {fw:>4.0f}% {fr:>+6.1f}% {flag}")

print("\n─── 2) WALK-FORWARD regla FIJA (banda 0.18–0.78 + sin Lun/Vie), por semestre ───")
print(f"  {'semestre':<9} {'n':>4} {'base $':>10}   {'filtro n':>8} {'filtro $':>10} {'win':>5} {'roi/d':>7}")
wins = 0; tot_f = 0
halves = sorted(df["half"].unique())
for h in halves:
    g = df[df["half"] == h]
    bn, bt, _, _ = ex(g)
    f = g[fixed_mask(g)]
    fn, ft, fw, fr = ex(f)
    tot_f += ft; wins += (ft > 0)
    print(f"  {h:<9} {bn:>4} ${bt:>+9,.0f}   {fn:>8} ${ft:>+9,.0f} {fw:>4.0f}% {fr:>+6.1f}%  {'✅' if ft>0 else '❌'}")
print(f"  → semestres con filtro POSITIVO: {wins}/{len(halves)} · total filtro acumulado: ${tot_f:+,.0f}")

print("\n─── 3) WALK-FORWARD RE-DERIVANDO la banda [p25,p75] del |gap| (train=pasado) ───")
print(f"  {'test sem':<9} {'banda derivada':>16} {'n':>4} {'filtro $':>10} {'win':>5} {'roi/d':>7}  vs base")
wins2 = 0; tested = 0; tot2 = 0
for i, h in enumerate(halves):
    if i == 0:
        continue  # sin train previo
    train = df[df["date"] < df[df["half"] == h]["date"].min()]
    test = df[df["half"] == h]
    if len(train) < 60 or test.empty:
        continue
    lo, hi = np.percentile(train["abs_gap"].dropna(), [25, 75])
    m = (test["abs_gap"] >= lo) & (test["abs_gap"] <= hi) & (~test["dow"].isin(["Lun", "Vie"]))
    f = test[m]
    fn, ft, fw, fr = ex(f)
    _, bt, _, br = ex(test)
    tested += 1; wins2 += (ft > 0); tot2 += ft
    print(f"  {h:<9} [{lo:>5.2f},{hi:>5.2f}]   {fn:>4} ${ft:>+9,.0f} {fw:>4.0f}% {fr:>+6.1f}%  base roi {br:+.1f}% {'✅' if fr>br else '·'}")
print(f"  → folds con filtro POSITIVO: {wins2}/{tested} · total: ${tot2:+,.0f}")

# Pooled fijo
f_all = df[fixed_mask(df)]
n, t, w, r = ex(f_all)
bn, bt, bw, br = ex(df)
print(f"\n─── POOLED 4 años ───")
print(f"  Baseline: n={bn} ${bt:+,.0f} win={bw:.0f}% roi={br:+.1f}%/d")
print(f"  Filtro  : n={n} ${t:+,.0f} win={w:.0f}% roi={r:+.1f}%/d  (capturó {n/bn*100:.0f}% de los días)")

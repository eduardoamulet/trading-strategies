"""pattern_finder.py — ¿CUÁNDO conviene entrar con 'CALL y PUT (Refuerzo)'? (no a ciegas)

Mide, día por día (QQQ+SPY+IWM, Fase 2), el P&L de la estrategia straddle+refuerzo y lo cruza
con features PRE-ENTRADA (conocibles ANTES de comprometer capital, sin look-ahead):
  · abs_gap   = |open 09:30 − cierre previo| / cierre previo  (gap de apertura)
  · prev_rng  = (high − low del día previo) / cierre previo    (volatilidad que se 'pega')
  · or5       = rango 09:30–09:34 / open                       (expansión de apertura; ⚠ exige entrar 09:35)
  · dow       = día de la semana
Busca la CONDICIÓN bajo la cual la EXPECTATIVA se vuelve positiva, con split TRAIN/TEST para no
auto-engañarnos (un filtro que sólo funciona in-sample no sirve).

Config analizada: Refuerzo×2 · umbral 10% · stop −80% (la 'sana' del sweep: cat=0; umbral ALTO
para CAPTURAR el movimiento los días que filtramos — un umbral 2% cortaría la ganancia enseguida).

Uso (desde options_replay/):  py pattern_finder.py
"""
from __future__ import annotations

import sys
import time
from datetime import time as _time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import numpy as np
import pandas as pd

import config
import signals_backtest as sbt
from adapter_polygon import PolygonAdapter
from downloader import Downloader

TICKERS = ["QQQ", "SPY", "IWM"]
DATE_START, DATE_END = "2026-01-01", "2026-06-18"
TRAIN_END = "2026-04-30"          # train = ene–abr · test = may–jun
ENTRY, SEARCH_WIN = "09:30", 4.0
INVERSION, CALL_PCT = 1000.0, 50.0
REFUERZO_MAX, REFUERZO_LOSS, UMBRAL, STOP = 2, 0.50, 10, -80
_DOW = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _day_stats(df: pd.DataFrame) -> dict | None:
    """open 09:30, high/low/last de la sesión regular (09:30–16:00) y rango de apertura 5min."""
    if df is None or df.empty:
        return None
    t = df["timestamp"].dt.time
    reg = df[(t >= _time(9, 30)) & (t < _time(16, 0))]
    if reg.empty:
        return None
    open0930 = float(reg.iloc[0]["open"])
    hi, lo = float(reg["high"].max()), float(reg["low"].min())
    last = float(reg.iloc[-1]["close"])
    tt = reg["timestamp"].dt.time
    or5 = reg[(tt >= _time(9, 30)) & (tt <= _time(9, 34))]
    or5_rng = (float(or5["high"].max()) - float(or5["low"].min())) if not or5.empty else np.nan
    return {"open": open0930, "high": hi, "low": lo, "last": last, "or5_rng": or5_rng}


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=600), HERE / "data")
    dl.resolution = "1min"
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(DATE_START, DATE_END)]
    print(f"== Patrón de entrada · Refuerzo×{REFUERZO_MAX} um{UMBRAL} stop{STOP} · "
          f"{','.join(TICKERS)} · {len(days)} días háb · Fase 2 ==", flush=True)
    t0 = time.time()
    recs = []
    for tk in TICKERS:
        prev = None  # stats del día previo con datos
        for d in days:
            try:
                udf = dl.underlying(tk, d)
            except Exception:
                udf = None
            st = _day_stats(udf)
            if st is None:
                continue
            gap = abs_gap = prev_rng = np.nan
            if prev is not None and prev["last"]:
                gap = (st["open"] - prev["last"]) / prev["last"] * 100.0
                abs_gap = abs(gap)
                prev_rng = (prev["high"] - prev["low"]) / prev["last"] * 100.0
            or5 = (st["or5_rng"] / st["open"] * 100.0) if st["open"] and not np.isnan(st["or5_rng"]) else np.nan
            prev = st  # actualizar para el próximo día
            if np.isnan(abs_gap):
                continue  # 1er día del ticker: sin gap → fuera
            r = sbt.run_one(
                dl, {"ticker": tk, "fecha": d, "hora": ENTRY, "tipo": "CALL y PUT (Refuerzo)"},
                inversion=INVERSION, umbral_pct=UMBRAL, stop_pct=STOP,
                entry_at_ask=True, exit_at_bid=True, nbbo_timeline=True,
                selection_criterion="spread", call_pct=CALL_PCT,
                refuerzo_loss_pct=REFUERZO_LOSS, refuerzo_max=REFUERZO_MAX,
                search_window_min=SEARCH_WIN)
            if r["status"] != "ok":
                continue
            it = r["iteration"]
            inv = it.invest_total
            roi = (it.gain_total / inv * 100.0) if inv else 0.0
            recs.append({"ticker": tk, "date": d, "dow": _DOW[pd.Timestamp(d).dayofweek],
                         "gain": it.gain_total, "roi": roi, "win": int(it.gain_total > 0),
                         "abs_gap": abs_gap, "gap": gap, "prev_rng": prev_rng, "or5": or5})
        print(f"  {tk}: {sum(1 for x in recs if x['ticker']==tk)} días", flush=True)

    df = pd.DataFrame(recs)
    if df.empty:
        print("Sin datos.", flush=True)
        return
    df.to_csv(HERE / "data" / "pattern_days.csv", index=False)
    print(f"\n== {len(df)} día-ticker en {(time.time()-t0)/60:.1f} min → data/pattern_days.csv ==", flush=True)

    def _exp(sub):
        n = len(sub)
        return dict(n=n, total=sub["gain"].sum(), win=sub["win"].mean()*100 if n else 0,
                    mean_roi=sub["roi"].mean() if n else 0)

    base = _exp(df)
    print(f"\nBASELINE (todos): n={base['n']} total=${base['total']:+,.0f} "
          f"win={base['win']:.0f}% ROI medio={base['mean_roi']:+.1f}%/día", flush=True)

    # 1) Cuartiles por cada feature continua → ¿la expectativa sube con el movimiento?
    for col, lbl in (("abs_gap", "Gap apertura |%|"), ("prev_rng", "Rango día previo %"),
                     ("or5", "Rango apertura 5min % (⚠ exige 09:35)")):
        sub = df.dropna(subset=[col])
        if sub.empty:
            continue
        q = pd.qcut(sub[col], 4, labels=["Q1 bajo", "Q2", "Q3", "Q4 alto"], duplicates="drop")
        print(f"\n── {lbl} (cuartiles) ──", flush=True)
        print(f"  {'cuartil':<9} {'rango':>16} {'n':>4} {'total':>10} {'win':>5} {'ROI/día':>8}", flush=True)
        for name, g in sub.groupby(q, observed=True):
            e = _exp(g)
            rng = f"[{g[col].min():.2f},{g[col].max():.2f}]"
            print(f"  {str(name):<9} {rng:>16} {e['n']:>4} ${e['total']:>+9,.0f} "
                  f"{e['win']:>4.0f}% {e['mean_roi']:>+7.1f}%", flush=True)

    # 2) Día de la semana
    print(f"\n── Día de la semana ──", flush=True)
    print(f"  {'dow':<5} {'n':>4} {'total':>10} {'win':>5} {'ROI/día':>8}", flush=True)
    for name in ["Lun", "Mar", "Mié", "Jue", "Vie"]:
        g = df[df["dow"] == name]
        if g.empty:
            continue
        e = _exp(g)
        print(f"  {name:<5} {e['n']:>4} ${e['total']:>+9,.0f} {e['win']:>4.0f}% {e['mean_roi']:>+7.1f}%", flush=True)

    # 3) TRAIN/TEST: elegir umbral del mejor feature en train, validar en test (honestidad)
    tr, te = df[df["date"] <= TRAIN_END], df[df["date"] > TRAIN_END]
    print(f"\n════════ TRAIN/TEST (train≤{TRAIN_END}: {len(tr)} · test: {len(te)}) ════════", flush=True)
    print(f"  test BASELINE (sin filtro): total=${te['gain'].sum():+,.0f} "
          f"win={te['win'].mean()*100:.0f}% ROI={te['roi'].mean():+.1f}%/día", flush=True)
    for col, lbl in (("abs_gap", "Gap |%|"), ("prev_rng", "Rango previo %"), ("or5", "OR5 %")):
        s_tr, s_te = tr.dropna(subset=[col]), te.dropna(subset=[col])
        if len(s_tr) < 20 or s_te.empty:
            continue
        # umbral candidato = percentiles 50..90 sobre TRAIN; elijo el de mayor ROI medio con n≥15
        best = None
        for p in [50, 60, 65, 70, 75, 80, 85, 90]:
            thr = np.percentile(s_tr[col], p)
            sub = s_tr[s_tr[col] >= thr]
            if len(sub) < 15:
                continue
            sc = sub["roi"].mean()
            if best is None or sc > best[1]:
                best = (thr, sc, p)
        if best is None:
            continue
        thr = best[0]
        ap = s_te[s_te[col] >= thr]
        if ap.empty:
            continue
        print(f"\n  Filtro «{lbl} ≥ {thr:.2f}» (p{best[2]} de train):", flush=True)
        print(f"    TRAIN: n={len(s_tr[s_tr[col]>=thr])} total=${s_tr[s_tr[col]>=thr]['gain'].sum():+,.0f} "
              f"win={s_tr[s_tr[col]>=thr]['win'].mean()*100:.0f}% ROI={best[1]:+.1f}%/día", flush=True)
        print(f"    TEST : n={len(ap)} total=${ap['gain'].sum():+,.0f} "
              f"win={ap['win'].mean()*100:.0f}% ROI={ap['roi'].mean():+.1f}%/día "
              f"{'✅ generaliza' if ap['roi'].mean() > te['roi'].mean() else '❌ no mejora baseline'}", flush=True)

    print(f"\n== listo ==", flush=True)


if __name__ == "__main__":
    main()

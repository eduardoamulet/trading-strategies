"""Test DIRECCIONAL: ¿las señales (CALL/PUT) tienen edge que el straddle ciego no tiene?

Toma las señales de Investep (Historial de Señales) que caen sobre tickers con NBBO cacheado,
y las backtestea de DOS formas sobre los MISMOS días, en Fase 2:
  A) DIRECCIONAL — señal CALL → compra Sólo CALL · señal PUT → Sólo PUT.
  B) STRADDLE ciego — CALL y PUT en toda señal (el baseline que ya sabemos que pierde).
Si (A) gana donde (B) pierde → la DIRECCIÓN de la señal aporta edge. Sweep umbral × stop.

Uso (desde options_replay/):  py directional_test.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import pandas as pd

import config
import analytics
import signals_db as db
import signals_backtest as sbt
from adapter_polygon import PolygonAdapter
from downloader import Downloader

# Tickers con NBBO cacheado (los del backfill). Solo estos son backtesteables offline/honesto.
CACHED = {"QQQ", "SPY", "IWM", "NVDA", "TSLA", "PLTR", "AMZN", "META", "MSFT", "GOOG", "AAPL"}
INVERSION = 1000.0
GRID_UMBRAL = [2, 5, 10]
GRID_STOP = [-50, -80, -100]
FASE2 = True


def _pf(pf):
    return 9.99 if pf == float("inf") else round(pf, 2)


def _metrics(rows):
    m = analytics.backtest_risk_metrics(rows)
    if m.get("n", 0) == 0:
        return None
    inv = sum(x["invest"] for x in rows)
    wins = sum(1 for x in rows if x["gain"] > 0)
    losses = sum(1 for x in rows if x["gain"] < 0)
    return {"n": m["n"], "total": round(m["total_gain"], 0),
            "roi_pct": round(m["total_gain"] / inv * 100, 1) if inv else 0.0,
            "pf": _pf(m["profit_factor"]),
            "win": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0,
            "maxdd": round(m["max_drawdown"], 0), "cat": m["n_catastrophic"],
            "sortino": round(m["sortino"], 2)}


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    d = db.load_signals()
    d = d[d["symbol"].str.upper().isin(CACHED)].copy()
    d["tipo_u"] = d["tipo"].str.upper().str.strip()
    d = d[d["tipo_u"].isin(["CALL", "PUT"])]
    # hora válida HH:MM
    d["hora"] = d["hora"].astype(str).str[:5]
    sigs = d[["symbol", "fecha", "hora", "tipo_u", "estrategia"]].dropna().reset_index(drop=True)
    print(f"== Test DIRECCIONAL · {len(sigs)} señales Investep sobre tickers cacheados ==", flush=True)
    print("  por estrategia:", d.groupby("estrategia").size().to_dict(), flush=True)
    print("  por tipo:", d.groupby("tipo_u").size().to_dict(), flush=True)
    if sigs.empty:
        print("Sin señales backtesteables.", flush=True)
        return

    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=600), HERE / "data")
    dl.resolution = "1min"
    t0 = time.time()

    def _run_set(tipo_fn, um, stp):
        """tipo_fn(señal_dir) → tipo de operación. Devuelve rows ok."""
        rows = []
        for _, s in sigs.iterrows():
            r = sbt.run_one(
                dl, {"ticker": s["symbol"], "fecha": s["fecha"], "hora": s["hora"],
                     "tipo": tipo_fn(s["tipo_u"])},
                inversion=INVERSION, umbral_pct=um, stop_pct=stp,
                entry_at_ask=FASE2, exit_at_bid=FASE2, nbbo_timeline=FASE2,
                selection_criterion="spread", search_window_min=4.0)
            if r["status"] == "ok":
                it = r["iteration"]
                inv = it.invest_total
                rows.append({"fecha": s["fecha"], "gain": it.gain_total, "invest": inv,
                             "roi": (it.gain_total / inv) if inv else 0.0})
        return rows

    print(f"\n  {'combo':<22} {'modo':<12} {'n':>3} {'total':>9} {'roi':>6} {'PF':>5} "
          f"{'win':>6} {'maxDD':>9} {'cat':>4} {'Sort':>5}", flush=True)
    out = []
    for um in GRID_UMBRAL:
        for stp in GRID_STOP:
            dirr = _metrics(_run_set(lambda t: ("CALL" if t == "CALL" else "PUT"), um, stp))
            strad = _metrics(_run_set(lambda t: "CALL y PUT", um, stp))
            for modo, mm in (("DIRECCIONAL", dirr), ("straddle", strad)):
                if mm is None:
                    continue
                out.append({"um": um, "stop": stp, "modo": modo, **mm})
                print(f"  um{um}·stop{stp:<8} {modo:<12} {mm['n']:>3} ${mm['total']:>+8,.0f} "
                      f"{mm['roi_pct']:>+5.1f}% {mm['pf']:>5} {mm['win']:>5.1f}% "
                      f"${mm['maxdd']:>+8,.0f} {mm['cat']:>4} {mm['sortino']:>5}", flush=True)

    print(f"\n== listo en {(time.time()-t0)/60:.1f} min ==", flush=True)
    dirs = [r for r in out if r["modo"] == "DIRECCIONAL"]
    if dirs:
        best = max(dirs, key=lambda r: r["total"])
        strad_match = next((r for r in out if r["modo"] == "straddle"
                            and r["um"] == best["um"] and r["stop"] == best["stop"]), None)
        print(f"\nMejor DIRECCIONAL: um{best['um']} stop{best['stop']} → "
              f"${best['total']:+,.0f} (PF {best['pf']}, win {best['win']}%, cat {best['cat']})", flush=True)
        if strad_match:
            print(f"  mismo combo como STRADDLE: ${strad_match['total']:+,.0f} (PF {strad_match['pf']})", flush=True)
            _delta = best["total"] - strad_match["total"]
            print(f"  → la DIRECCIÓN {'APORTA' if _delta>0 else 'NO aporta'} edge: "
                  f"{_delta:+,.0f} vs straddle", flush=True)


if __name__ == "__main__":
    main()

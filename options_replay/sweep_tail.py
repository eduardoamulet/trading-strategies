"""Sweep de COLA (martingala): ¿qué config (stop × refuerzos × umbral) MINIMIZA el drawdown
y los días catastróficos (≤−90%) manteniendo PF>1 y total>0? Combinado SPY+IWM, Fase 2.

El stop es la palanca clave para acotar la cola; los refuerzos la agrandan. Reusa
signals_backtest.run_one + analytics.backtest_risk_metrics. Combos en el loop EXTERNO y días
en el INTERNO → los quotes de entrada se cachean en el 1er combo y se reusan en el resto.

Uso (desde options_replay/):  py sweep_tail.py
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import pandas as pd

import config
import analytics
import signals_backtest as sbt
from adapter_polygon import PolygonAdapter
from downloader import Downloader

TICKERS = ["SPY", "IWM"]
ENTRY = "09:30"
INVERSION = 1000.0
DATE_START, DATE_END = "2026-01-01", "2026-06-18"
GRID_REFUERZO_MAX = [0, 2, 4]               # 0 = straddle simple (sin martingala)
GRID_UMBRAL = [2, 10]                        # 2% = el del usuario (win rate alto); 10% más sano
GRID_STOP = [-100, -50, -40, -30, -20]       # -100 = sin stop (cola enorme); más apretado = menos cola
REFUERZO_LOSS = 0.50
FASE2 = True


def _pf(pf):
    return 9.99 if pf == float("inf") else pf


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=600), HERE / "data")
    dl.resolution = "1min"
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(DATE_START, DATE_END)]
    n_combos = len(GRID_REFUERZO_MAX) * len(GRID_UMBRAL) * len(GRID_STOP)
    print(f"== Sweep COLA {','.join(TICKERS)} · {DATE_START}→{DATE_END} · {len(days)} días háb · "
          f"Fase 2 · {n_combos} combos ==", flush=True)
    t0 = time.time()
    out = []
    for rm in GRID_REFUERZO_MAX:
        for um in GRID_UMBRAL:
            for stp in GRID_STOP:
                rows = []
                for tk in TICKERS:
                    for d in days:
                        r = sbt.run_one(
                            dl, {"ticker": tk, "fecha": d, "hora": ENTRY,
                                 "tipo": "CALL y PUT (Refuerzo)"},
                            inversion=INVERSION, umbral_pct=um, stop_pct=stp,
                            entry_at_ask=FASE2, exit_at_bid=FASE2, nbbo_timeline=FASE2,
                            refuerzo_loss_pct=REFUERZO_LOSS, refuerzo_max=rm)
                        if r["status"] == "ok":
                            it = r["iteration"]
                            rows.append({"fecha": d, "gain": it.gain_total,
                                         "invest": it.invest_total,
                                         "roi": (it.gain_total / it.invest_total)
                                                if it.invest_total else 0.0})
                m = analytics.backtest_risk_metrics(rows)
                if m.get("n", 0) == 0:
                    continue
                tot_inv = sum(x["invest"] for x in rows)
                roi = (m["total_gain"] / tot_inv * 100.0) if tot_inv else 0.0
                wr = sum(1 for x in rows if x["gain"] > 0) / m["n"] * 100.0
                rec = {"refuerzos": rm, "umbral": um, "stop": stp, "n": m["n"],
                       "total": round(m["total_gain"], 0), "roi_pct": round(roi, 1),
                       "pf": round(_pf(m["profit_factor"]), 2), "win_pct": round(wr, 0),
                       "max_dd": round(m["max_drawdown"], 0),
                       "catastroficos": m["n_catastrophic"],
                       "peor_dia_pct": round(m["worst_roi"] * 100, 0),
                       "sortino": round(m["sortino"], 2)}
                out.append(rec)
                print(f"  ref={rm} um={um:>2} stop={stp:>4} | n={m['n']:>3} "
                      f"tot=${rec['total']:>+9,.0f} roi={rec['roi_pct']:>+6.1f}% "
                      f"PF={rec['pf']:>5} win={rec['win_pct']:>3.0f}% "
                      f"maxDD=${rec['max_dd']:>+9,.0f} cat={rec['catastroficos']:>2} "
                      f"peor={rec['peor_dia_pct']:>4.0f}%", flush=True)

    if not out:
        print("Sin resultados.", flush=True)
        return
    csv_path = HERE / "data" / "sweep_tail.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    print(f"\n== {len(out)} combos en {(time.time() - t0) / 60:.1f} min · CSV: {csv_path.name} ==", flush=True)
    # Viables = rentables (PF>1 y total>0). Ranking por COLA: menos catastróficos, menor
    # drawdown (más cerca de 0), luego más total.
    viable = [r for r in out if r["pf"] > 1 and r["total"] > 0]
    viable.sort(key=lambda r: (r["catastroficos"], -r["max_dd"], -r["total"]))
    print(f"\n--- Viables (PF>1, total>0): {len(viable)}/{len(out)} · TOP 6 por menor COLA ---", flush=True)
    print(f"  {'ref':>3} {'um':>2} {'stop':>4} | {'total':>9} {'PF':>4} {'win':>4} "
          f"{'maxDD':>10} {'cat':>3} {'peor':>5}", flush=True)
    for r in viable[:6]:
        print(f"  {r['refuerzos']:>3} {r['umbral']:>2} {r['stop']:>4} | ${r['total']:>+8,.0f} "
              f"{r['pf']:>4} {r['win_pct']:>3.0f}% ${r['max_dd']:>+9,.0f} "
              f"{r['catastroficos']:>3} {r['peor_dia_pct']:>4.0f}%", flush=True)
    # Comparar contra la config actual del usuario (ref=4, um=2, stop=-100).
    _cur = next((r for r in out if r["refuerzos"] == 4 and r["umbral"] == 2 and r["stop"] == -100), None)
    if _cur:
        print(f"\n--- Config ACTUAL (ref=4 um=2 stop=-100): total=${_cur['total']:+,.0f} "
              f"PF={_cur['pf']} win={_cur['win_pct']:.0f}% maxDD=${_cur['max_dd']:+,.0f} "
              f"cat={_cur['catastroficos']} peor={_cur['peor_dia_pct']:.0f}% ---", flush=True)


if __name__ == "__main__":
    main()

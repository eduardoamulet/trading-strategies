"""Barrido de sensibilidad: ¿ALGÚN set de parámetros hace rentable la estrategia con
fills REALISTAS (Fase 2 — bid por barra), o el resultado era un pico frágil de los
precios de barra?

Varía refuerzo_max × umbral_ROI × stop_loss sobre una muestra de días y reporta, por
combo: total $, ROI %, profit factor, win rate, max drawdown y # días catastróficos
(≤ −90%). Reusa analytics.backtest_risk_metrics.

Eficiencia: para un (ticker, fecha, hora) fijos el contrato elegido NO depende de los
parámetros de salida → la línea de quotes (Fase 2) se baja una vez por día y todos los
combos la reusan desde el cache. Editá las constantes de abajo y re-corré.
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

# ----------------------------- Config (editable) -----------------------------
TICKER = "QQQ"
ENTRY = "09:30"
INVERSION = 1000.0
DATE_START, DATE_END = "2026-01-02", "2026-06-13"
DAY_STEP = 4                       # 1 = todos los hábiles; 4 = 1 de cada 4 (muestra)
REFUERZO_LOSS = 0.50               # umbral de pérdida por pierna que dispara reforzar
GRID_REFUERZO_MAX = [0, 2, 4]      # 0 = straddle simple (sin martingala)
GRID_UMBRAL = [10, 20, 30]         # umbral de ROI total (%) para salir ganando
GRID_STOP = [-100, -50, -30]       # stop sobre el ROI total (%); -100 = sin stop
FASE2 = True                       # fills NBBO por barra (realista). False = precio de barra.
# -----------------------------------------------------------------------------


def _pf_str(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def main() -> None:
    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY), HERE / "data")
    dl.resolution = "1min"
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(DATE_START, DATE_END)][::DAY_STEP]
    n_combos = len(GRID_REFUERZO_MAX) * len(GRID_UMBRAL) * len(GRID_STOP)
    print(f"Barrido {TICKER} · entrada {ENTRY} · {len(days)} días muestreados · "
          f"Fase 2={FASE2} · {n_combos} combos", flush=True)
    t0 = time.time()
    out = []
    for rm in GRID_REFUERZO_MAX:
        for um in GRID_UMBRAL:
            for stp in GRID_STOP:
                rows = []
                for d in days:
                    r = sbt.run_one(
                        dl, {"ticker": TICKER, "fecha": d, "hora": ENTRY,
                             "tipo": "CALL y PUT (Refuerzo)"},
                        inversion=INVERSION, umbral_pct=um, stop_pct=stp,
                        entry_at_ask=FASE2, exit_at_bid=FASE2, nbbo_timeline=FASE2,
                        refuerzo_loss_pct=REFUERZO_LOSS, refuerzo_max=rm)
                    if r["status"] == "ok":
                        it = r["iteration"]
                        rows.append({"fecha": d, "gain": it.gain_total,
                                     "invest": it.invest_total,
                                     "roi": (it.gain_total / it.invest_total
                                             if it.invest_total else 0.0)})
                m = analytics.backtest_risk_metrics(rows)
                if m.get("n", 0) == 0:
                    continue
                tot_inv = sum(x["invest"] for x in rows)
                roi_tot = (m["total_gain"] / tot_inv * 100.0) if tot_inv else 0.0
                wr = sum(1 for x in rows if x["gain"] > 0) / m["n"] * 100.0
                rec = {"refuerzo_max": rm, "umbral": um, "stop": stp, "n": m["n"],
                       "total": m["total_gain"], "roi_pct": roi_tot,
                       "pf": m["profit_factor"], "winrate": wr,
                       "maxdd": m["max_drawdown"], "ncat": m["n_catastrophic"]}
                out.append(rec)
                print(f"  rm={rm} um={um:>2} stop={stp:>4} | n={m['n']:>2} "
                      f"tot=${m['total_gain']:>+10,.0f} roi={roi_tot:>+6.1f}% "
                      f"PF={_pf_str(m['profit_factor']):>5} win={wr:>4.0f}% "
                      f"maxDD=${m['max_drawdown']:>9,.0f} cat={m['n_catastrophic']}", flush=True)

    if not out:
        print("Sin resultados (¿días sin data?).", flush=True)
        return

    out.sort(key=lambda x: x["total"], reverse=True)
    csv_path = HERE / "data" / "sweep_sensitivity.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    elapsed = time.time() - t0
    print(f"\n=== TOP 5 por total $ (de {len(out)} combos · {elapsed:.0f}s) ===", flush=True)
    for r in out[:5]:
        print(f"  rm={r['refuerzo_max']} um={r['umbral']} stop={r['stop']:>4} -> "
              f"${r['total']:>+10,.0f} ({r['roi_pct']:+.1f}%) PF={_pf_str(r['pf'])} "
              f"win={r['winrate']:.0f}% cat={r['ncat']}", flush=True)

    prof = [r for r in out if r["pf"] != float("inf") and r["pf"] > 1.0 and r["total"] > 0]
    print(f"\nCombos RENTABLES (PF>1 y total>0): {len(prof)}/{len(out)}", flush=True)
    best0 = max((r for r in out if r["refuerzo_max"] == 0), key=lambda x: x["total"], default=None)
    bestR = max((r for r in out if r["refuerzo_max"] > 0), key=lambda x: x["total"], default=None)
    if best0 and bestR:
        print(f"Mejor SIN refuerzo (rm=0): ${best0['total']:+,.0f} PF={_pf_str(best0['pf'])} | "
              f"Mejor CON refuerzo: ${bestR['total']:+,.0f} PF={_pf_str(bestR['pf'])}", flush=True)
    print(f"CSV guardado: {csv_path}", flush=True)


if __name__ == "__main__":
    main()

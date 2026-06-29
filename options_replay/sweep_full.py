"""Sweep COMPLETO (Fase 2) — QQQ+SPY+IWM combinado, 2026-01-01→2026-06-18, ventana 4 min,
Opción 1 (menor spread), entrada 09:30 / salida 16:00, capital $1000 (CALL 50% / PUT 50%).

Grid focalizado (~45 combos): Umbral ROI [2,4,6,8,10] × Stop [60,80,100] × Tipo:
  · CALL y PUT (sin refuerzo)
  · CALL y PUT (Refuerzo) — umbral 50%, max 2
  · CALL y PUT (Refuerzo) — umbral 50%, max 4

Por combo calcula: totales (días, inversión, ganancia, capital final, ganadores/perdedores,
win rate), riesgo (PF, max drawdown, peor día, días ≤−90%, racha perdedora, Sortino) y
categorías de salida (umbral / stop / cierre). Produce rankings por capital final, PF, Sortino,
max drawdown y win rate, e identifica las configs con MEJOR EQUILIBRIO (rank-sum). Guarda
data/sweep_full.csv (todos los combos) y data/sweep_full_best_daily.csv (detalle diario del mejor).

Uso (desde options_replay/):  py sweep_full.py
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

TICKERS = ["QQQ", "SPY", "IWM"]
DATE_START, DATE_END = "2026-01-01", "2026-06-18"
ENTRY, SEARCH_WIN = "09:30", 4.0
INVERSION, CALL_PCT = 1000.0, 50.0
GRID_UMBRAL = [2, 4, 6, 8, 10]
GRID_STOP = [-60, -80, -100]
# (label, refuerzo_loss, refuerzo_max). "CALL y PUT" = sin refuerzo.
TIPO_VARIANTS = [
    ("CALL y PUT", 0.50, 0),
    ("CALL y PUT (Refuerzo)", 0.50, 2),
    ("CALL y PUT (Refuerzo)", 0.50, 4),
]
_REASON = {"100%_threshold": "umbral", "stop_loss": "stop", "session_end": "cierre"}


def _pf(pf):
    return 9.99 if pf == float("inf") else round(pf, 2)


def _tag(tv, um, stp):
    _t, _rl, _rm = tv
    _base = "CALL y PUT" if _rm == 0 else f"Refuerzo(50%,x{_rm})"
    return f"{_base} · um{um} · stop{stp}"


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=600), HERE / "data")
    dl.resolution = "1min"
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(DATE_START, DATE_END)]
    n_combos = len(TIPO_VARIANTS) * len(GRID_UMBRAL) * len(GRID_STOP)
    print(f"== Sweep COMPLETO {','.join(TICKERS)} · {DATE_START}→{DATE_END} · {len(days)} días háb "
          f"· Fase 2 · ventana {SEARCH_WIN}m · {n_combos} combos ==", flush=True)
    t0 = time.time()
    out, best_daily = [], {}

    for tv in TIPO_VARIANTS:
        _tipo, _rloss, _rmax = tv
        for um in GRID_UMBRAL:
            for stp in GRID_STOP:
                rows = []
                for tk in TICKERS:
                    for d in days:
                        r = sbt.run_one(
                            dl, {"ticker": tk, "fecha": d, "hora": ENTRY, "tipo": _tipo},
                            inversion=INVERSION, umbral_pct=um, stop_pct=stp,
                            entry_at_ask=True, exit_at_bid=True, nbbo_timeline=True,
                            selection_criterion="spread", call_pct=CALL_PCT,
                            refuerzo_loss_pct=_rloss, refuerzo_max=_rmax,
                            search_window_min=SEARCH_WIN)
                        if r["status"] == "ok":
                            it = r["iteration"]
                            inv = it.invest_total
                            rows.append({"fecha": d, "ticker": tk, "gain": it.gain_total,
                                         "invest": inv,
                                         "roi": (it.gain_total / inv) if inv else 0.0,
                                         "reason": getattr(it, "exit_reason", "")})
                m = analytics.backtest_risk_metrics(rows)
                if m.get("n", 0) == 0:
                    continue
                tot_inv = sum(x["invest"] for x in rows)
                wins = sum(1 for x in rows if x["gain"] > 0)
                losses = sum(1 for x in rows if x["gain"] < 0)
                n_um = sum(1 for x in rows if x["reason"] == "100%_threshold")
                n_st = sum(1 for x in rows if x["reason"] == "stop_loss")
                n_eo = sum(1 for x in rows if x["reason"] == "session_end")
                rec = {
                    "tipo": "CALL y PUT" if _rmax == 0 else "CALL y PUT (Refuerzo)",
                    "ref_max": _rmax, "umbral": um, "stop": stp,
                    "dias": m["n"], "inv_total": round(tot_inv, 0),
                    "ganancia": round(m["total_gain"], 0),
                    "capital_final": round(tot_inv + m["total_gain"], 0),
                    "ganadores": wins, "perdedores": losses,
                    "win_pct": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0,
                    "pf": _pf(m["profit_factor"]),
                    "max_dd": round(m["max_drawdown"], 0),
                    "peor_dia_pct": round(m["worst_roi"] * 100, 0),
                    "dias_cat": m["n_catastrophic"], "racha_perd": m["max_losing_streak"],
                    "sortino": round(m["sortino"], 2),
                    "exit_umbral": n_um, "exit_stop": n_st, "exit_cierre": n_eo,
                    "_tag": _tag(tv, um, stp),
                }
                out.append(rec)
                best_daily[rec["_tag"]] = rows
                print(f"  {rec['_tag']:<30} | cap=${rec['capital_final']:>9,.0f} "
                      f"gan=${rec['ganancia']:>+8,.0f} PF={rec['pf']:>5} win={rec['win_pct']:>5.1f}% "
                      f"DD=${rec['max_dd']:>+8,.0f} cat={rec['dias_cat']:>2} Sortino={rec['sortino']:>5} "
                      f"| salidas u/s/c={n_um}/{n_st}/{n_eo}", flush=True)

    if not out:
        print("Sin resultados.", flush=True)
        return
    cols = [k for k in out[0] if not k.startswith("_")]
    with open(HERE / "data" / "sweep_full.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)

    print(f"\n== {len(out)} combos en {(time.time() - t0) / 60:.1f} min ==", flush=True)

    def _rank(key, reverse=True, n=5):
        return sorted(out, key=lambda r: r[key], reverse=reverse)[:n]

    def _line(r):
        return (f"    {r['_tag']:<30} cap=${r['capital_final']:>9,.0f} PF={r['pf']:>5} "
                f"Sortino={r['sortino']:>5} DD=${r['max_dd']:>+8,.0f} win={r['win_pct']:>5.1f}% "
                f"cat={r['dias_cat']}")

    print("\n──────────── RANKINGS (TOP 5) ────────────")
    for _title, _key, _rev in (("💰 Capital final", "capital_final", True),
                               ("📈 Profit Factor", "pf", True),
                               ("⚖️ Sortino", "sortino", True),
                               ("🛡️ Max Drawdown (menor)", "max_dd", True),  # max_dd negativo: mayor=mejor
                               ("🎯 Win Rate", "win_pct", True)):
        print(f"\n{_title}:")
        for r in _rank(_key, _rev):
            print(_line(r))

    # MEJOR EQUILIBRIO: rank-sum sobre los 5 criterios (capital, PF, Sortino, DD↑, win) + penaliza cat.
    _ranks = {id(r): 0 for r in out}
    for _key, _rev in (("capital_final", True), ("pf", True), ("sortino", True),
                       ("max_dd", True), ("win_pct", True)):
        for i, r in enumerate(sorted(out, key=lambda x: x[_key], reverse=_rev)):
            _ranks[id(r)] += i
    for r in out:
        r["_score"] = _ranks[id(r)] + r["dias_cat"] * 3   # cada día catastrófico penaliza
    balance = sorted(out, key=lambda r: r["_score"])[:5]
    print("\n──────────── 🏆 MEJOR EQUILIBRIO riesgo/retorno (rank-sum + penaliza cola) ────────────")
    for r in balance:
        print(_line(r) + f"  [score={r['_score']}]")

    # Detalle diario del MEJOR-equilibrio → CSV.
    _best = balance[0]
    _bd = best_daily.get(_best["_tag"], [])
    if _bd:
        _df = pd.DataFrame(_bd).sort_values(["fecha", "ticker"])
        _df["cap_acum"] = INVERSION + _df["gain"].cumsum()
        _df.to_csv(HERE / "data" / "sweep_full_best_daily.csv", index=False)
        print(f"\nDetalle diario del MEJOR ({_best['_tag']}) → data/sweep_full_best_daily.csv "
              f"({len(_bd)} filas)")


if __name__ == "__main__":
    main()

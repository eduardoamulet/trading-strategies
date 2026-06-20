"""EXPERIMENTO 1 — Base rate honesto del straddle 0DTE (el ancla del plan).

Straddle ciego diario (CALL+PUT, Opción 1 menor spread) · hold hasta 16:00 (sin TP/stop) ·
fills Fase 2 (entrada al ASK, salida al BID, timeline NBBO por minuto) · QQQ/SPY/IWM sobre
la muestra grande (4 años). Responde: ¿el 0DTE straddle es estructuralmente break-even/perdedor
por theta + spread? Fija el piso contra el que se comparan los demás experimentos.

Headless, reusa signals_backtest.run_one + analytics.backtest_risk_metrics. Toda la data está
cacheada → no pega a Polygon. Paralelo por día (el Downloader es thread-safe).

Uso (desde options_replay/):
    py exp1_baserate.py                       # QQQ,SPY,IWM · 2022-06-01→2026-06-18 · todos los días
    py exp1_baserate.py --step 5              # muestra (1 de cada 5 días) para una corrida rápida
    py exp1_baserate.py --tickers QQQ --start 2022-06-01 --end 2022-12-31
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import pandas as pd  # noqa: E402

import config  # noqa: E402
import analytics  # noqa: E402
import signals_backtest as sbt  # noqa: E402
from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402


def _pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def run_ticker(dl, ticker: str, days: list[str], entry: str, inversion: float, workers: int,
               criterion: str = "itm_first") -> list[dict]:
    """Corre el straddle hold-to-close (Fase 2) para todos los días; devuelve rows con gain/invest/roi.

    criterion='itm_first' (Opción 2) NO evalúa spread → corre 100% offline desde quotes_minute.
    criterion='spread' (Opción 1) llama option_quote_at en la entrada → pega a Polygon si esos
    quotes no están cacheados (cache `quotes/`)."""
    def _one(d: str):
        try:
            r = sbt.run_one(dl, {"ticker": ticker, "fecha": d, "hora": entry, "tipo": "CALL y PUT"},
                            inversion=inversion, umbral_pct=1000.0, stop_pct=-100.0,
                            entry_at_ask=True, exit_at_bid=True, nbbo_timeline=True,
                            selection_criterion=criterion, search_window_min=0.0)
            if r.get("status") != "ok":
                return None
            it = r["iteration"]
            inv = it.invest_total
            return {"fecha": d, "gain": it.gain_total, "invest": inv,
                    "roi": (it.gain_total / inv if inv else 0.0)}
        except Exception:  # noqa: BLE001 — un día que falla no frena al resto
            return None

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_one, days):
            if res is not None:
                rows.append(res)
    rows.sort(key=lambda x: x["fecha"])
    return rows


def report(name: str, rows: list[dict]) -> dict:
    m = analytics.backtest_risk_metrics(rows)
    if m.get("n", 0) == 0:
        print(f"{name:<10} sin datos")
        return m
    tot_inv = sum(r["invest"] for r in rows)
    roi = (m["total_gain"] / tot_inv * 100.0) if tot_inv else 0.0
    wr = sum(1 for r in rows if r["gain"] > 0) / m["n"] * 100.0
    print(f"{name:<10} n={m['n']:>4} | tot=${m['total_gain']:>+11,.0f} roi/día={roi:>+6.2f}% "
          f"PF={_pf(m['profit_factor']):>5} win={wr:>4.0f}% exp=${m['expectancy']:>+6.1f} "
          f"maxDD=${m['max_drawdown']:>9,.0f} | peor={m['worst_roi']*100:>5.0f}% ({m['worst_fecha']}) "
          f"Sortino={_pf(m['sortino']):>5} cat(≤-90%)={m['n_catastrophic']} rachaL={m['max_losing_streak']}")
    return m


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Exp 1 — base rate straddle 0DTE (Fase 2).")
    ap.add_argument("--tickers", default="QQQ,SPY,IWM")
    ap.add_argument("--start", default="2022-06-01")
    ap.add_argument("--end", default="2026-06-18")
    ap.add_argument("--step", type=int, default=1, help="1 = todos los días; N = 1 de cada N (muestra).")
    ap.add_argument("--entry", default="09:30")
    ap.add_argument("--inversion", type=float, default=1000.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rate", type=int, default=600, help="Rate del adapter (entradas no cacheadas).")
    ap.add_argument("--criterion", default="spread", choices=["itm_first", "spread"],
                    help="spread (Opción 1, lo que operás) | itm_first (Opción 2). Ambos cachean la entrada.")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(args.start, args.end)][::args.step]
    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=args.rate), HERE / "data")
    dl.resolution = "1min"

    _opc = "Opción 2 (ITM-first)" if args.criterion == "itm_first" else "Opción 1 (menor spread)"
    print(f"== EXP 1 · base rate straddle 0DTE (hold 16:00, Fase 2, {_opc}) ==")
    print(f"   {', '.join(tickers)} · {args.start}→{args.end} · {len(days)} días hábiles"
          f"{f' (1 de cada {args.step})' if args.step > 1 else ''} · entrada {args.entry}\n")

    all_rows: list[dict] = []
    for tk in tickers:
        rows = run_ticker(dl, tk, days, args.entry, args.inversion, args.workers, args.criterion)
        report(tk, rows)
        all_rows.extend(rows)

    print("-" * 150)
    report("PORTFOLIO", all_rows)
    print("\nLectura: PF<1 = pierde; PF≈1 = break-even; mirá max-drawdown, peor día y catastróficos, "
          "no el win rate. Esto es el PISO; los siguientes experimentos deben superarlo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

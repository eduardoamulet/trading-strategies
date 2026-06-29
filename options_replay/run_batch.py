"""Runner de TERMINAL para el batch (multiproceso REAL, fuera de Streamlit).

Útil para lotes GRANDES o cuando el subproceso falla dentro de la app (p.ej. [WinError 5] sobre %TEMP%).
Corre cada config del Excel × ticker × fecha y escribe el Excel de resultados (Resumen + Detalle).

Uso (desde la carpeta del proyecto «Traiding»):
  python options_replay/run_batch.py --excel "Backtesting_QQQ_160.xlsx" --tickers QQQ \
      --start 2025-01-01 --end 2026-01-30 --out resultados.xlsx

  • --tickers QQQ,SPY,IWM           (varios, separados por coma)
  • --dates 2026-01-13,2026-01-14   (fechas explícitas, en vez de --start/--end)
  • --processes 8                   (forzar nº de procesos; 0 = automático = cores−1)
"""
import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def _business_dates(start: str, end: str) -> list:
    import pandas as pd
    return [d.date().isoformat() for d in pd.date_range(start, end, freq="B")]


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Batch backtesting (multiproceso real, sin Streamlit).")
    ap.add_argument("--excel", required=True, help="Excel de configuraciones (hoja «Backtesting»).")
    ap.add_argument("--tickers", default="QQQ", help="Ticker(s) separados por coma. Default: QQQ.")
    ap.add_argument("--start", help="Fecha inicial YYYY-MM-DD (con --end).")
    ap.add_argument("--end", help="Fecha final YYYY-MM-DD (con --start).")
    ap.add_argument("--dates", help="Fechas explícitas separadas por coma (en vez de --start/--end).")
    ap.add_argument("--tipo", default="CALL y PUT", help="Tipo de operación. Default: CALL y PUT.")
    ap.add_argument("--out", default="resultados_batch.xlsx", help="Excel de salida.")
    ap.add_argument("--processes", type=int, default=0, help="Nº de procesos (0 = automático).")
    a = ap.parse_args()

    import config
    import batch_runner as br

    configs = br.read_configs(a.excel)
    if not configs:
        sys.exit(f"El Excel '{a.excel}' no tiene filas de config (hoja «Backtesting» con columna ID).")
    tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    if a.dates:
        dates = [d.strip() for d in a.dates.split(",") if d.strip()]
    elif a.start and a.end:
        dates = _business_dates(a.start, a.end)
    else:
        sys.exit("Pasá --dates  o  --start y --end.")

    total = len(configs) * len(tickers) * len(dates)
    procs = a.processes or br.auto_processes()
    print(f"{len(configs)} configs × {len(tickers)} ticker(s) × {len(dates)} fecha(s) = "
          f"{total:,} backtests · {procs} procesos\n", flush=True)
    t0 = time.time()

    def _cb(done, tot):
        el = time.time() - t0
        eta = (el / done * (tot - done)) if done else 0
        print(f"\r  {el:6.0f}s · {done:,}/{tot:,} ({100 * done / max(tot, 1):4.0f}%) · ETA {eta / 60:4.1f} min  ",
              end="", flush=True)

    rows = br.run_batch_parallel(str(HERE / "data"), config.POLYGON_API_KEY, configs, tickers, dates,
                                 a.tipo, progress_cb=_cb, processes=procs)
    print()
    out_path = Path(a.out)
    out_path.write_bytes(br.results_to_xlsx(rows, {"n_cfg": len(configs)}))
    t = br.batch_totals(rows)
    print(f"\n✅ Listo en {(time.time() - t0) / 60:.1f} min → {out_path.resolve()}")
    print(f"   {t['n_ok']:,}/{t['n_total']:,} ok · Inversión ${t['inv']:,.0f} · "
          f"Ganancia ${t['gain']:+,.0f} · ROI {t['roi']:.1%}")
    # Top-5 configs por ganancia
    top = sorted(br.summarize(rows), key=lambda s: s["Ganancia Total"], reverse=True)[:5]
    print("   Top configs:", " · ".join(f"{s['ID']} ${s['Ganancia Total']:+,.0f}" for s in top))


if __name__ == "__main__":
    import multiprocessing as mp
    mp.freeze_support()
    main()

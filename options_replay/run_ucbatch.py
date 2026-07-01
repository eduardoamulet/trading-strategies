"""Entrypoint de TERMINAL del batch de use-cases (multiproceso real, fuera de Streamlit).

Lee `Backtesting_use_cases_template.xlsx` (Data seed + scenarios) → corre escenario × día × ticker →
escribe el workbook de resultados → notifica. TODA la configuración sale del Data seed; NO usa
Session Parameters.

Uso (desde la carpeta «Traiding»):
  python options_replay/run_ucbatch.py --excel "Backtesting_use_cases_template.xlsx" --notify
  • --out <dir>        carpeta de salida (default: resultados/)
  • --processes 8      nº de procesos (0 = automático = cores−1)
  • --limit 10         correr solo los primeros N escenarios (0 = todos; útil para probar)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Batch backtesting dirigido por el template (Data seed + scenarios).")
    ap.add_argument("--excel", required=True, help="Backtesting_use_cases_template.xlsx")
    ap.add_argument("--out", default=str(HERE.parent / "resultados"), help="Carpeta de salida.")
    ap.add_argument("--processes", type=int, default=0, help="Nº de procesos (0 = automático).")
    ap.add_argument("--limit", type=int, default=0, help="Correr solo los primeros N escenarios (0 = todos).")
    ap.add_argument("--notify", action="store_true", help="Popup + consola abierta al terminar (lo usa la app).")
    ap.add_argument("--fill", default="", help="Results file a RELLENAR (1 fila por escenario, agregando "
                                               "sus runs y matcheando por ID). Si se da, no genera un workbook nuevo.")
    a = ap.parse_args()

    import config
    from ucbatch import reader, runner, report, scenario
    from ucbatch.progress import cli_progress, notify_done

    seed, scens = reader.read_template(a.excel)
    if a.limit and a.limit > 0:
        scens = scens[:a.limit]
    mapped = [scenario.map_scenario(seed, s) for s in scens]
    days = runner.trading_days(seed.fecha_inicial, seed.fecha_final)
    total = len(mapped) * len(days) * len(seed.tickers)
    procs = a.processes or runner.auto_processes()
    print(f"{len(mapped)} escenarios × {len(days)} días × {len(seed.tickers)} tickers "
          f"({', '.join(seed.tickers)}) = {total:,} backtests · {procs} procesos\n", flush=True)

    t0 = time.time()
    rows, days = runner.run(seed, mapped, str(HERE / "data"), config.POLYGON_API_KEY,
                            processes=procs, progress_cb=lambda d, t: cli_progress(d, t, t0))
    print()
    if a.fill:
        out = report.fill_results(seed, rows, a.fill, Path(a.out) / report.output_filename(seed))
        print(f"   (modo RELLENAR: 1 fila por escenario sobre {Path(a.fill).name})")
    else:
        out = report.write(seed, rows, a.out, listas_src=a.excel)
    n_err = sum(1 for r in rows if r.get("n_err"))
    el = time.time() - t0
    print(f"\n✅ Listo en {el / 60:.1f} min → {out}")
    print(f"   {len(rows):,} posiciones · {len(rows) - n_err:,} OK · {n_err:,} con error")
    if a.notify:
        notify_done(out, len(rows) - n_err, n_err, el)


if __name__ == "__main__":
    import multiprocessing as mp
    mp.freeze_support()
    main()

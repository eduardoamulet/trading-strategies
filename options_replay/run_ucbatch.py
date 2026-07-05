"""Entrypoint de TERMINAL del batch de use-cases (multiproceso real, fuera de Streamlit).

Lee `Backtesting_use_cases_template.xlsx` (Data seed + scenarios) → corre escenario × día × ticker →
escribe el workbook de resultados → notifica. TODA la configuración sale del Data seed; NO usa
Session Parameters.

Uso (desde la carpeta «Traiding»):
  python options_replay/run_ucbatch.py --excel "Backtesting_use_cases_template.xlsx" --notify
  python options_replay/run_ucbatch.py --combination comb_XXXX --desde 2026-07-01 --hasta 2026-07-02
  • --combination <id> correr los escenarios de una COMBINACIÓN de la base (combinations.db)
                       en vez de un Excel; --desde/--hasta/--tickers overridean su seed.
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
    ap = argparse.ArgumentParser(description="Batch backtesting dirigido por template o por COMBINACIÓN (DB).")
    ap.add_argument("--excel", default="", help="Backtesting_use_cases_template.xlsx (modo archivo).")
    ap.add_argument("--combination", default="", help="ID de la combinación en combinations.db (modo DB).")
    ap.add_argument("--desde", default="", help="(modo combinación) Fecha inicial — override del seed.")
    ap.add_argument("--hasta", default="", help="(modo combinación) Fecha final — override del seed.")
    ap.add_argument("--tickers", default="", help="(modo combinación) Tickers coma-separados — override.")
    ap.add_argument("--out", default=str(HERE.parent / "resultados"), help="Carpeta de salida.")
    ap.add_argument("--processes", type=int, default=0, help="Nº de procesos (0 = automático).")
    ap.add_argument("--limit", type=int, default=0, help="Correr solo los primeros N escenarios (0 = todos).")
    ap.add_argument("--notify", action="store_true", help="Popup + consola abierta al terminar (lo usa la app).")
    ap.add_argument("--fill", default="", help="Results file a RELLENAR (1 fila por escenario, agregando "
                                               "sus runs y matcheando por ID). Si se da, no genera un workbook nuevo.")
    ap.add_argument("--ingest", action="store_true",
                    help="PERSISTENCIA DIRECTA: las filas van al almacén (bt_results.db) sin "
                         "escribir un results xlsx — sin el techo de 1.048.576 filas por hoja. "
                         "La ejecución queda registrada en reeval_runs (estado/duración/métricas).")
    ap.add_argument("--run-id", default="", help="(--ingest) Id de la ejecución en reeval_runs; "
                                                 "default: timestamp.")
    ap.add_argument("--run-tipo", default="reevaluacion",
                    help="(--ingest) Tipo registrado: reevaluacion | incremental | verify.")
    a = ap.parse_args()
    if bool(a.excel) == bool(a.combination):
        ap.error("indicá exactamente UNO: --excel <template> o --combination <id>.")
    if a.ingest and not a.combination:
        ap.error("--ingest requiere --combination (el almacén segrega por combinación).")
    if a.ingest and a.fill:
        ap.error("--ingest y --fill son excluyentes.")

    import logging
    import os

    import config
    from obs_log import (get_logger, log_exception, redact, resource_snapshot,
                         set_correlation_id, timed)
    from ucbatch import reader, runner, report, scenario
    from ucbatch.progress import cli_progress, notify_done

    # Correlation ID: rastrea ESTA corrida por todo el log (persiste en options_replay/logs/batch.log,
    # rotativo → sobrevive al crash para el post-mortem). Consola a WARNING para no ensuciar la barra de
    # progreso \r; el archivo captura TODO (DEBUG+). NUNCA se loguea la API key (ver redact()).
    corr = f"bt-{str(int(time.time()))[-6:]}-{os.getpid() % 1000:03d}"
    set_correlation_id(corr)
    log = get_logger("ucbatch", to_file="batch.log", level=logging.WARNING)
    print(f"(log detallado → options_replay/logs/batch.log · corr={corr})", flush=True)

    _run_id = a.run_id or time.strftime("%Y%m%d_%H%M%S")
    try:
        if a.combination:
            import combinations as _comb
            seed = _comb.seed_for(a.combination, fecha_inicial=a.desde or None,
                                  fecha_final=a.hasta or None,
                                  tickers=([t.strip().upper() for t in a.tickers.split(",")
                                            if t.strip()] or None))
            scens = _comb.scenarios_for_batch(a.combination)
            if not scens:
                raise ValueError(f"La combinación {a.combination!r} no tiene escenarios "
                                 "generados — generalos desde la página Playbook.")
            _src_name, _listas, _tag = f"combination:{a.combination}", None, a.combination
            _comb_nombre = (_comb.get_combination(a.combination) or {}).get("nombre") or a.combination
        else:
            seed, scens = reader.read_template(a.excel)
            _src_name, _listas, _tag = Path(a.excel).name, a.excel, ""
            _comb_nombre = ""
        if a.limit and a.limit > 0:
            scens = scens[:a.limit]
        mapped = [scenario.map_scenario(seed, s) for s in scens]
        days = runner.trading_days(seed.fecha_inicial, seed.fecha_final)
        total = len(mapped) * len(days) * len(seed.tickers)
        procs = a.processes or runner.auto_processes()
        if a.ingest:
            import bt_store
            bt_store.record_run_start(_run_id, combination=a.combination,
                                      combination_nombre=_comb_nombre, tipo=a.run_tipo,
                                      fecha_desde=seed.fecha_inicial, fecha_hasta=seed.fecha_final,
                                      tickers=",".join(seed.tickers),
                                      n_escenarios=len(mapped), n_dias=len(days))
        # Config de la corrida SIN datos sensibles (api_key nunca entra al dict; redact() es doble-seguro).
        log.info("BATCH cfg %s", redact({
            "src": _src_name, "fill": Path(a.fill).name if a.fill else "",
            "out": a.out, "processes": procs, "limit": a.limit or 0,
            "tickers": ",".join(seed.tickers), "rango": f"{seed.fecha_inicial}..{seed.fecha_final}",
            "escenarios": len(mapped), "dias": len(days), "backtests": total}))
        print(f"{len(mapped)} escenarios × {len(days)} días × {len(seed.tickers)} tickers "
              f"({', '.join(seed.tickers)}) = {total:,} backtests · {procs} procesos\n", flush=True)
        resource_snapshot(log, "batch-start")

        t0 = time.time()

        def _cb(d: int, t: int) -> None:
            cli_progress(d, t, t0)
            if d == 1 or d % 20 == 0 or d == t:   # tendencia de RAM cada ~20 días → caza el OOM
                resource_snapshot(log, f"dia {d}/{t}")

        _rate = int(getattr(config, "POLYGON_RATE_LIMIT_PER_MIN", 600))
        with timed(log, "batch-run", escenarios=len(mapped), dias=len(days), backtests=total):
            rows, days = runner.run(seed, mapped, str(HERE / "data"), config.POLYGON_API_KEY,
                                    processes=procs, progress_cb=_cb, rate_limit=_rate)
        print()
        n_err = sum(1 for r in rows if r.get("n_err"))
        if a.ingest:
            # PERSISTENCIA DIRECTA: filas → almacén (sin Excel intermedio) + cierre del run.
            import bt_store
            from ucbatch.canonical import rows_to_canonical_df
            df = rows_to_canonical_df(seed, rows)
            n_new = bt_store.ingest_df(df, source_file=f"ingest:{_run_id}",
                                       combination=a.combination)
            _g = df["ganancia"].fillna(0)
            _resumen = {"ganancia_total": round(float(_g.sum()), 2),
                        "wr_pct": (round(float((_g > 0).mean() * 100), 1) if len(_g) else None),
                        "posiciones": int(len(df))}
            bt_store.record_run_finish(_run_id, estado="exitosa", n_filas=len(df),
                                       n_filas_nuevas=n_new, n_err=n_err, resumen=_resumen)
            out = f"almacén [{a.combination}] (+{n_new:,} filas nuevas de {len(df):,})"
        elif a.fill:
            out = report.fill_results(seed, rows, a.fill, Path(a.out) / report.output_filename(seed))
            print(f"   (modo RELLENAR: 1 fila por escenario sobre {Path(a.fill).name})")
        else:
            out = report.write(seed, rows, a.out, listas_src=_listas, tag=_tag)
        el = time.time() - t0
        resource_snapshot(log, "batch-end")
        log.info("BATCH OK: %d posiciones · %d con error · %.1f min -> %s",
                 len(rows), n_err, el / 60, out)
        print(f"\n✅ Listo en {el / 60:.1f} min → {out}")
        print(f"   {len(rows):,} posiciones · {len(rows) - n_err:,} OK · {n_err:,} con error")
        if a.notify and not a.ingest:
            notify_done(out, len(rows) - n_err, n_err, el)
    except Exception as _crash:
        # Captura del CRASH de tope: traza completa al log (lo que faltaba cuando el batch «moría solo»).
        if a.ingest:
            try:
                import bt_store
                bt_store.record_run_finish(_run_id, estado="fallida",
                                           error_msg=f"{type(_crash).__name__}: {_crash}"[:500])
            except Exception:  # noqa: BLE001 — registrar el fallo nunca debe tapar la traza real
                pass
        log_exception(log, f"BATCH CRASH (corr={corr})")
        resource_snapshot(log, "batch-crash")
        print(f"\n❌ El batch abortó — traza completa en options_replay/logs/batch.log (corr={corr})",
              file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    import multiprocessing as mp
    mp.freeze_support()
    main()

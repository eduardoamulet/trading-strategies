"""Actualización INCREMENTAL diaria del playbook (Fase 1).

Flujo (pensado para la Tarea Programada de las 05:00, después de update_all.py):
  1. --bootstrap (opcional, una vez): ingesta al almacén los results DETALLADOS ya existentes
     en resultados/ (los agregados se saltan con aviso) — historia instantánea sin re-backtestear.
  2. Detecta los días hábiles FALTANTES entre el último día del almacén y ayer.
  3. Si faltan: corre el batch (run_ucbatch, 480 escenarios) SOLO para esos días —
     ~20 s por día — e ingesta el results al almacén (dedupe por PK).
  4. Recalcula el playbook con ventana rodante + decaimiento (build_from_store), lo guarda en
     data/playbook.json y agrega el snapshot a playbook_history.jsonl.

Nunca reprocesa días ya almacenados: las filas son hechos inmutables; solo la AGREGACIÓN
(el veredicto) se recalcula, y eso tarda milisegundos.

Uso:
  python update_playbook.py                     # incremental estándar (ventana 60 · HL 35 · n≥10)
  python update_playbook.py --bootstrap         # + ingesta los xlsx detallados de resultados/
  python update_playbook.py --skip-batch        # solo re-agregar (sin backtestear días nuevos)
  python update_playbook.py --window 40 --half-life 20 --min-n 8
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bt_store  # noqa: E402
import playbook_store as pbs  # noqa: E402


def _bootstrap() -> None:
    files = sorted(pbs.RESULTS_DIR.glob("Backtesting_Results_*.xlsx"))
    if not files:
        print("bootstrap: no hay results en resultados/")
        return
    for f in files:
        try:
            n = bt_store.ingest_results_file(f)
            print(f"bootstrap: {f.name} → +{n:,} filas")
        except ValueError as e:                      # agregados / no detallados
            print(f"bootstrap: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"bootstrap: {f.name} → ERROR {e}")


def _run_batch(desde: str, hasta: str, tickers: list[str]) -> Path:
    """Corre el batch SÍNCRONO para [desde, hasta] con el template parcheado. Devuelve el path
    del results generado (lo nombra ucbatch a partir del seed)."""
    from ucbatch import reader as _ucr, report as _ucrep

    job_dir = HERE / "data" / ".playbook_jobs" / ("incr_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    job_dir.mkdir(parents=True, exist_ok=True)
    tpl = job_dir / "template.xlsx"
    pbs._patch_template(tpl, desde, hasta, tickers)
    seed, scens = _ucr.read_template(str(tpl))
    out = pbs.RESULTS_DIR / _ucrep.output_filename(seed)
    print(f"batch incremental: {desde} → {hasta} · {len(tickers)} tickers × {len(scens)} escenarios")
    r = subprocess.run([sys.executable, str(HERE / "run_ucbatch.py"),
                        "--excel", str(tpl), "--out", str(pbs.RESULTS_DIR)],
                       cwd=str(HERE))
    if r.returncode != 0:
        raise RuntimeError(f"run_ucbatch terminó con código {r.returncode}")
    if not out.exists():
        raise RuntimeError(f"el batch no produjo {out.name}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Actualización incremental del playbook")
    ap.add_argument("--window", type=int, default=60, help="Ventana rodante (días hábiles).")
    ap.add_argument("--half-life", type=int, default=35, help="Half-life del decaimiento (días hábiles).")
    ap.add_argument("--min-n", type=int, default=10, help="Muestra mínima por día de la semana.")
    ap.add_argument("--bootstrap", action="store_true",
                    help="Ingesta los results detallados existentes en resultados/ antes de actualizar.")
    ap.add_argument("--skip-batch", action="store_true",
                    help="No backtestear días faltantes; solo re-agregar el veredicto.")
    args = ap.parse_args()

    if args.bootstrap:
        _bootstrap()

    cov = bt_store.coverage()
    print(f"almacén: {cov['filas']:,} filas · {cov['dias']} días ({cov['desde']} → {cov['hasta']}) "
          f"· tickers: {', '.join(cov['tickers'])}")
    if not cov["filas"]:
        print("Almacén vacío: corré con --bootstrap o lanzá una Reevaluación desde la página Playbook.")
        return

    # Días hábiles faltantes: (último almacenado, ayer]. Hoy se excluye (la sesión puede estar
    # abierta y el update de datos de las 05:00 cubre hasta ayer).
    if not args.skip_batch:
        from ucbatch import runner as _ucrun
        hoy = date.today().isoformat()
        pend = [d for d in _ucrun.trading_days(cov["hasta"], hoy)
                if cov["hasta"] < d < hoy]
        if pend:
            out = _run_batch(pend[0], pend[-1], cov["tickers"] or ["QQQ", "SPY", "IWM"])
            n = bt_store.ingest_results_file(out)
            print(f"ingesta: {out.name} → +{n:,} filas nuevas")
        else:
            print("sin días faltantes — el almacén está al día.")

    pb = pbs.build_from_store(window_days=args.window, half_life=args.half_life,
                              min_n=args.min_n)
    pbs.save_playbook(pb)
    pbs.append_history(pb)
    print(f"playbook: {pb['evaluado_desde']} → {pb['evaluado_hasta']} · {pb['modo']}")
    for d, i in pb["per_day"].items():
        print(f"  {d}: {i.get('scenario')} · {i.get('recommendation')} · "
              f"WR {i.get('win_rate')}% · Sharpe {i.get('sharpe')} · n={i.get('n')}")


if __name__ == "__main__":
    main()

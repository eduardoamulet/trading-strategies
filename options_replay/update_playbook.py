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
  python update_playbook.py                     # incremental estándar (ventana 120 · HL 35 · n≥16)
  python update_playbook.py --bootstrap         # + ingesta los xlsx detallados de resultados/
  python update_playbook.py --skip-batch        # solo re-agregar (sin backtestear días nuevos)
  python update_playbook.py --window 60 --half-life 20 --min-n 10
  python update_playbook.py --verify            # revalidación de integridad (sola, corre el día 1)

Política oficial (2026-07-03): ventana 120 días hábiles + half-life 35 + min_n 16; histéresis
campeón/retador; monitores de vigencia (calibración, CUSUM, churn, régimen de vol); los
parámetros solo se cambian en la revisión trimestral (window_sweep.py) o si el CUSUM alerta.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))           # config.py (API keys) vive en la raíz Traiding
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


def _run_batch(desde: str, hasta: str, tickers: list[str], combination: str,
               tipo: str = "incremental") -> dict:
    """Corre el batch SÍNCRONO para [desde, hasta] con INGESTA DIRECTA al almacén (sin Excel
    intermedio — el techo de 1.048.576 filas por hoja no aplica). La ejecución queda registrada
    en reeval_runs. Devuelve el registro del run (bt_store.get_run)."""
    import combinations as _comb

    c = _comb.get_combination(combination)
    if not c or not c.get("n_escenarios"):
        raise RuntimeError(f"La combinación {combination!r} no tiene escenarios generados.")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"batch incremental: {desde} → {hasta} · {len(tickers)} tickers × "
          f"{c['n_escenarios']:,} escenarios · combinación «{c['nombre']}» · ingesta directa")
    r = subprocess.run([sys.executable, str(HERE / "run_ucbatch.py"),
                        "--combination", combination, "--desde", desde, "--hasta", hasta,
                        "--tickers", ",".join(tickers), "--ingest",
                        "--run-id", run_id, "--run-tipo", tipo],
                       cwd=str(HERE))
    if r.returncode != 0:
        raise RuntimeError(f"run_ucbatch terminó con código {r.returncode}")
    run = bt_store.get_run(run_id)
    if not run or run.get("estado") != "exitosa":
        raise RuntimeError(f"el run {run_id} no quedó exitoso "
                           f"(estado: {(run or {}).get('estado')!r} · "
                           f"error: {(run or {}).get('error_msg')!r})")
    return run


def verify_integrity(n_sample: int = 3, combination: str | None = None) -> bool:
    """Revalidación MENSUAL de integridad: re-corre el batch para una muestra de días ya
    almacenados (primero / medio / último) de la COMBINACIÓN y compara `ganancia` fila a fila
    contra el almacén (clave fecha+ticker+id, tolerancia $0.01). Si hay deriva, el motor cambió
    respecto de lo almacenado → hay que subir bt_store.ENGINE_VERSION y re-backtestear (las
    filas viejas y nuevas dejaron de ser comparables). Devuelve True si la muestra está limpia."""
    import combinations as _comb
    import pandas as pd

    import config
    from ucbatch import runner as _ucrun, scenario as _ucsc
    from ucbatch.canonical import rows_to_canonical_df

    combination = combination or _comb.active_combination() or bt_store.LEGACY_COMBINATION
    dates = bt_store.distinct_dates(combination)
    # El almacén puede contener FERIADOS viejos (días de puro error, previos al calendario
    # NYSE en trading_days) — un re-run de esos días ahora produce un batch vacío: se excluyen.
    dates = [d for d in dates if _ucrun.trading_days(d, d)]
    if not dates:
        print("verify: almacén vacío — nada que verificar.")
        return True
    idx = sorted({0, len(dates) // 2, len(dates) - 1})
    sample = [dates[i] for i in idx][:max(1, n_sample)]
    cov = bt_store.coverage(combination)
    tickers = cov["tickers"] or ["QQQ", "SPY", "IWM"]
    print(f"verify [{combination}]: re-corriendo {len(sample)} día(s) de muestra: "
          f"{', '.join(sample)}")

    limpio = True
    scens = _comb.scenarios_for_batch(combination)
    for d in sample:
        # Re-run IN-PROCESS + DataFrame canónico directo (sin subprocess ni Excel efímero):
        # mismo motor, mismos redondeos que el results (ucbatch.canonical).
        seed = _comb.seed_for(combination, fecha_inicial=d, fecha_final=d, tickers=tickers)
        mapped = [_ucsc.map_scenario(seed, s) for s in scens]
        rows, _dd = _ucrun.run(seed, mapped, str(HERE / "data"), config.POLYGON_API_KEY)
        rdf = rows_to_canonical_df(seed, rows)
        nuevo = rdf.assign(fecha=rdf["fecha"].astype(str).str[:10])
        viejo = bt_store.load_range(d, d, combination=combination)
        viejo = viejo.assign(fecha=viejo["fecha"].astype(str).str[:10])
        keys = ["fecha", "ticker", "id"]
        m = viejo[keys + ["ganancia"]].merge(nuevo[keys + ["ganancia"]], on=keys, how="outer",
                                             suffixes=("_store", "_rerun"), indicator=True)
        falta = m[m["_merge"] != "both"]
        both = m[m["_merge"] == "both"]
        g_s = pd.to_numeric(both["ganancia_store"], errors="coerce")
        g_r = pd.to_numeric(both["ganancia_rerun"], errors="coerce")
        # filas-error (NaN en ambos lados) = iguales; deriva = difiere el número o el estado NaN
        drift = both[((g_s - g_r).abs() > 0.01) | (g_s.isna() != g_r.isna())]
        if len(falta) or len(drift):
            limpio = False
            print(f"verify {d}: ❌ {len(drift)} filas con deriva de ganancia · "
                  f"{len(falta)} filas sin contraparte (de {len(m):,})")
        else:
            print(f"verify {d}: ✓ {len(m):,} filas idénticas (tolerancia $0.01)")

    if limpio:
        print("verify: ✓ almacén consistente con el motor actual "
              f"(ENGINE_VERSION {bt_store.ENGINE_VERSION})")
    else:
        print("verify: ❌ DERIVA DETECTADA — el motor ya no reproduce las filas almacenadas.\n"
              "  Acción: subir bt_store.ENGINE_VERSION, borrar data/bt_results.db y "
              "re-backtestear (o mantener versiones separadas). Las filas viejas y nuevas "
              "NO son comparables.")
    # Registro de la ejecución (auditoría): el verify también es una corrida.
    _vid = datetime.now().strftime("%Y%m%d_%H%M%S") + "_vf"
    bt_store.record_run_start(_vid, combination=combination, tipo="verify",
                              fecha_desde=sample[0], fecha_hasta=sample[-1],
                              tickers=",".join(tickers), n_dias=len(sample))
    bt_store.record_run_finish(_vid, estado="exitosa",
                               resumen={"dias_muestra": sample, "limpio": bool(limpio)})
    return limpio


def main() -> None:
    ap = argparse.ArgumentParser(description="Actualización incremental del playbook")
    ap.add_argument("--window", type=int, default=120, help="Ventana rodante (días hábiles).")
    ap.add_argument("--half-life", type=int, default=35, help="Half-life del decaimiento (días hábiles).")
    ap.add_argument("--min-n", type=int, default=16, help="Muestra mínima por día de la semana.")
    ap.add_argument("--bootstrap", action="store_true",
                    help="Ingesta los results detallados existentes en resultados/ antes de actualizar.")
    ap.add_argument("--skip-batch", action="store_true",
                    help="No backtestear días faltantes; solo re-agregar el veredicto.")
    ap.add_argument("--verify", action="store_true",
                    help="Revalidación de integridad: re-corre días de muestra y compara con "
                         "el almacén. (Corre sola el día 1 de cada mes.)")
    ap.add_argument("--combination", default="",
                    help="ID de la Combinación de Backtesting a actualizar (default: la ACTIVA).")
    args = ap.parse_args()

    import combinations as _comb
    _comb.ensure_legacy()                      # migración única del template 480 → combinations.db
    combination = args.combination or _comb.active_combination()
    if not combination:
        print("No hay combinación ACTIVA — importá/generá una en la página Playbook "
              "(«Combinaciones de Backtesting») o pasá --combination.")
        return
    _c = _comb.get_combination(combination) or {}
    print(f"combinación activa: «{_c.get('nombre') or combination}» "
          f"({_c.get('n_escenarios', 0):,} escenarios)")

    if args.bootstrap:
        _bootstrap()

    cov = bt_store.coverage(combination)
    print(f"almacén [{combination}]: {cov['filas']:,} filas · {cov['dias']} días "
          f"({cov['desde']} → {cov['hasta']}) · tickers: {', '.join(cov['tickers'])}")
    if not cov["filas"]:
        print("Sin historia para esta combinación: lanzá una Reevaluación desde la página "
              "Playbook para poblarla (o --bootstrap si es la legacy tpl480).")
        return

    # Días hábiles faltantes: (último almacenado, ayer]. Hoy se excluye (la sesión puede estar
    # abierta y el update de datos de las 05:00 cubre hasta ayer).
    if not args.skip_batch:
        from ucbatch import runner as _ucrun
        hoy = date.today().isoformat()
        pend = [d for d in _ucrun.trading_days(cov["hasta"], hoy)
                if cov["hasta"] < d < hoy]
        if pend:
            run = _run_batch(pend[0], pend[-1], cov["tickers"] or ["QQQ", "SPY", "IWM"],
                             combination)
            print(f"ingesta directa [{run['run_id']}]: +{run['n_filas_nuevas']:,} filas nuevas "
                  f"(de {run['n_filas']:,} · {run['duration_s']:.0f} s)")
        else:
            print("sin días faltantes — el almacén está al día.")

    pb = pbs.build_from_store(window_days=args.window, half_life=args.half_life,
                              min_n=args.min_n, combination=combination)
    pbs.save_playbook(pb)
    pbs.append_history(pb)
    print(f"playbook: {pb['evaluado_desde']} → {pb['evaluado_hasta']} · {pb['modo']}")
    for d, i in pb["per_day"].items():
        rt = i.get("retador")
        flags = "".join([
            " · ⚠calibración" if i.get("calib_alerta") else "",
            " · ⚠CUSUM" if i.get("cusum_alerta") else "",
            (f" · churn {int(i['churn']['tasa'] * 100)}%"
             if (i.get("churn") or {}).get("alerta") else ""),
            (f" · retador {rt['scenario']} ({rt['racha']}/5)" if rt else ""),
        ])
        print(f"  {d}: {i.get('scenario')} · {i.get('recommendation')} · "
              f"WR {i.get('win_rate')}% (P5 {i.get('wr_p5')}%) · Sharpe {i.get('sharpe')} · "
              f"n={i.get('n')} · estado: {i.get('estado')}{flags}")
    rv = pb.get("regimen_vol")
    if rv:
        print(f"régimen vol {rv['ticker']}: 5d/mediana60d = {rv['ratio']}× "
              + ("⚠ ALERTA (>2×)" if rv.get("alerta") else "(normal)"))
    if pb.get("revision_anticipada"):
        print("⚠ CUSUM fuera de banda en ≥1 día → revisión anticipada de parámetros "
              "(correr window_sweep.py)")

    # Revalidación de integridad: a pedido (--verify) o automática el día 1 de cada mes.
    if args.verify or (date.today().day == 1 and not args.skip_batch):
        verify_integrity(combination=combination)


if __name__ == "__main__":
    main()

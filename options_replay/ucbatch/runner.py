"""Backtesting Engine — orquesta escenario × día × ticker.

Paraleliza por DÍA × TRAMO-DE-ESCENARIOS: cada tarea corre un tramo contiguo de escenarios de
un día (todos los tickers de cada escenario juntos, porque la salida COLECTIVA los acopla) y con
pocos días el día se parte en tramos para usar todos los cores (antes 1 día = 1 core). La señal
de dirección se cachea en data/md_cache.db (determinista) — no se recomputa por corrida.
Sin estado mutable compartido entre workers; el merge ocurre al final, en el proceso padre.

Reusa `run_one` (1 posición) y `apply_collective_exit` (ROI de cartera). El multiproceso DEBE
lanzarse desde un entrypoint guardado (`run_ucbatch.py`), no desde Streamlit (cuelga en Windows).
"""
from __future__ import annotations

import os

import pandas as pd

from portfolio_exit import apply_collective_exit
from signals_backtest import run_one

from .metrics import error_row, position_metrics, position_snapshot
from .scenario import resolution_from_seg

_G: dict = {}   # estado por-proceso (Downloader + seed), seteado en _init


# Feriados NYSE (cierres COMPLETOS) 2025–2027 — estático, sin dependencia externa. Los medios
# días (24-dic, viernes post-Thanksgiving, 3-jul cuando aplica como media sesión) SÍ son días
# hábiles y se backtestean normal. Fuera de este rango de años, cae al comportamiento anterior
# (el feriado entra y produce filas de error — inofensivo, solo ruido).
_NYSE_HOLIDAYS = frozenset({
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})


def trading_days(fecha_inicial: str, fecha_final: str) -> list:
    """Días hábiles [inicial, final] inclusive: excluye sáb/dom Y feriados NYSE (antes cada
    feriado entraba al batch como un día de puro error — 1.440 filas basura que además se
    ingestaban al almacén del playbook)."""
    return [d for d in (x.date().isoformat() for x in pd.bdate_range(fecha_inicial, fecha_final))
            if d not in _NYSE_HOLIDAYS]


def auto_processes() -> int:
    return max(2, (os.cpu_count() or 4) - 1)


def _init(data_dir, api_key, seed, resolution, mapped, corr) -> None:
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent      # options_replay
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    import obs_log
    obs_log.set_correlation_id(corr)      # el worker hereda el correlation-id de la corrida (trazable)
    from adapter_polygon import PolygonAdapter
    from downloader import Downloader
    dl = Downloader(PolygonAdapter(api_key, rate_limit_per_min=600), Path(data_dir))
    dl.resolution = resolution
    _G["dl"], _G["seed"], _G["mapped"] = dl, seed, mapped
    # Provider del Market Direction Engine (para enriquecer cada posición con la señal a la entrada).
    # Cacheado por worker; lee el MISMO cache de underlying → no agrega llamadas a Polygon.
    try:
        from market_direction.data import default_provider
        from market_direction.data.caching_provider import CachingProvider
        _G["dir_provider"] = CachingProvider(default_provider())
    except Exception:
        _G["dir_provider"] = None


def _chunk_ranges(n_scen: int, n_days: int, procs: int, min_chunk: int = 60) -> list:
    """Particiona [0, n_scen) en tramos CONTIGUOS para paralelizar DENTRO del día cuando hay más
    cores que días (antes el incremental de 1 día usaba 1 solo core). Nº de tramos ≈ procs/n_días,
    capado para que ningún tramo baje de `min_chunk` escenarios: cada worker de más paga ~1 s de
    spawn+imports en Windows y relee los datos del día — medido 2026-07-04, 480 escenarios × 1 día:
    8 workers = 9.2 s, 15 workers = 12.8 s. min_chunk=60 → 8 tramos para el template 480.
    Los tramos concatenados en orden reproducen EXACTAMENTE el orden original (verify-safe)."""
    import math
    if n_scen <= 0:
        return []
    k = max(1, min(math.ceil(procs / max(1, n_days)), math.ceil(n_scen / max(1, min_chunk))))
    base, extra = divmod(n_scen, k)
    bounds, lo = [], 0
    for i in range(k):
        hi = lo + base + (1 if i < extra else 0)
        bounds.append((lo, hi))
        lo = hi
    return bounds


def _run_day_chunk(task) -> list:
    """Corre UN TRAMO de escenarios de UN DÍA: mapped[lo:hi] × N tickers con los DATOS del día
    MEMOIZADOS. La salida COLECTIVA acopla los tickers de un mismo escenario y cada escenario
    viaja ENTERO dentro de su tramo → partir por escenarios no la rompe.

    El `MemoDownloader` y la señal de dirección se cachean POR WORKER PARA EL DÍA ACTUAL (los
    tramos del mismo día en el mismo worker no releen nada); al cambiar de día se descartan →
    memoria acotada a un día por worker."""
    day, lo, hi = task
    seed = _G["seed"]
    mapped_all = _G["mapped"][lo:hi]
    if _G.get("_memo_day") != day:
        from .memo_downloader import MemoDownloader
        # Señal del Market Direction Engine a la ENTRADA — 1× por ticker/día (la entrada es fija
        # para todos los escenarios); md_cache la hace ~gratis entre workers y entre corridas.
        _G["_memo_day"] = day
        _G["_memo"] = MemoDownloader(_G["dl"])            # cache de datos SCOPED a este día
        _G["_dir"] = {tk: _direction_fields(tk, day, seed.entrada) for tk in seed.tickers}
    memo = _G["_memo"]
    dir_fields = _G["_dir"]
    rows = []
    for mapped in mapped_all:
        results = []
        for ticker in seed.tickers:
            spec = {"ticker": ticker, "fecha": day, "hora": seed.entrada, "tipo": seed.tipo}
            try:
                results.append(run_one(memo, spec, **mapped.run_kwargs))
            except Exception as e:                               # noqa: BLE001 — aislar fallos de 1 posición
                results.append({**spec, "status": "error", "iteration": None, "error": str(e)})
        if mapped.collective:
            try:
                apply_collective_exit(results, **mapped.collective)   # muta los IterationResult abiertos
            except Exception:                                    # noqa: BLE001
                pass
        for ticker, r in zip(seed.tickers, results):
            base = {"ID": mapped.id, "Ticker": ticker, "Fecha": day, **dir_fields.get(ticker, {})}
            if r.get("status") == "ok" and r.get("iteration") is not None:
                rows.append({**base, **position_metrics(r["iteration"]),
                             **position_snapshot(r["iteration"]), "error": ""})
            else:
                rows.append({**base, **error_row(), **position_snapshot(None),
                             "error": str(r.get("error") or "")})
    return rows


def _direction_fields(ticker: str, day: str, hora: str) -> dict:
    """Señal del Market Direction Engine a la entrada — CACHEADA en data/md_cache.db (la señal
    histórica es determinista por ticker/fecha/hora + MD_ENGINE_VERSION): la 1ª vez se computa
    (~2 s), después es una lectura de ms. Si falla, devuelve None (nunca tumba el backtest)."""
    try:
        import md_cache
        return md_cache.get_or_compute(ticker, day, str(hora), provider=_G.get("dir_provider"))
    except Exception:   # noqa: BLE001 — el enriquecimiento nunca debe tumbar el backtest
        return {"md_action": None, "md_score": None, "md_confidence": None, "md_trend": None}


def run(seed, mapped_scenarios, data_dir, api_key, processes=None, progress_cb=None) -> tuple:
    """Ejecuta TODO el batch, paralelizando por DÍA × TRAMO-DE-ESCENARIOS. Con más días que
    cores, cada tarea es un día entero (comportamiento previo); con POCOS días (el incremental
    diario = 1), el día se parte en tramos contiguos de escenarios (_chunk_ranges) para usar
    todos los cores. La salida COLECTIVA acopla los tickers de un mismo día+escenario y cada
    escenario viaja entero en su tramo → la partición no la afecta. Devuelve (filas, días) con
    las filas en EL MISMO ORDEN de siempre (día-mayor, escenario ascendente — verify-safe).
    Llamar desde un entrypoint con `if __name__ == '__main__': mp.freeze_support()` (NO desde Streamlit).

    ROBUSTEZ: una tarea que lance excepción se LOGUEA (con traza + día/tramo) y se SALTA; el
    batch NO se tumba (antes el `for ... in imap` sin try/except rompía el iterator)."""
    import multiprocessing as mp

    import obs_log
    log = obs_log.get_logger("ucbatch")        # ya configurado por run_ucbatch (mismo proceso/archivo)
    # Fail-fast ANTES de abrir el pool: si la key está vacía, el initializer de CADA worker lanzaría y
    # el Pool se CUELGA reintentando (multiprocessing wart). Mejor un error claro que un cuelgue mudo.
    if not api_key:
        raise ValueError("POLYGON_API_KEY vacía — el batch no puede inicializar el Downloader.")
    corr = obs_log.correlation_id()
    days = trading_days(seed.fecha_inicial, seed.fecha_final)
    resolution = resolution_from_seg(seed.granularidad_seg)
    procs_avail = int(processes) if processes else auto_processes()
    bounds = _chunk_ranges(len(mapped_scenarios), len(days), procs_avail)
    tasks = [(day, lo, hi) for day in days for (lo, hi) in bounds]
    procs = min(procs_avail, max(1, len(tasks)))
    rows: list = []
    n_fail = 0
    with mp.Pool(procs, initializer=_init,
                 initargs=(str(data_dir), api_key, seed, resolution, mapped_scenarios, corr)) as pool:
        it = pool.imap(_run_day_chunk, tasks, chunksize=1)   # preserva el orden → i-ésimo result = tasks[i]
        for i in range(len(tasks)):
            day, lo, hi = tasks[i]
            try:
                chunk_rows = next(it)
                rows.extend(chunk_rows or [])
                log.debug("día %s tramo [%d,%d) OK (%d filas)", day, lo, hi, len(chunk_rows or []))
            except StopIteration:
                break
            except Exception:                            # noqa: BLE001 — aislar el fallo de UNA tarea
                n_fail += 1
                obs_log.log_exception(log, f"día {day} tramo [{lo},{hi}) FALLÓ — se salta "
                                           "(el batch continúa)", dia=day)
            if progress_cb:
                progress_cb(i + 1, len(tasks))
    if n_fail:
        log.warning("batch terminó con %d/%d tareas fallidas (ver trazas arriba)", n_fail, len(tasks))
    return rows, days

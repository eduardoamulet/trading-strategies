"""Backtesting Engine — orquesta escenario × día × ticker.

Paraleliza por (escenario, día): cada tarea corre los N tickers de ese día (posiciones
independientes) y luego aplica la salida COLECTIVA sobre las abiertas (que exige tenerlas juntas).
Sin estado mutable compartido entre workers; el merge ocurre al final, en el proceso padre.

Reusa `run_one` (1 posición) y `apply_collective_exit` (ROI de cartera). El multiproceso DEBE
lanzarse desde un entrypoint guardado (`run_ucbatch.py`), no desde Streamlit (cuelga en Windows).
"""
from __future__ import annotations

import os

import pandas as pd

from portfolio_exit import apply_collective_exit
from signals_backtest import run_one

from .metrics import error_row, position_metrics
from .scenario import resolution_from_seg

_G: dict = {}   # estado por-proceso (Downloader + seed), seteado en _init


def trading_days(fecha_inicial: str, fecha_final: str) -> list:
    """Días hábiles [inicial, final] inclusive (excluye sáb/dom; los feriados sin datos saldrán
    como filas de error)."""
    return [d.date().isoformat() for d in pd.bdate_range(fecha_inicial, fecha_final)]


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


def _run_day(day) -> list:
    """Corre UN DÍA: los N escenarios × N tickers con los DATOS del día MEMOIZADOS (se leen 1× y se
    reusan entre escenarios). Aplica el colectivo por escenario. Devuelve las filas escalares del día.

    El `MemoDownloader` se crea y descarta por día → la memoria queda acotada a un día por worker, y
    los datos NO se releen 480× (uno por escenario)."""
    from .memo_downloader import MemoDownloader
    seed, mapped_all = _G["seed"], _G["mapped"]
    memo = MemoDownloader(_G["dl"])           # cache de datos SCOPED a este día
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
            base = {"ID": mapped.id, "Ticker": ticker, "Fecha": day}
            if r.get("status") == "ok" and r.get("iteration") is not None:
                rows.append({**base, **position_metrics(r["iteration"]), "error": ""})
            else:
                rows.append({**base, **error_row(), "error": str(r.get("error") or "")})
    return rows


def run(seed, mapped_scenarios, data_dir, api_key, processes=None, progress_cb=None) -> tuple:
    """Ejecuta TODO el batch, paralelizando POR DÍA (cada día lee sus datos 1× y corre los N
    escenarios encima → sin releer 480×; memoria acotada a un día por worker). Devuelve (filas, días).
    Llamar desde un entrypoint con `if __name__ == '__main__': mp.freeze_support()` (NO desde Streamlit).

    ROBUSTEZ: un día que lance excepción se LOGUEA (con traza + día) y se SALTA; el batch NO se
    tumba (antes el `for ... in imap` sin try/except rompía el iterator y colgaba/mataba todo)."""
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
    procs = int(processes) if processes else auto_processes()
    rows: list = []
    n_fail = 0
    with mp.Pool(procs, initializer=_init,
                 initargs=(str(data_dir), api_key, seed, resolution, mapped_scenarios, corr)) as pool:
        it = pool.imap(_run_day, days, chunksize=1)     # preserva el orden → el i-ésimo result = days[i]
        for i in range(len(days)):
            day = days[i]
            try:
                day_rows = next(it)
                rows.extend(day_rows or [])
                log.debug("día %s OK (%d filas)", day, len(day_rows or []))
            except StopIteration:
                break
            except Exception:                            # noqa: BLE001 — aislar el fallo de UN día
                n_fail += 1
                obs_log.log_exception(log, f"día {day} FALLÓ — se salta (el batch continúa)", dia=day)
            if progress_cb:
                progress_cb(i + 1, len(days))
    if n_fail:
        log.warning("batch terminó con %d/%d días fallidos (ver trazas arriba)", n_fail, len(days))
    return rows, days

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


def _init(data_dir, api_key, seed, resolution) -> None:
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent      # options_replay
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from adapter_polygon import PolygonAdapter
    from downloader import Downloader
    dl = Downloader(PolygonAdapter(api_key, rate_limit_per_min=600), Path(data_dir))
    dl.resolution = resolution
    _G["dl"], _G["seed"] = dl, seed


def _run_scenario_day(task) -> list:
    """Corre un (escenario, día): los N tickers + colectivo + métricas. Devuelve filas escalares."""
    mapped, day = task
    dl, seed = _G["dl"], _G["seed"]
    results = []
    for ticker in seed.tickers:
        spec = {"ticker": ticker, "fecha": day, "hora": seed.entrada, "tipo": seed.tipo}
        try:
            results.append(run_one(dl, spec, **mapped.run_kwargs))
        except Exception as e:                                   # noqa: BLE001 — aislar fallos de 1 posición
            results.append({**spec, "status": "error", "iteration": None, "error": str(e)})
    if mapped.collective:
        try:
            apply_collective_exit(results, **mapped.collective)  # muta los IterationResult abiertos
        except Exception:                                        # noqa: BLE001
            pass
    rows = []
    for ticker, r in zip(seed.tickers, results):
        base = {"ID": mapped.id, "Ticker": ticker, "Fecha": day}
        if r.get("status") == "ok" and r.get("iteration") is not None:
            rows.append({**base, **position_metrics(r["iteration"]), "error": ""})
        else:
            rows.append({**base, **error_row(), "error": str(r.get("error") or "")})
    return rows


def run(seed, mapped_scenarios, data_dir, api_key, processes=None, progress_cb=None) -> tuple:
    """Ejecuta TODO el batch. Devuelve (filas, días). Llamar desde un entrypoint con
    `if __name__ == '__main__': mp.freeze_support()` (NO desde Streamlit)."""
    import multiprocessing as mp
    days = trading_days(seed.fecha_inicial, seed.fecha_final)
    resolution = resolution_from_seg(seed.granularidad_seg)
    tasks = [(m, d) for m in mapped_scenarios for d in days]     # escenario × día (tickers adentro)
    procs = int(processes) if processes else auto_processes()
    chunk = max(1, min(40, (len(tasks) // (procs * 8)) or 1))
    rows: list = []
    with mp.Pool(procs, initializer=_init, initargs=(str(data_dir), api_key, seed, resolution)) as pool:
        for i, chunk_rows in enumerate(pool.imap(_run_scenario_day, tasks, chunksize=chunk), 1):
            rows.extend(chunk_rows)
            if progress_cb:
                progress_cb(i, len(tasks))
    return rows, days

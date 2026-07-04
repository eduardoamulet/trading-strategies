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


def _run_day(day) -> list:
    """Corre UN DÍA: los N escenarios × N tickers con los DATOS del día MEMOIZADOS (se leen 1× y se
    reusan entre escenarios). Aplica el colectivo por escenario. Devuelve las filas escalares del día.

    El `MemoDownloader` se crea y descarta por día → la memoria queda acotada a un día por worker, y
    los datos NO se releen 480× (uno por escenario)."""
    from .memo_downloader import MemoDownloader
    seed, mapped_all = _G["seed"], _G["mapped"]
    memo = MemoDownloader(_G["dl"])           # cache de datos SCOPED a este día
    # Señal del Market Direction Engine a la ENTRADA — 1× por ticker/día (la entrada es fija para los
    # 480 escenarios), reusada en cada posición de ese ticker → cruza el «contexto de mercado» con el ROI.
    dir_fields = {tk: _direction_fields(tk, day, seed.entrada) for tk in seed.tickers}
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
    """Señal del Market Direction Engine a la entrada (score/confianza/tendencia/acción). Solo compute
    (usa el cache de underlying); si falla, devuelve None. Enriquece el análisis: «con este contexto,
    qué escenario funciona»."""
    try:
        from market_direction.engine import market_direction_engine
        sig = market_direction_engine(ticker, day, str(hora), provider=_G.get("dir_provider"))
        return {"md_action": sig.action.value, "md_score": round(float(sig.score), 1),
                "md_confidence": round(float(sig.confidence), 3), "md_trend": sig.trend.value}
    except Exception:   # noqa: BLE001 — el enriquecimiento nunca debe tumbar el backtest
        return {"md_action": None, "md_score": None, "md_confidence": None, "md_trend": None}


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
    # La unidad de paralelismo es el DÍA (no día×ticker): la salida COLECTIVA acopla los
    # tickers de un mismo día+escenario (apply_collective_exit) — partir más fino la rompería.
    # Sí capeamos los workers al nº de días: una corrida de 1 día (el incremental diario) no
    # paga el arranque de 15 procesos para usar 1.
    procs = min(int(processes) if processes else auto_processes(), max(1, len(days)))
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

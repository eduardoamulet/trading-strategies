"""Backfill MASIVO de la línea de bid/ask por minuto (quotes_minute) para TODOS los
contratos que ya tenés en barras (options/), ANTES de cancelar Polygon.

Los NBBO (tick-level) son el ÚNICO dato irreemplazable: cuando canceles el add-on de
opciones, el histórico de bid/ask NO se puede recomprar. Las barras OHLCV sí se
consiguen en otros proveedores. Este script vacía esa mina: por cada (contrato, día)
con barra 1-min cacheada, baja y cachea su quotes_minute (idempotente vía Downloader).

  - Orden de PRIORIDAD: el de --tickers (default QQQ → SPY → IWM), y dentro de cada uno
    los días más NUEVOS primero. Si cancelás a mitad, te quedaste con lo más reciente.
  - RESUMIBLE: lo ya cacheado se saltea (option_quote_series es idempotente + escribe
    atómico). Re-correr continúa donde quedó.
  - MULTIPROCESO: el cuello de botella es CPU (parsear el JSON de 50k filas/página y
    armar los DataFrames está serializado por el GIL en 1 solo core). Cada PROCESO usa
    su propio core → escala ~lineal con --procs. El rate-limit se reparte por proceso;
    un 429 del server se auto-throttlea (backoff).

Uso (desde options_replay/):
    py backfill_quotes.py --dry-run                  # solo reporta cuánto falta + ETA
    py backfill_quotes.py                             # baja QQQ/SPY/IWM (default)
    py backfill_quotes.py --tickers QQQ,SPY,IWM --procs 8 --rate 600
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import re
import sys
import time as _t
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import config  # type: ignore  # noqa: E402
from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402

DEFAULT_TICKERS = ["QQQ", "SPY", "IWM"]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OCC_RE = re.compile(r"^O:([A-Z]+)\d{6}[CP]\d{8}$")

# Downloader por-proceso (lo crea el initializer del pool; cada proceso tiene el suyo).
_DL: Downloader | None = None


def _parse_stem(stem: str):
    """`O_QQQ260116C00500000_2026-01-16` → (occ, ticker, date) o None si no es una
    barra de opción 1-min válida (p.ej. sufijo _15s/_30s, o nombre raro)."""
    occ_safe, _, tail = stem.rpartition("_")
    if not _DATE_RE.match(tail):          # _15s/_30s u otro → no es 1-min
        return None
    occ = occ_safe.replace("_", ":", 1)   # O_QQQ... → O:QQQ... (único ':' del OCC)
    m = _OCC_RE.match(occ)
    if not m:
        return None
    return occ, m.group(1), tail


def _scan(opt_dir: Path, want: set):
    """Una sola pasada por options/: bucket {ticker: [(occ, date), ...]} para los `want`."""
    buckets = {tk: [] for tk in want}
    with os.scandir(opt_dir) as it:
        for e in it:
            if not e.name.endswith(".parquet"):
                continue
            parsed = _parse_stem(e.name[:-8])   # sin ".parquet"
            if not parsed:
                continue
            occ, tk, date = parsed
            if tk in want:
                buckets[tk].append((occ, date))
    return buckets


def _init_worker(rate_per_proc: int, data_dir_str: str):
    """Initializer del pool: crea UN Downloader por proceso (con su propio rate-limit)."""
    global _DL
    _DL = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=rate_per_proc),
                     Path(data_dir_str))


def _job(occ_date):
    """Baja+cachea el quotes_minute de un contrato/día. Devuelve (was_empty, err|None)."""
    occ, date = occ_date
    try:
        df = _DL.option_quote_series(occ, date)   # idempotente + atómico
        return (df is None or df.empty), None
    except Exception as e:  # noqa: BLE001 — un contrato que falla no frena al resto
        return False, f"{occ} {date}: {e}"


def main() -> int:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    _cpu = os.cpu_count() or 4
    ap = argparse.ArgumentParser(description="Backfill de quotes_minute (NBBO por minuto).")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS),
                    help="CSV de tickers EN ORDEN de prioridad (default QQQ,SPY,IWM).")
    ap.add_argument("--procs", type=int, default=max(2, min(8, _cpu - 2)),
                    help=f"Procesos en paralelo (default {max(2, min(8, _cpu - 2))}; tenés {_cpu} cores).")
    ap.add_argument("--rate", type=int, default=600,
                    help="Llamadas/min TOTALES (se reparten por proceso; planes pagos no tienen tope).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo reporta cuánto falta + estimación de tiempo; no baja nada.")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    data_dir = HERE / "data"
    opt_dir = data_dir / "options"
    qm_dir = data_dir / "quotes_minute"
    qm_dir.mkdir(parents=True, exist_ok=True)

    print(f"== backfill quotes_minute == {', '.join(tickers)} · {args.procs} procesos", flush=True)
    buckets = _scan(opt_dir, set(tickers))

    todo: list = []
    for tk in tickers:                     # respeta el ORDEN de prioridad de --tickers
        items = sorted(set(buckets.get(tk, [])), key=lambda od: od[1], reverse=True)  # día nuevo 1°
        missing = [(occ, d) for (occ, d) in items
                   if not (qm_dir / f"{occ.replace(':', '_')}_{d}.parquet").exists()]
        print(f"  {tk:5s}: {len(items):6d} contratos-día en barras · faltan NBBO: {len(missing):6d}"
              f" · ya cacheados: {len(items) - len(missing):6d}", flush=True)
        todo.extend(missing)

    miss = len(todo)
    print(f"  TOTAL faltan: {miss}", flush=True)
    if args.dry_run or miss == 0:
        print("  Nada que bajar — todo cacheado. ✔" if miss == 0 else "  (dry-run: no se bajó nada)",
              flush=True)
        return 0

    rate_per = max(1, args.rate // args.procs)
    done = empty = errc = 0
    t0 = _t.monotonic()   # monotónico: inmune a cambios de hora del sistema / hibernación
    print(f"  arrancando {args.procs} procesos · {rate_per}/min c/u ({rate_per * args.procs}/min total)...",
          flush=True)
    with ProcessPoolExecutor(max_workers=args.procs, initializer=_init_worker,
                             initargs=(rate_per, str(data_dir))) as ex:
        for i, (was_empty, err) in enumerate(ex.map(_job, todo, chunksize=8), 1):
            done += 1
            if was_empty:
                empty += 1
            if err:
                errc += 1
                if errc <= 20:
                    print(f"  ERR {err}", flush=True)
            if i % 200 == 0 or i == miss:
                el = _t.monotonic() - t0
                rate = i / el * 60 if el else 0
                eta = (miss - i) / (i / el) / 60 if (el and i) else 0
                print(f"  [{i:6d}/{miss}] {rate:.0f}/min · {empty} sin quotes · {errc} err"
                      f" · {el / 60:.1f}min · ETA {eta:.1f}min", flush=True)

    print(f"== listo: {done} procesados ({empty} sin quotes, {errc} errores) en"
          f" {(_t.monotonic() - t0) / 60:.1f} min ==", flush=True)
    return 0


if __name__ == "__main__":
    mp.freeze_support()   # Windows (spawn): inofensivo en scripts normales
    sys.exit(main())

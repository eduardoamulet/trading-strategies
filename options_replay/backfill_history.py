"""Backfill HISTÓRICO: extiende la data hacia ATRÁS (antes del cache actual) para 0DTE.

Para cada (ticker, día hábil) en [--start, --end] baja TODO lo necesario para backtestear
ese día: barras del subyacente, chain 0DTE, barras de opción de las ±N strikes ATM, y
—lo irreemplazable— la línea de bid/ask por minuto (quotes_minute) de esas opciones.

Pensado para correr ANTES de cancelar Polygon. El horizonte de NBBO del plan llega ~4 años
atrás (verificado: QQQ/SPY/IWM 0DTE con NBBO desde ~2022-06; 2022-01 ya no tiene). Multiproceso
(1 ticker-día por tarea), prioridad días NUEVOS primero (lo más relevante primero), resumible
(todo lo cacheado se saltea vía el Downloader idempotente).

Uso (desde options_replay/):
    py backfill_history.py --dry-run
    py backfill_history.py --tickers QQQ,SPY,IWM --start 2022-06-01 --end 2025-03-30 --procs 12
    py backfill_history.py ... --no-bars     # solo subyacente+chain+quotes_minute (NBBO), sin barras de opción
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sys
import time as _t
from concurrent.futures import ProcessPoolExecutor
from datetime import date as _date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import config  # type: ignore  # noqa: E402
import pandas as pd  # noqa: E402
from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402

_DL: Downloader | None = None
_STRIKES = 10
_BARS = True


def _init_worker(rate_per_proc: int, data_dir_str: str, strikes: int, bars: bool):
    global _DL, _STRIKES, _BARS
    _DL = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=rate_per_proc),
                     Path(data_dir_str))
    _STRIKES = strikes
    _BARS = bars


def _job(ticker_date):
    """Procesa un ticker-día completo. Devuelve (status, info). status ∈
    ok / nomkt (feriado-sin sesión) / no0dte (sin expiry ese día) / err."""
    ticker, date = ticker_date
    try:
        under = _DL.underlying(ticker, date)
        if under.empty:
            return ("nomkt", None)
        spot = float(under.iloc[0]["open"])
        chain = _DL.chain(ticker, date)            # 0DTE = contratos que expiran EXACTO ese día
        if chain.empty:
            return ("no0dte", None)
        n = 2 * _STRIKES + 1
        calls = chain[chain["contract_type"].str.lower() == "call"]
        puts = chain[chain["contract_type"].str.lower() == "put"]
        cs = calls.assign(_d=(calls["strike_price"] - spot).abs()).sort_values("_d").head(n)
        ps = puts.assign(_d=(puts["strike_price"] - spot).abs()).sort_values("_d").head(n)
        nq = 0
        for _, row in pd.concat([cs, ps]).iterrows():
            ct = "C" if str(row["contract_type"]).lower() == "call" else "P"
            occ = row.get("ticker") or PolygonAdapter.build_occ(ticker, date, ct, row["strike_price"])
            if _BARS:
                _DL.option(occ, date)              # barras de opción (reemplazable, pero útil)
            df = _DL.option_quote_series(occ, date)  # NBBO por minuto (lo IRREEMPLAZABLE)
            if df is not None and not df.empty:
                nq += 1
        return ("ok", nq)
    except Exception as e:  # noqa: BLE001 — un ticker-día que falla no frena al resto
        return ("err", f"{ticker} {date}: {e}")


def _weekdays(start: _date, end: _date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def main() -> int:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    cpu = os.cpu_count() or 4
    ap = argparse.ArgumentParser(description="Backfill histórico 0DTE (barras + NBBO por minuto).")
    ap.add_argument("--tickers", default="QQQ,SPY,IWM", help="CSV (default QQQ,SPY,IWM).")
    ap.add_argument("--start", default="2022-06-01", help="Fecha inicio YYYY-MM-DD (default 2022-06-01).")
    ap.add_argument("--end", default="2025-03-30", help="Fecha fin YYYY-MM-DD (default 2025-03-30).")
    ap.add_argument("--strikes", type=int, default=10, help="Strikes ATM por lado (default 10).")
    ap.add_argument("--procs", type=int, default=max(2, min(12, cpu - 2)),
                    help=f"Procesos (default {max(2, min(12, cpu - 2))}; tenés {cpu} cores).")
    ap.add_argument("--rate", type=int, default=1200, help="Llamadas/min TOTALES (default 1200).")
    ap.add_argument("--no-bars", action="store_true", help="Saltear barras de opción (solo NBBO).")
    ap.add_argument("--dry-run", action="store_true", help="Solo reporta el plan; no baja nada.")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    start = _date.fromisoformat(args.start)
    end = _date.fromisoformat(args.end)
    days = _weekdays(start, end)
    todo = [(tk, d) for d in sorted(days, reverse=True) for tk in tickers]   # días NUEVOS primero

    print(f"== backfill HISTÓRICO == {', '.join(tickers)} · {args.start}→{args.end} · "
          f"{len(days)} días háb. × {len(tickers)} = {len(todo)} ticker-días · ±{args.strikes} strikes · "
          f"{'SIN' if args.no_bars else 'CON'} barras de opción · {args.procs} procs", flush=True)
    if args.dry_run:
        per = (2 * args.strikes + 1) * (1 if args.no_bars else 2)
        print(f"  (dry-run) ~{len(todo) * per} llamadas de opción + ~{len(todo) * 2} de subyacente/chain "
              f"(cota alta; feriados y días sin 0DTE se saltean)", flush=True)
        return 0

    rate_per = max(1, args.rate // args.procs)
    data_dir = HERE / "data"
    t0 = _t.monotonic()
    ok = nomkt = no0dte = err = nq_tot = 0
    print(f"  arrancando {args.procs} procs · {rate_per}/min c/u...", flush=True)
    with ProcessPoolExecutor(max_workers=args.procs, initializer=_init_worker,
                             initargs=(rate_per, str(data_dir), args.strikes, not args.no_bars)) as ex:
        for i, (status, info) in enumerate(ex.map(_job, todo, chunksize=2), 1):
            if status == "ok":
                ok += 1
                nq_tot += (info or 0)
            elif status == "nomkt":
                nomkt += 1
            elif status == "no0dte":
                no0dte += 1
            else:
                err += 1
                if err <= 20:
                    print(f"  ERR {info}", flush=True)
            if i % 100 == 0 or i == len(todo):
                el = _t.monotonic() - t0
                rate = i / el * 60 if el else 0
                eta = (len(todo) - i) / (i / el) / 60 if (el and i) else 0
                print(f"  [{i}/{len(todo)}] {rate:.0f} tk-días/min · ok={ok} nomkt={nomkt} "
                      f"no0dte={no0dte} err={err} · NBBO_contratos={nq_tot} · {el / 60:.1f}min · "
                      f"ETA {eta:.1f}min", flush=True)

    print(f"== listo histórico: ok={ok} nomkt={nomkt} no0dte={no0dte} err={err} · "
          f"NBBO_contratos={nq_tot} · {(_t.monotonic() - t0) / 60:.1f} min ==", flush=True)
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())

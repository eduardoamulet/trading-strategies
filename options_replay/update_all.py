"""Actualiza la cache de datos (Parquet) de TODOS los tickers hasta el día actual.

Pensado para correr de fondo (Tarea Programada de Windows) → los datos "amanecen"
actualizados. Por cada ticker (de ticker_info.json), por cada día hábil de la ventana:
  - barras 1-min del subyacente
  - chain 0 DTE
  - barras 1-min de las (2N+1) strikes más cercanas a ATM por lado  [omitible]

Es INCREMENTAL: lo que ya está cacheado (parquet) se saltea; solo baja lo nuevo. La
ventana corta (--days) hace que un run diario solo agregue el/los día(s) nuevo(s).

Uso (desde options_replay/):
    py update_all.py [--days N] [--strikes N] [--underlying-only] [--tickers A,B,C]
Defaults: --days 5  --strikes 10  (full: subyacente + chain + opciones)

OJO: con todos los tickers + opciones hace MUCHAS llamadas a Polygon. Ajustá --days /
--strikes o usá --underlying-only (mucho más liviano, suficiente para los gráficos).
"""
from __future__ import annotations

import argparse
import json
import sys
import time as _t
from datetime import date as _date, datetime, time as _time, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import pandas as pd  # noqa: E402

import config  # type: ignore  # noqa: E402
from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402

# Tickers a SALTEAR: tu plan de Polygon no los cubre (403). Ej: el índice SPX (I:SPX)
# necesita el add-on de Índices. Quitá un ticker de acá si conseguís ese acceso.
SKIP_TICKERS = {"SPX"}


def _log(*a):
    print(*a, flush=True)


def _weekdays(start: _date, end: _date) -> list[str]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _last_complete_session() -> _date:
    """Último día con sesión COMPLETA, para NO cachear un 'hoy' parcial. Si ahora (ET) es
    después de ~16:15 (post-cierre), hoy cuenta; si no, el día hábil anterior. Los feriados
    no se filtran finos: si cae uno, Polygon devuelve vacío y simplemente no se cachea."""
    now_et = pd.Timestamp.now(tz="America/New_York")
    d = now_et.date()
    if now_et.time() < _time(16, 15):
        d = d - timedelta(days=1)
    while d.weekday() >= 5:   # retroceder sáb/dom
        d = d - timedelta(days=1)
    return d


def _is_incomplete_session(dl: Downloader, ticker: str, date_str: str) -> bool:
    """True si el subyacente cacheado de ese día quedó INCOMPLETO (típico cuando la tarea
    lo bajó de madrugada y solo trae barras pre-market). Heurística robusta: la última barra
    es ANTERIOR a las 10:00 ET → seguro incompleto (una sesión real siempre tiene barras de
    RTH bien pasadas las 10:00). Si no hay cache, NO es 'incompleto' (se baja normal). Se usa
    para auto-sanar parciales: re-bajar ese día con force=True."""
    path = dl.data_dir / "underlying" / f"{ticker}_{date_str}.parquet"
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path, columns=["timestamp"])
    except Exception:  # noqa: BLE001 — parquet ilegible/corrupto → re-bajar
        return True
    if df.empty:
        return True
    last = pd.to_datetime(df["timestamp"]).max()
    return last.time() < _time(10, 0)   # timestamp es tz-aware ET → hora de pared ET


def update_ticker(dl: Downloader, ticker: str, days: list[str], strikes_window: int,
                  underlying_only: bool) -> tuple[int, int, int]:
    """Actualiza un ticker en la ventana. Devuelve (días con datos, llamadas a opciones,
    días sanados). 'Sanado' = el día estaba cacheado incompleto (parcial) y se re-bajó con
    force para completarlo."""
    n_days, n_opt, n_healed = 0, 0, 0
    n = 2 * strikes_window + 1
    for date_str in days:
        # Si el día ya estaba cacheado pero parcial (p.ej. lo bajó la tarea pre-apertura),
        # se re-baja con force para completarlo. Días completos o faltantes → flujo normal.
        force = _is_incomplete_session(dl, ticker, date_str)
        if force:
            n_healed += 1
        under = dl.underlying(ticker, date_str, force=force)
        if under.empty:
            continue
        n_days += 1
        if underlying_only:
            continue
        spot_open = float(under.iloc[0]["open"])
        chain = dl.chain(ticker, date_str, force=force)
        if chain.empty:
            continue
        calls = chain[chain["contract_type"].str.lower() == "call"]
        puts = chain[chain["contract_type"].str.lower() == "put"]
        cs = calls.assign(_d=(calls["strike_price"] - spot_open).abs()).sort_values("_d").head(n)
        ps = puts.assign(_d=(puts["strike_price"] - spot_open).abs()).sort_values("_d").head(n)
        for _, row in pd.concat([cs, ps]).iterrows():
            ct = "C" if str(row["contract_type"]).lower() == "call" else "P"
            occ = row.get("ticker") or PolygonAdapter.build_occ(ticker, date_str, ct, row["strike_price"])
            dl.option(occ, date_str, force=force)
            n_opt += 1
    return n_days, n_opt, n_healed


def main() -> int:
    # Escribir el log SIEMPRE en UTF-8 (la consola de Windows usa cp1252 por defecto →
    # mojibake en '·', 'í', etc. al redirigir a archivo). reconfigure existe en Python 3.7+.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Actualiza la cache de todos los tickers hasta hoy.")
    ap.add_argument("--days", type=int, default=5, help="Días hábiles hacia atrás a revisar (default 5).")
    ap.add_argument("--strikes", type=int, default=10, help="Strikes ATM por lado (default 10).")
    ap.add_argument("--underlying-only", action="store_true", help="Solo subyacente (sin opciones).")
    ap.add_argument("--tickers", default="", help="CSV de tickers; vacío = todos (ticker_info.json).")
    args = ap.parse_args()

    info = json.loads((HERE / "ticker_info.json").read_text(encoding="utf-8"))
    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else sorted(info.keys()))
    _skipped = [t for t in tickers if t.upper() in SKIP_TICKERS]
    tickers = [t for t in tickers if t.upper() not in SKIP_TICKERS]
    if _skipped:
        _log(f"(salteados por acceso/plan: {', '.join(_skipped)})")

    end = _last_complete_session()   # NO cachear 'hoy' parcial (la tarea corre pre-apertura)
    start = end - timedelta(days=args.days + 4)   # +4 para cubrir fin de semana
    days = _weekdays(start, end)

    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY), HERE / "data")

    _log(f"== update_all == {len(tickers)} tickers · ventana {start}..{end} "
         f"({len(days)} días hábiles) · "
         f"{'SOLO subyacente' if args.underlying_only else f'subyacente+chain+opciones (±{args.strikes})'}")
    _started = datetime.now()
    t0 = _t.time()
    tot_opt = 0
    tot_healed = 0
    results = []
    for i, tk in enumerate(tickers, 1):
        try:
            n_days, n_opt, n_healed = update_ticker(dl, tk, days, args.strikes, args.underlying_only)
            tot_opt += n_opt
            tot_healed += n_healed
            results.append({"ticker": tk, "days": n_days, "opt": n_opt, "healed": n_healed, "error": None})
            _log(f"[{i:3d}/{len(tickers)}] {tk:6s} ok · {n_days} día(s) · {n_opt} opt"
                 f"{f' · {n_healed} sanado(s)' if n_healed else ''} · {(_t.time() - t0) / 60:.1f}min")
        except Exception as e:  # noqa: BLE001 — un ticker que falla no debe frenar al resto
            results.append({"ticker": tk, "days": 0, "opt": 0, "healed": 0, "error": str(e)})
            _log(f"[{i:3d}/{len(tickers)}] {tk:6s} ERROR: {e}")
    _dur = (_t.time() - t0) / 60
    _log(f"== listo en {_dur:.1f} min · {tot_opt} llamadas a opciones · {tot_healed} día(s) sanado(s) ==")
    # Reporte estructurado para la página de estado (Herramientas → Datos).
    try:
        status = {
            "started": _started.isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds"),
            "duration_min": round(_dur, 1),
            "window_start": str(start), "window_end": str(end),
            "mode": "underlying-only" if args.underlying_only else f"full (±{args.strikes})",
            "n_tickers": len(tickers), "total_opt_calls": tot_opt, "total_healed": tot_healed,
            "n_errors": sum(1 for r in results if r["error"]),
            "tickers": results,
        }
        (HERE / "data").mkdir(parents=True, exist_ok=True)
        (HERE / "data" / "update_status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _log(f"(no pude escribir update_status.json: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

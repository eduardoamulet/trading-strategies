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
from datetime import date as _date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import pandas as pd  # noqa: E402

import config  # type: ignore  # noqa: E402
from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402


def _log(*a):
    print(*a, flush=True)


def _weekdays(start: _date, end: _date) -> list[str]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def update_ticker(dl: Downloader, ticker: str, days: list[str], strikes_window: int,
                  underlying_only: bool) -> tuple[int, int]:
    """Actualiza un ticker en la ventana. Devuelve (días con datos, llamadas a opciones)."""
    n_days, n_opt = 0, 0
    n = 2 * strikes_window + 1
    for date_str in days:
        under = dl.underlying(ticker, date_str)
        if under.empty:
            continue
        n_days += 1
        if underlying_only:
            continue
        spot_open = float(under.iloc[0]["open"])
        chain = dl.chain(ticker, date_str)
        if chain.empty:
            continue
        calls = chain[chain["contract_type"].str.lower() == "call"]
        puts = chain[chain["contract_type"].str.lower() == "put"]
        cs = calls.assign(_d=(calls["strike_price"] - spot_open).abs()).sort_values("_d").head(n)
        ps = puts.assign(_d=(puts["strike_price"] - spot_open).abs()).sort_values("_d").head(n)
        for _, row in pd.concat([cs, ps]).iterrows():
            ct = "C" if str(row["contract_type"]).lower() == "call" else "P"
            occ = row.get("ticker") or PolygonAdapter.build_occ(ticker, date_str, ct, row["strike_price"])
            dl.option(occ, date_str)
            n_opt += 1
    return n_days, n_opt


def main() -> int:
    ap = argparse.ArgumentParser(description="Actualiza la cache de todos los tickers hasta hoy.")
    ap.add_argument("--days", type=int, default=5, help="Días hábiles hacia atrás a revisar (default 5).")
    ap.add_argument("--strikes", type=int, default=10, help="Strikes ATM por lado (default 10).")
    ap.add_argument("--underlying-only", action="store_true", help="Solo subyacente (sin opciones).")
    ap.add_argument("--tickers", default="", help="CSV de tickers; vacío = todos (ticker_info.json).")
    args = ap.parse_args()

    info = json.loads((HERE / "ticker_info.json").read_text(encoding="utf-8"))
    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else sorted(info.keys()))

    end = _date.today()
    start = end - timedelta(days=args.days + 4)   # +4 para cubrir fin de semana
    days = _weekdays(start, end)

    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY), HERE / "data")

    _log(f"== update_all == {len(tickers)} tickers · ventana {start}..{end} "
         f"({len(days)} días hábiles) · "
         f"{'SOLO subyacente' if args.underlying_only else f'subyacente+chain+opciones (±{args.strikes})'}")
    t0 = _t.time()
    tot_opt = 0
    for i, tk in enumerate(tickers, 1):
        try:
            n_days, n_opt = update_ticker(dl, tk, days, args.strikes, args.underlying_only)
            tot_opt += n_opt
            _log(f"[{i:3d}/{len(tickers)}] {tk:6s} ok · {n_days} día(s) · {n_opt} opt · "
                 f"{(_t.time() - t0) / 60:.1f}min")
        except Exception as e:  # noqa: BLE001 — un ticker que falla no debe frenar al resto
            _log(f"[{i:3d}/{len(tickers)}] {tk:6s} ERROR: {e}")
    _log(f"== listo en {(_t.time() - t0) / 60:.1f} min · {tot_opt} llamadas a opciones ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Pre-descarga las barras del SUBYACENTE a 30s y 15s para los tickers con resolución
fina habilitada (los 11 de TICKERS). Las OPCIONES se bajan on-demand durante el backtest
a la resolución elegida (son demasiadas para pre-bajar). Cachea per-day (mismo esquema que
el minuto, con sufijo _30s/_15s) y escribe data/resolutions_available.json — la app usa ese
archivo para habilitar/deshabilitar el toggle de 30s/15s por ticker.

Correr:  python options_replay/download_subsecond.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import config  # noqa: E402
from adapter_polygon import PolygonAdapter, _aggs_ticker  # noqa: E402
from downloader import Downloader  # noqa: E402

TICKERS = ["QQQ", "SPY", "IWM", "NVDA", "TSLA", "PLTR", "AMZN", "META", "MSFT", "GOOG", "AAPL"]
DATA = _HERE / "data"
UND = DATA / "underlying"
AVAIL = DATA / "resolutions_available.json"
# Días por chunk (bien por debajo del límite de 50000 barras/llamada de Polygon).
CHUNK_DAYS = {"30s": 12, "15s": 8}


def _minute_date_range(ticker: str):
    """(min, max) de los días con minuto cacheado (stems ticker_YYYY-MM-DD, sin sufijo)."""
    days = sorted(p.stem.split("_")[1] for p in UND.glob(f"{ticker}_*.parquet")
                  if p.stem.count("_") == 1)
    return (days[0], days[-1]) if days else (None, None)


def _fetch_split(ad: PolygonAdapter, ticker: str, res: str, start: str, end: str) -> int:
    """Baja el subyacente a `res` en chunks multi-día y lo parte en parquets per-day."""
    sfx, mult, span = Downloader.RES[res]
    pt = _aggs_ticker(ticker)
    cur, d1 = pd.Timestamp(start), pd.Timestamp(end)
    written = 0
    while cur <= d1:
        chunk_end = min(cur + pd.Timedelta(days=CHUNK_DAYS[res]), d1)
        data = ad._get(f"/v2/aggs/ticker/{pt}/range/{mult}/{span}/{cur.date()}/{chunk_end.date()}",
                       {"adjusted": "true", "sort": "asc", "limit": 50000})
        df = PolygonAdapter._bars_to_df(data.get("results", []))
        if not df.empty:
            df["_d"] = df["timestamp"].dt.strftime("%Y-%m-%d")
            for d, g in df.groupby("_d"):
                p = UND / f"{ticker}_{d}{sfx}.parquet"
                if not p.exists():
                    g.drop(columns=["_d"]).reset_index(drop=True).to_parquet(p, index=False)
                    written += 1
        cur = chunk_end + pd.Timedelta(days=1)
    return written


def _save_available(done: list):
    AVAIL.write_text(json.dumps({"tickers": done}, indent=2), encoding="utf-8")


def main():
    ad = PolygonAdapter(config.POLYGON_API_KEY)
    done = []
    for tk in TICKERS:
        s, e = _minute_date_range(tk)
        if not s:
            print(f"{tk}: SIN minuto cacheado → salto", flush=True)
            continue
        n30 = _fetch_split(ad, tk, "30s", s, e)
        n15 = _fetch_split(ad, tk, "15s", s, e)
        done.append(tk)
        _save_available(done)   # incremental: la app habilita el ticker apenas termina
        print(f"{tk} ({s} → {e}): 30s +{n30} días · 15s +{n15} días", flush=True)
    print(f"LISTO. {len(done)} tickers con 30s/15s: {done}", flush=True)


if __name__ == "__main__":
    main()

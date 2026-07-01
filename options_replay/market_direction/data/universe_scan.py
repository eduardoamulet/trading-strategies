"""Escáner de volatilidad del universo — recalcula el "gran movimiento" desde el cache de datos.

Mide el rango medio intradía (high-low)/close en RTH sobre los últimos N días, por ticker, y lo
relativiza a SPY. Es lo que dispara el botón «Actualizar» del panel de Configuración: reemplaza el
snapshot hardcodeado del `TickerProfile` por una medición fresca (persistida a JSON).
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

_TZ = "America/New_York"
_RTH_OPEN, _RTH_CLOSE = 9 * 60 + 30, 16 * 60


def _mean_range_pct(files: list, days: int):
    """(nº días usados, rango medio intradía %) sobre los últimos `days` archivos."""
    vals = []
    for f in sorted(files)[-days:]:
        try:
            df = pd.read_parquet(f, columns=["timestamp", "high", "low", "close"])
        except Exception:
            continue
        et = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(_TZ)
        mins = et.dt.hour * 60 + et.dt.minute
        rth = df[(mins >= _RTH_OPEN) & (mins < _RTH_CLOSE)]
        if rth.empty:
            continue
        c = float(rth["close"].iloc[-1])
        if c > 0:
            vals.append((float(rth["high"].max()) - float(rth["low"].min())) / c * 100)
    return (len(vals), float(np.mean(vals))) if vals else (0, None)


def scan_universe(underlying_dir, tickers, days: int = 60, as_of: str | None = None) -> dict:
    """Mide todos los `tickers` en `underlying_dir`. Devuelve
    {"_as_of":…, "_days":…, "metrics": {ticker: {daily_range_pct, vol_x_spy, n_days}}}.
    Tickers sin datos se omiten (el panel cae al snapshot para ésos)."""
    underlying_dir = Path(underlying_dir)
    metrics = {}
    for tk in tickers:
        n, r = _mean_range_pct(glob.glob(str(underlying_dir / f"{tk}_*.parquet")), days)
        if r is not None:
            metrics[tk] = {"daily_range_pct": round(r, 2), "n_days": n}
    base = metrics.get("SPY", {}).get("daily_range_pct")
    for d in metrics.values():
        d["vol_x_spy"] = round(d["daily_range_pct"] / base, 2) if base else None
    return {"_as_of": as_of, "_days": days, "metrics": metrics}


def save_scan(scan: dict, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")


def load_scan(path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None

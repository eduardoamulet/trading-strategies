"""Constructor de la tabla de calibración empírica — backtestea el motor SIN look-ahead.

Para cada (ticker, fecha, minuto) del set, corre `market_direction_engine` (causal: solo velas ≤ t)
y mide el RESULTADO usando el futuro REALIZADO (que NO entra en la señal): ¿el cierre del día se
movió en la dirección predicha? Agrupa por bucket de score → hit-rate = confianza CALIBRADA.

La tabla la consume `EmpiricalCalibrator`; `make_calibrator()` la usa automáticamente si existe.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from ..data.caching_provider import CachingProvider
from ..domain import Action

_TZ = "America/New_York"
_DEFAULT_TABLE = Path(__file__).resolve().parent / "calibration_table.json"


def _minute_grid(start: str, end: str, step: int) -> list:
    h1, m1 = (int(x) for x in start.split(":"))
    h2, m2 = (int(x) for x in end.split(":"))
    return [f"{x // 60:02d}:{x % 60:02d}" for x in range(h1 * 60 + m1, h2 * 60 + m2 + 1, step)]


def _outcome(action: Action, entry_price: float, date: str, t: str, full_session) -> bool | None:
    """¿Acertó la dirección al CIERRE del día? (futuro realizado, no usado en la señal).
    None si no hay velas después de t."""
    t_ts = pd.Timestamp(f"{date} {t}").tz_localize(_TZ)
    fut = full_session.df[full_session.df["timestamp"] > t_ts]
    if fut.empty:
        return None
    eod = float(fut["close"].iloc[-1])
    return eod > entry_price if action == Action.CALL else eod < entry_price


def build_calibration_table(provider, tickers, dates, *, window=("09:35", "14:30"),
                            step_min: int = 30, bucket_size: int = 5, min_samples: int = 12,
                            progress=None) -> dict:
    """Devuelve {"bucket_size", "fallback", "table": {bucket: hit_rate}, "n_total", "n_used"}."""
    from ..engine import market_direction_engine
    prov = CachingProvider(provider)
    minutes = _minute_grid(window[0], window[1], step_min)
    buckets = defaultdict(lambda: [0, 0])   # bucket → [aciertos, total]
    n_done, n_days = 0, len(dates)
    for date in dates:
        for tk in tickers:
            full = prov.session(tk, date)
            if full.empty:
                continue
            for t in minutes:
                sig = market_direction_engine(tk, date, t, provider=prov)
                if sig.action == Action.NO_TRADE:
                    continue
                won = _outcome(sig.action, sig.entry_price, date, t, full)
                if won is None:
                    continue
                b = int(sig.score // bucket_size) * bucket_size
                buckets[b][1] += 1
                buckets[b][0] += int(won)
        n_done += 1
        if progress:
            progress(n_done, n_days)
    table = {str(b): round(w / n, 3) for b, (w, n) in sorted(buckets.items()) if n >= min_samples}
    return {"bucket_size": bucket_size, "fallback": 0.5, "table": table,
            "n_total": sum(n for _, n in buckets.values()),
            "n_used": sum(n for _, (w, n) in buckets.items() if n >= min_samples),
            "by_bucket": {str(b): n for b, (w, n) in sorted(buckets.items())}}


def save_calibration_table(result: dict, path=None) -> Path:
    p = Path(path) if path else _DEFAULT_TABLE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return p

"""Clocks — adapters del puerto Clock.

BacktestClock itera una grilla de minutos (la resolución del cache de quotes/bars). En
vivo, LiveClock emitiría instantes reales con sleep(poll_sec) — mismo puerto, otra fuente
de tiempo → el loop de gestión (execution.run_straddle) no cambia."""
from __future__ import annotations

from typing import Any, Iterator

import pandas as pd

from ..ports import Clock


class BacktestClock(Clock):
    """Grilla de tiempo simulada. `step_min` = paso (1 min = la granularidad del cache
    de quotes). Itera de `start` a `end` inclusive."""

    def __init__(self, step_min: float = 1.0):
        self._step = pd.Timedelta(minutes=float(step_min or 1.0))

    def ticks(self, start: Any, end: Any) -> Iterator[Any]:
        t = pd.Timestamp(start)
        end = pd.Timestamp(end)
        while t <= end:
            yield t
            t = t + self._step


class LiveClock(Clock):
    """Tiempo REAL para paper/vivo: emite el instante actual cada `poll_sec` segundos hasta
    `end`. El loop de gestión (execution) no cambia — solo cambia de dónde sale el tiempo.
    `now_fn` se inyecta (default time.time→Timestamp) para poder testear sin reloj real."""

    def __init__(self, poll_sec: float = 3.0, tz: str = "America/New_York", now_fn=None):
        self._poll = float(poll_sec)
        self._tz = tz
        self._now = now_fn

    def _now_ts(self):
        if self._now is not None:
            return pd.Timestamp(self._now())
        import time
        return pd.Timestamp(time.time(), unit="s", tz="UTC").tz_convert(self._tz)

    def ticks(self, start: Any, end: Any) -> Iterator[Any]:
        import time
        end = pd.Timestamp(end)
        while True:
            now = self._now_ts()
            yield now
            if now >= end:
                break
            time.sleep(self._poll)

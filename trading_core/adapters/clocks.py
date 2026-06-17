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

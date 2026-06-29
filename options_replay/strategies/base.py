"""ABC de estrategias + esquema de señales. Una estrategia nueva = subclase con
`detect_signals(bars15m, ticker) -> DataFrame[SIGNAL_COLS]`."""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

SIGNAL_COLS = [
    "fecha", "hora_et", "ticker", "direccion",
    "open", "basis", "prevMidpoint", "trendline", "basisChangePct", "atr", "rango",
    "R1", "R2", "R3", "R4", "fuente",
]


class Strategy(ABC):
    name: str = "base"

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params(), **(params or {})}

    @staticmethod
    def default_params() -> dict:
        return {}

    @abstractmethod
    def detect_signals(self, bars15m: pd.DataFrame, ticker: str) -> pd.DataFrame:
        """bars15m: 15m RTH continuo (cols open/high/low/close/volume/timestamp/
        session_date/is_opening). Devuelve un DataFrame con SIGNAL_COLS."""
        ...

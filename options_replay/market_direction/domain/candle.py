"""Vela (`Candle`) y serie de sesión (`Session`).

`Session` es el contenedor de las velas 1-min de UNA sesión RTH. Su método `up_to(t)` es la
PIEDRA ANGULAR de la garantía anti-look-ahead: devuelve una sub-sesión con SOLO las velas cuyo
timestamp es ≤ t. Todo el cómputo de indicadores opera sobre una `Session` recortada con `up_to`,
así que es estructuralmente imposible que un feature vea el futuro.

`Session` envuelve un `DataFrame` (representación natural de los datos de Polygon) pero expone una
interfaz limpia (last, opening_range, arrays) para que las capas superiores no toquen pandas.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

_TZ = "America/New_York"
_COLS = ("timestamp", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Candle:
    """Una vela OHLCV de 1 minuto. Inmutable."""
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


class Session:
    """Serie causal de velas 1-min de una sesión RTH (09:30–16:00 ET), ordenada por timestamp."""

    def __init__(self, df: pd.DataFrame, ticker: str, date: str):
        # Normaliza: solo las columnas OHLCV, ordenadas, índice reseteado.
        _df = df[[c for c in _COLS if c in df.columns]].copy()
        _df["timestamp"] = pd.to_datetime(_df["timestamp"])
        self._df = _df.sort_values("timestamp").reset_index(drop=True)
        self.ticker = ticker
        self.date = date

    # ── básicos ──────────────────────────────────────────────────────────────
    @property
    def df(self) -> pd.DataFrame:
        return self._df

    def __len__(self) -> int:
        return len(self._df)

    @property
    def empty(self) -> bool:
        return self._df.empty

    @property
    def last(self) -> Candle:
        r = self._df.iloc[-1]
        return Candle(r["timestamp"], float(r["open"]), float(r["high"]),
                      float(r["low"]), float(r["close"]), float(r["volume"]))

    # ── causalidad: la sub-sesión hasta el minuto `t` (inclusive) ────────────
    def up_to(self, t) -> "Session":
        """Sub-sesión con las velas cuyo timestamp es ≤ `t`. `t` puede ser un Timestamp o «HH:MM»."""
        if self.empty:                       # sesión vacía → nada que cortar (evita comparar dtypes)
            return self
        ts = self._coerce(t)
        return Session(self._df[self._df["timestamp"] <= ts], self.ticker, self.date)

    def _coerce(self, t) -> pd.Timestamp:
        if isinstance(t, pd.Timestamp):
            return t
        ts = pd.Timestamp(f"{self.date} {t}")
        return ts.tz_localize(_TZ) if ts.tzinfo is None else ts.tz_convert(_TZ)

    # ── opening range: (high, low) de los primeros `minutes` minutos ──────────
    def opening_range(self, minutes: int) -> tuple[float, float]:
        if self.empty:
            return (np.nan, np.nan)
        start = self._df["timestamp"].iloc[0]
        win = self._df[self._df["timestamp"] < start + pd.Timedelta(minutes=minutes)]
        if win.empty:
            return (np.nan, np.nan)
        return (float(win["high"].max()), float(win["low"].min()))

    # ── accessors como arrays numpy (para los indicadores) ───────────────────
    @property
    def closes(self) -> np.ndarray:
        return self._df["close"].to_numpy(dtype=float)

    @property
    def highs(self) -> np.ndarray:
        return self._df["high"].to_numpy(dtype=float)

    @property
    def lows(self) -> np.ndarray:
        return self._df["low"].to_numpy(dtype=float)

    @property
    def opens(self) -> np.ndarray:
        return self._df["open"].to_numpy(dtype=float)

    @property
    def volumes(self) -> np.ndarray:
        return self._df["volume"].to_numpy(dtype=float)

    @property
    def timestamps(self) -> pd.Series:
        return self._df["timestamp"]

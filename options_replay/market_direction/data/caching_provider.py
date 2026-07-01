"""`CachingProvider` — memoiza `session`/`previous_session` por (ticker, fecha).

Envuelve cualquier `MarketDataProvider`. Sirve para el backtest de calibración (que evalúa muchos
minutos del mismo día): la sesión se lee una vez y se corta con `up_to(t)` para cada minuto, en
lugar de re-leer el parquet en cada llamada. Cumple el mismo Protocol.
"""
from __future__ import annotations

from ..domain import Session


class CachingProvider:
    def __init__(self, inner):
        self._inner = inner
        self._sess: dict = {}
        self._prev: dict = {}

    def session(self, ticker: str, date: str) -> Session:
        k = (ticker.upper(), date)
        if k not in self._sess:
            self._sess[k] = self._inner.session(ticker, date)
        return self._sess[k]

    def previous_session(self, ticker: str, date: str) -> Session:
        k = (ticker.upper(), date)
        if k not in self._prev:
            self._prev[k] = self._inner.previous_session(ticker, date)
        return self._prev[k]

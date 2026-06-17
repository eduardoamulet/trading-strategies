"""Puertos (interfaces abstractas) que el dominio necesita del mundo exterior.

Inversión de dependencias (la 'D' de SOLID): la lógica de negocio depende de estas
ABSTRACCIONES, no de proveedores concretos. Polygon (backtest), Tradier o Schwab (vivo)
las IMPLEMENTAN en adapters/. Migrar de backtest a paper o a producción = inyectar otra
implementación de estos puertos, SIN tocar selection.py / execution.py.

Unificación clave: todo acceso a datos es AS-OF un instante `at`. En backtest, `at` es el
tiempo simulado y el adapter resuelve el dato cacheado de ese momento; en vivo, el adapter
devuelve el último dato real (puede ignorar `at`). La MISMA firma sirve para ambos mundos.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, List, Optional

from .domain import Contract, Fill, OrderRequest, Quote


class MarketData(ABC):
    """Puerto de DATOS DE MERCADO (lectura). Implementaciones: PolygonBacktestData
    (lee parquet cacheado), TradierMarketData (REST en vivo), FakeMarketData (tests)."""

    @abstractmethod
    def underlying_price(self, ticker: str, at: Any) -> Optional[float]:
        """Precio del subyacente as-of `at` (para ATM/ITM). None si no hay dato."""

    @abstractmethod
    def chain(self, ticker: str, expiry: str, at: Any) -> List[Contract]:
        """Contratos del vencimiento `expiry` (sin cotizar; el quote se pide aparte)."""

    @abstractmethod
    def quote(self, occ: str, at: Any) -> Quote:
        """NBBO del contrato `occ` as-of `at`. Quote(None, None) si no hay."""

    @abstractmethod
    def nearest_expiry(self, ticker: str, on_or_after: str) -> Optional[str]:
        """Primer vencimiento >= `on_or_after` (YYYY-MM-DD). None si no hay."""


class Broker(ABC):
    """Puerto de EJECUCIÓN. Implementaciones: SimulatedBroker (llena contra el NBBO:
    compra al ask, vende al bid — modelo Fase 2), TradierBroker / SchwabBroker (vivo)."""

    @abstractmethod
    def execute(self, order: OrderRequest, at: Any) -> Fill:
        """Ejecuta y devuelve el Fill (prima efectiva). En vivo bloquea hasta llenarse o
        cancelarse; en backtest llena al instante contra el NBBO de `at`."""


class Clock(ABC):
    """Puerto de TIEMPO. Unifica el loop de gestión: backtest itera la grilla de minutos
    de la sesión; vivo emite instantes de polling en tiempo real (con sleep)."""

    @abstractmethod
    def ticks(self, start: Any, end: Any) -> Iterable[Any]:
        """Itera instantes desde `start` hasta `end` (inclusive). El paso lo define la
        implementación (1 min en backtest; poll_sec en vivo)."""

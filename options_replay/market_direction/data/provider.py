"""`MarketDataProvider` — la interface (Protocol) de acceso a datos del motor.

El engine depende de ESTA abstracción, no de Polygon ni del Downloader. Mañana cambiás de fuente
(otro broker, un CSV, un mock de tests) implementando este Protocol, sin tocar el engine ni los
indicadores. Es el principio de Inversión de Dependencias (la D de SOLID).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain import Session


@runtime_checkable
class MarketDataProvider(Protocol):
    """Provee sesiones RTH (09:30–16:00 ET) de velas 1-min, listas para el dominio."""

    def session(self, ticker: str, date: str) -> Session:
        """Sesión RTH del `date` para `ticker`. Vacía si no hay datos (día sin mercado / sin cache)."""
        ...

    def previous_session(self, ticker: str, date: str) -> Session:
        """Sesión RTH del día HÁBIL anterior con datos (para gap, máximo/mínimo del día previo)."""
        ...

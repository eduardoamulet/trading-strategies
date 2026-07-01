"""Enumeraciones del dominio: la acción de trade, el sesgo de tendencia y la fuerza del mercado.

Se usan `str, Enum` para que sean serializables a JSON y comparables con strings sin fricción
(p.ej. `signal.action == "CALL"`), pero con la seguridad de tipos de un Enum.
"""
from __future__ import annotations

from enum import Enum


class Action(str, Enum):
    """Decisión final del motor."""
    CALL = "CALL"
    PUT = "PUT"
    NO_TRADE = "NO TRADE"


class Trend(str, Enum):
    """Sesgo direccional del mercado en el minuto evaluado."""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Strength(str, Enum):
    """Interpretación del score 0–100 en 5 bandas (igual que el medidor visual)."""
    MUY_BAJISTA = "MUY BAJISTA"     # 0–20
    BAJISTA = "BAJISTA"             # 21–40
    NEUTRAL = "NEUTRAL"            # 41–60
    ALCISTA = "ALCISTA"           # 61–80
    MUY_ALCISTA = "MUY ALCISTA"   # 81–100

    @classmethod
    def from_score(cls, score: float) -> "Strength":
        """Mapea un score 0–100 a su banda. Topes inclusivos en el límite superior de cada banda."""
        if score <= 20:
            return cls.MUY_BAJISTA
        if score <= 40:
            return cls.BAJISTA
        if score <= 60:
            return cls.NEUTRAL
        if score <= 80:
            return cls.ALCISTA
        return cls.MUY_ALCISTA

    def to_trend(self) -> Trend:
        """Sesgo de tendencia derivado de la banda de fuerza."""
        if self in (Strength.ALCISTA, Strength.MUY_ALCISTA):
            return Trend.BULLISH
        if self in (Strength.BAJISTA, Strength.MUY_BAJISTA):
            return Trend.BEARISH
        return Trend.NEUTRAL

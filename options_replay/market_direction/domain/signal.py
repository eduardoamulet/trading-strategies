"""`DirectionScore` (salida del modelo) y `TradeSignal` (salida final del motor).

`DirectionScore` es lo que devuelve cualquier `DirectionModel` (regla o ML): un score 0–100 + las
razones + el desglose de contribuciones (auditable). El `SignalGenerator` lo combina con las
condiciones y los niveles para producir el `TradeSignal` que consume la UI / el caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .enums import Action, Trend


@dataclass(frozen=True, slots=True)
class DirectionScore:
    """Salida de un `DirectionModel`. `score` 0–100 (50 neutral, →100 alcista, →0 bajista)."""
    score: float
    reasons: list[str] = field(default_factory=list)
    contributions: dict = field(default_factory=dict)   # regla → puntos (para auditar el score)


@dataclass(frozen=True, slots=True)
class TradeSignal:
    """Decisión final del motor para un (ticker, fecha, minuto). Inmutable y serializable.

    `action` ∈ {CALL, PUT, NO TRADE}. Cuando es NO TRADE, los campos de niveles son None.
    `market_strength` == `score` (0–100, fuerza direccional). `confidence` (0–1) es la confianza.
    """
    action: Action
    confidence: float            # 0–1
    market_strength: float       # 0–100  (= score; "fuerza" del mercado)
    trend: Trend
    score: float                 # 0–100  (score crudo del modelo)
    entry_time: str | None = None
    entry_price: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_reward: float | None = None
    reasons: list[str] = field(default_factory=list)
    ticker: str = ""
    date: str = ""

    def to_dict(self) -> dict:
        """Representación serializable (para JSON / la UI / logs)."""
        return {
            "action": self.action.value,
            "confidence": round(self.confidence, 4),
            "market_strength": round(self.market_strength, 1),
            "trend": self.trend.value,
            "score": round(self.score, 1),
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "stop": self.stop,
            "target": self.target,
            "risk_reward": self.risk_reward,
            "reasons": list(self.reasons),
            "ticker": self.ticker,
            "date": self.date,
        }

    @classmethod
    def no_trade(cls, score: float, confidence: float, trend: Trend,
                 reasons: list[str], ticker: str = "", date: str = "") -> "TradeSignal":
        """Constructor de conveniencia para una señal NO TRADE (sin niveles)."""
        return cls(action=Action.NO_TRADE, confidence=confidence, market_strength=score,
                   trend=trend, score=score, reasons=reasons, ticker=ticker, date=date)

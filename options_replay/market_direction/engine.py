"""`market_direction_engine` — el orquestador y punto de entrada público del motor.

Ensambla todo el pipeline CAUSAL (nada mira más allá de `start_time`):

    provider → FeatureBuilder → dirección tentativa → confirmación cross-asset
             → RuleBasedModel.predict → SignalGenerator → TradeSignal

Las 3 costuras (provider · model · calibrator vía el generator) se inyectan; los defaults cablean
el stack estándar. Cambiar cualquiera es una línea, sin tocar el resto.
"""
from __future__ import annotations

from typing import Optional

from .confirmation import CrossAssetConfirmation
from .data import default_provider
from .decision import SignalGenerator
from .domain import Strength, TradeSignal, Trend
from .indicators import FeatureBuilder
from .model import RuleBasedModel

_FB = FeatureBuilder()

# Versión de la LÓGICA del motor de dirección. La señal histórica es DETERMINISTA por
# (ticker, fecha, hora) para una versión dada → options_replay/md_cache.py la cachea con esta
# clave. SUBILA al cambiar indicadores/modelo/confirmación: invalida el cache automáticamente.
MD_ENGINE_VERSION = "2026-07-04"


def market_direction_engine(ticker: str, date: str, start_time: str, *,
                            provider=None, model=None, generator=None) -> TradeSignal:
    """Evalúa la dirección de `ticker` el `date` a las `start_time` (HH:MM). Devuelve un `TradeSignal`
    (CALL/PUT/NO TRADE + fuerza + confianza + niveles). Sin look-ahead: usa solo velas ≤ start_time."""
    ticker = str(ticker).upper().strip()
    provider = provider or default_provider()
    model = model or RuleBasedModel()
    generator = generator or SignalGenerator()
    try:
        session = provider.session(ticker, date).up_to(start_time)
        if session.empty:
            return TradeSignal.no_trade(
                50.0, 0.0, Trend.NEUTRAL,
                [f"Sin datos para {ticker} {date} ≤ {start_time}"], ticker, date)

        prev = provider.previous_session(ticker, date)
        features = _FB.build(session, prev)

        # 1) dirección TENTATIVA (solo features del propio activo)
        tentative = model.predict(features, None)
        tentative_trend = Strength.from_score(tentative.score).to_trend()
        # 2) confirmación cross-asset PARA esa dirección tentativa (SPY ancla / breadth)
        confirmation = CrossAssetConfirmation(provider).confirm(ticker, tentative_trend, date, start_time)
        # 3) score FINAL, ya con la confirmación
        score = model.predict(features, confirmation)
        # 4) decisión (acción + niveles + confianza)
        return generator.generate(score, features, confirmation, ticker=ticker, date=date)
    except Exception as e:   # el motor nunca debe tumbar la UI
        return TradeSignal.no_trade(50.0, 0.0, Trend.NEUTRAL, [f"Error: {e}"], ticker, date)


# Alias semántico: evaluar un activo en un minuto puntual.
evaluate_at_minute = market_direction_engine

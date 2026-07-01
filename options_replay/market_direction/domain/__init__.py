"""domain — entidades y contratos del motor. CERO dependencias de infraestructura (ni Polygon,
ni Streamlit, ni el engine). Es el núcleo estable que todas las demás capas consumen.
"""
from .enums import Action, Strength, Trend
from .candle import Candle, Session
from .features import FeatureSet
from .confirmation import Confirmation
from .signal import DirectionScore, TradeSignal
from . import ticker_profile
from .ticker_profile import AssetClass, ZeroDTE, TickerProfile

__all__ = ["Action", "Trend", "Strength", "Candle", "Session", "FeatureSet",
           "Confirmation", "DirectionScore", "TradeSignal",
           "AssetClass", "ZeroDTE", "TickerProfile", "ticker_profile"]

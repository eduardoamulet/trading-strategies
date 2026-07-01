"""data — capa de acceso a datos (infraestructura).

`MarketDataProvider` (interface) es la COSTURA de datos: el engine no sabe de Polygon ni del
Downloader. `CachedMarketDataProvider` la implementa leyendo el cache de Polygon (SPY/QQQ),
filtrando a RTH y convirtiendo a hora de NY, y entregando `Session` del dominio.
"""
from .provider import MarketDataProvider
from .cached_provider import CachedMarketDataProvider, default_provider

__all__ = ["MarketDataProvider", "CachedMarketDataProvider", "default_provider"]

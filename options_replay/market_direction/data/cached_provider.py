"""`CachedMarketDataProvider` — implementación de `MarketDataProvider` sobre el Downloader existente.

Lee el cache de Polygon (parquets 1-min en `data/underlying/`), **filtra a RTH (09:30–16:00 ET)**,
convierte el timestamp a hora de Nueva York y arma una `Session` del dominio. El Downloader se
INYECTA (DI) → testeable con un mock, y reutiliza todo el caching/offline ya existente.

Para SPY/QQQ los datos ya están cacheados; no toca la API.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..domain import Session

_TZ = "America/New_York"
_RTH_OPEN, _RTH_CLOSE = 9 * 60 + 30, 16 * 60   # minutos desde medianoche: [09:30, 16:00)
_EMPTY = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


def _to_session(df: pd.DataFrame, ticker: str, date: str) -> Session:
    """DataFrame crudo del Downloader → Session RTH en hora de NY. Robusto a tz-aware/naive."""
    if df is None or df.empty or "timestamp" not in df.columns:
        return Session(_EMPTY.copy(), ticker, date)
    out = df.copy()
    # utc=True interpreta naive como UTC y respeta tz-aware → siempre obtenemos ET correcto.
    et = pd.to_datetime(out["timestamp"], utc=True).dt.tz_convert(_TZ)
    mins = et.dt.hour * 60 + et.dt.minute
    mask = (mins >= _RTH_OPEN) & (mins < _RTH_CLOSE)
    out = out.loc[mask].copy()
    out["timestamp"] = et.loc[mask]
    return Session(out, ticker, date)


def _prev_business_days(date: str, lookback: int = 6) -> list:
    """Hasta `lookback` días HÁBILES anteriores a `date` (sin fines de semana; feriados se filtran
    por ausencia de datos aguas arriba)."""
    d = pd.Timestamp(date)
    days, cur = [], d
    for _ in range(lookback):
        cur = cur - pd.tseries.offsets.BDay(1)
        days.append(cur.date().isoformat())
    return days


class CachedMarketDataProvider:
    """Provider sobre el Downloader (cache de Polygon). Cumple el Protocol `MarketDataProvider`."""

    def __init__(self, downloader):
        self._dl = downloader

    def session(self, ticker: str, date: str) -> Session:
        try:
            df = self._dl.underlying(ticker, date)
        except Exception:
            df = None
        return _to_session(df, ticker, date)

    def previous_session(self, ticker: str, date: str) -> Session:
        """Primer día hábil anterior con datos (salta feriados/fines de semana sin cache)."""
        for d in _prev_business_days(date):
            sess = self.session(ticker, d)
            if not sess.empty:
                return sess
        return Session(_EMPTY.copy(), ticker, date)


def default_provider(data_dir=None) -> CachedMarketDataProvider:
    """Factory: arma un Downloader (config + adapter de Polygon) apuntando al cache del proyecto."""
    import sys
    _opts = Path(__file__).resolve().parents[2]    # options_replay/
    _root = _opts.parent                           # Traiding/  (config.py vive acá)
    for _p in (str(_opts), str(_root)):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    import config
    from adapter_polygon import PolygonAdapter
    from downloader import Downloader
    dl = Downloader(PolygonAdapter(getattr(config, "POLYGON_API_KEY", ""), rate_limit_per_min=600),
                    Path(data_dir) if data_dir else _opts / "data")
    return CachedMarketDataProvider(dl)

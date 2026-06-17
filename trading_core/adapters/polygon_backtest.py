"""PolygonBacktestData — adapter del puerto MarketData para backtest.

Envuelve un `Downloader` (la capa de cache parquet sobre Polygon) que se INYECTA en el
constructor. No importa `options_replay` ni `Downloader` → el adapter solo usa su interfaz
(duck typing): underlying(t,d), chain(t,exp), option_quote(occ,d,ts), nearest_expiry(t,d).
Así el dominio (trading_core) queda 100% desacoplado del proyecto de backtest.

Resuelve los datos AS-OF `at`: el subyacente = open de la barra del minuto de `at`; el
quote = NBBO cacheado de ese minuto. Para vivo se inyecta otro MarketData (Tradier/Schwab)
que devuelve lo último real — misma interfaz, cero cambios en la lógica de negocio."""
from __future__ import annotations

from typing import Any, List, Optional

import pandas as pd

from ..domain import Contract, Quote, Right
from ..ports import MarketData


def _date_of(at: Any) -> str:
    return pd.Timestamp(at).strftime("%Y-%m-%d")


class PolygonBacktestData(MarketData):
    def __init__(self, downloader: Any):
        self._dl = downloader
        self._under_memo: dict = {}   # (ticker, date) -> DataFrame
        self._chain_memo: dict = {}   # (ticker, expiry) -> list[Contract]

    def _underlying_df(self, ticker: str, date: str):
        key = (ticker, date)
        if key not in self._under_memo:
            self._under_memo[key] = self._dl.underlying(ticker, date)
        return self._under_memo[key]

    def underlying_price(self, ticker: str, at: Any) -> Optional[float]:
        df = self._underlying_df(ticker, _date_of(at))
        if df is None or df.empty:
            return None
        at = pd.Timestamp(at)
        future = df[df["timestamp"] >= at]
        if not future.empty:
            return float(future.iloc[0]["open"])
        return float(df.iloc[-1]["close"])   # `at` después del cierre → último precio

    def chain(self, ticker: str, expiry: str, at: Any) -> List[Contract]:
        key = (ticker, expiry)
        if key not in self._chain_memo:
            df = self._dl.chain(ticker, expiry)
            out: List[Contract] = []
            if df is not None and not df.empty:
                for _, r in df.iterrows():
                    ct = str(r.get("contract_type", "")).lower()
                    right = Right.CALL if ct == "call" else Right.PUT
                    out.append(Contract(
                        occ=str(r.get("ticker") or ""),
                        underlying=ticker,
                        expiry=str(r.get("expiration_date") or expiry),
                        strike=float(r.get("strike_price") or 0.0),
                        right=right))
            self._chain_memo[key] = out
        return self._chain_memo[key]

    def quote(self, occ: str, at: Any) -> Quote:
        try:
            q = self._dl.option_quote(occ, _date_of(at), pd.Timestamp(at))
        except Exception:
            q = {}
        return Quote(bid=q.get("bid"), ask=q.get("ask"), ts=at,
                     bid_size=q.get("bid_size"), ask_size=q.get("ask_size"))

    def nearest_expiry(self, ticker: str, on_or_after: str) -> Optional[str]:
        try:
            return self._dl.nearest_expiry(ticker, on_or_after)
        except Exception:
            return None

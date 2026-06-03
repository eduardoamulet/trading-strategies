"""Interfaz abstracta de broker. Cambiar de Tradier a Alpaca = nueva impl acá."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from core.models import (
    Account,
    Contract,
    Fill,
    OrderRequest,
    OrderResult,
    Quote,
)


class BrokerError(Exception):
    pass


class BrokerAdapter(ABC):
    @abstractmethod
    def nearest_expiry(self, ticker: str) -> Optional[str]:
        """Expiración más cercana disponible (0DTE si existe)."""

    @abstractmethod
    def get_option_chain(self, ticker: str, expiry: Optional[str] = None) -> list[Contract]:
        """Chain completo con bid/ask/vol/oi/delta."""

    @abstractmethod
    def get_underlying_price(self, ticker: str) -> float:
        """Último precio del subyacente (para ATM/ITM)."""

    @abstractmethod
    def get_quote(self, occ_symbol: str) -> Quote:
        """NBBO + greeks del contrato."""

    @abstractmethod
    def get_account(self) -> Account:
        """Equity / buying power."""

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResult:
        """Envía orden LIMIT. Devuelve order_id."""

    @abstractmethod
    def get_order(self, order_id: str) -> Fill:
        """Estado de la orden (fills parciales incluidos)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    def stream_quotes(self, symbols: list[str], on_quote: Callable[[Quote], None],
                      stop_flag: Callable[[], bool], poll_sec: float = 3.0) -> None:
        """Default: polling loop (robusto, funciona en sandbox). Subclases pueden
        sobreescribir con WebSocket real. Llama on_quote(quote) por símbolo cada
        poll_sec hasta que stop_flag() sea True."""
        import time
        while not stop_flag():
            for occ in list(symbols):
                try:
                    on_quote(self.get_quote(occ))
                except Exception:
                    pass
            time.sleep(poll_sec)

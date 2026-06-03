"""Modelos de dominio (dataclasses puras, sin lógica de I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Contract:
    """Un contrato de opción del chain, con quote y greeks (si disponibles)."""
    occ: str                # OCC symbol, ej. "AAPL260619C00190000"
    underlying: str
    strike: float
    expiry: str             # YYYY-MM-DD
    right: str              # "C" | "P"
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    delta: Optional[float] = None

    @property
    def spread(self) -> float:
        return abs(self.ask - self.bid)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if (self.bid and self.ask) else self.last


@dataclass(frozen=True)
class Quote:
    occ: str
    bid: float
    ask: float
    last: float
    volume: int = 0
    open_interest: int = 0
    delta: Optional[float] = None
    ts: Optional[datetime] = None

    @property
    def spread(self) -> float:
        return abs(self.ask - self.bid)


@dataclass
class OrderRequest:
    occ: str
    underlying: str
    side: str               # "buy_to_open" | "sell_to_close"
    qty: int
    type: str = "limit"     # SIEMPRE limit (nunca market)
    limit_price: float = 0.0
    duration: str = "day"
    tag: str = ""           # idempotency key


@dataclass
class OrderResult:
    order_id: str
    status: str             # "ok" | "rejected" | "error"
    raw: dict = field(default_factory=dict)


@dataclass
class Fill:
    order_id: str
    status: str             # "filled" | "partial" | "pending" | "canceled" | "rejected"
    filled_qty: int
    avg_price: float
    time: Optional[datetime] = None


@dataclass
class Position:
    occ: str
    underlying: str
    qty: int
    entry_price: float      # prima por contrato
    entry_time: datetime
    cost_total: float       # entry_price * qty * 100 (+ comisiones)
    order_id: str
    roi_target_pct: float
    side: str = "buy_to_open"
    tp_armed: bool = False
    # live (actualizado por el monitor)
    current_price: float = 0.0
    roi_pct: float = 0.0
    pnl: float = 0.0
    status: str = "open"    # "open" | "closing" | "closed"


@dataclass
class Account:
    equity: float
    buying_power: float
    option_buying_power: float = 0.0

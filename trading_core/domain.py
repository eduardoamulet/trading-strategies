"""Modelos de dominio — value objects PUROS, sin I/O ni dependencias de proveedor.

La lógica de negocio (selección, ejecución, gestión de posición, decisión) depende SOLO
de estos modelos y de los puertos (ports.py). Ningún proveedor concreto (Polygon, Tradier,
Schwab, …) aparece acá → el dominio es reusable en backtest, paper y vivo sin cambios.

SOLID: estos son los DTOs estables que cruzan las fronteras. Los adapters traducen el
formato de cada proveedor (parquet de Polygon, JSON de Tradier, …) HACIA estos modelos."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional


class Right(str, Enum):
    """Tipo de opción."""
    CALL = "C"
    PUT = "P"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Quote:
    """NBBO (bid/ask) de un contrato en un instante. `ts` es el momento del quote."""
    bid: Optional[float]
    ask: Optional[float]
    ts: Any = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return abs(self.ask - self.bid)

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class Contract:
    """Un contrato de opción. `quote` es opcional (se adjunta cuando se cotiza)."""
    occ: str
    underlying: str
    expiry: str               # YYYY-MM-DD
    strike: float
    right: Right
    quote: Optional[Quote] = None
    open_interest: int = 0
    volume: int = 0

    def ask(self) -> Optional[float]:
        return self.quote.ask if self.quote else None

    def spread(self) -> Optional[float]:
        return self.quote.spread if self.quote else None


@dataclass(frozen=True)
class OrderRequest:
    """Orden a ejecutar. `limit=None` = a mercado (el broker simulado usa el NBBO)."""
    occ: str
    side: OrderSide
    qty: int
    limit: Optional[float] = None
    ts: Any = None


@dataclass(frozen=True)
class Fill:
    """Resultado de una ejecución: prima POR ACCIÓN efectivamente pagada/cobrada."""
    occ: str
    side: OrderSide
    qty: int
    price: float
    ts: Any = None


@dataclass
class Leg:
    """Una pierna abierta (ej. la CALL de un straddle). Mutable: el qty puede crecer
    si en el futuro se agrega martingala/refuerzo (otro tranche al mismo occ)."""
    contract: Contract
    qty: int
    entry_price: float        # prima por acción al abrir
    entry_ts: Any

    def cost(self) -> float:
        """Costo total de la pierna ($) = prima × qty × 100."""
        return self.qty * self.entry_price * 100.0

    def value_at(self, mark: float) -> float:
        """Valor de liquidación ($) a una prima `mark` por acción."""
        return self.qty * mark * 100.0


@dataclass
class Position:
    """Una posición = varias piernas/tranches. Cada refuerzo agrega un Leg (otro tranche)
    al MISMO contrato (occ) → soporta martingala sin re-arquitecturar. Las piernas se
    agrupan por `Right` (CALL/PUT) para el ROI por pierna."""
    legs: List[Leg] = field(default_factory=list)
    reinforcements: List[dict] = field(default_factory=list)   # [{ts, right, price, qty}, …]

    def add(self, leg: Leg) -> None:
        self.legs.append(leg)

    def rights(self) -> set:
        return {l.contract.right for l in self.legs}

    def cost(self) -> float:
        return sum(l.cost() for l in self.legs)

    def cost_of(self, right: "Right") -> float:
        return sum(l.cost() for l in self.legs if l.contract.right == right)

    def value_of(self, right: "Right", mark: float) -> float:
        """Valor de liquidación de TODAS las tranches de `right` a la prima `mark`."""
        return sum(l.value_at(mark) for l in self.legs if l.contract.right == right)

    def qty_of(self, right: "Right") -> int:
        return sum(l.qty for l in self.legs if l.contract.right == right)

    def contract_of(self, right: "Right") -> Optional[Contract]:
        for l in self.legs:
            if l.contract.right == right:
                return l.contract
        return None


@dataclass
class TradeResult:
    """Resultado de una operación completa (entrada → salida)."""
    underlying: str
    entry_ts: Any
    exit_ts: Any
    legs: List[Leg]
    entry_cost: float
    exit_proceeds: float
    exit_reason: str          # 'take_profit' | 'stop_loss' | 'session_end'
    marks: List[float] = field(default_factory=list)        # ROI(%) por tick (para gráficos)
    reinforcements: List[dict] = field(default_factory=list)  # eventos de refuerzo (martingala)

    @property
    def pnl(self) -> float:
        return self.exit_proceeds - self.entry_cost

    @property
    def roi(self) -> float:
        return (self.pnl / self.entry_cost) if self.entry_cost else 0.0


class NoContractError(Exception):
    """No se encontró ningún contrato que cumpla los criterios (dentro de la ventana)."""

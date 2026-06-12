"""Puente ALERTA → ENTRADA en vivo (paper).

Toma una alerta del Historial de Señales (símbolo + CALL/PUT + parámetros del usuario)
y abre UNA posición usando exactamente las mismas piezas que ya existen:
  - ContractSelector  → elige el contrato (misma filosofía que el backtest: spread+ATM/ITM)
  - RiskGuard         → valida PRE-orden (mercado abierto, spread, OI, costo, breakers)
  - OrderManager.buy  → limit @ ask, fills parciales, idempotencia
Luego el daemon (PositionMonitor) monitorea y vende al Umbral de ROI / EOD.

NO opera en real: respeta settings.LIVE_TRADING_ENABLED (False = sandbox/paper).
Esta capa NO conoce Polygon ni el backtest — separa simulación de ejecución.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from brokers.base import BrokerAdapter
from core.models import Contract, Position
from core.order_manager import OrderManager
from core.risk import RiskGuard
from core.selector import ContractSelector, NoLiquidContract
from core.store import Store


class EntryError(Exception):
    """No se pudo abrir la posición para la alerta (con el motivo)."""


@dataclass
class AlertEntry:
    alert_id: str          # id de la alerta (idempotencia)
    underlying: str        # símbolo (ej. "AAPL")
    side: str              # "CALL" | "PUT"  (← Tipo de la alerta)
    inversion: float       # USD a invertir en esa pierna
    roi_target_pct: float  # Umbral de ROI (%) → take-profit automático
    strategy: str = "atm"  # "atm" (más cercano al spot) | "itm" (1-ITM)
    arm_tp: bool = True    # armar el take-profit al comprar → el daemon vende solo al Umbral


@dataclass
class AlertPreview:
    """Resultado de un dry-run (NO se compró nada). status: 'ok'|'blocked'|'error'."""
    status: str
    underlying: str
    side: str
    occ: str = ""
    strike: float = 0.0
    expiry: str = ""
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    open_interest: int = 0
    qty: int = 0
    cost: float = 0.0
    reasons: list = field(default_factory=list)   # motivos de bloqueo/error


def _resolve_contract(broker: BrokerAdapter, selector: ContractSelector, e: AlertEntry):
    """Resuelve spot/expiry/chain/contrato/qty para la alerta. Lanza EntryError si no
    se puede. NO compra, NO valida cuenta → compartido por preview y entrada (sin drift).
    Usa el vencimiento más cercano (0DTE si existe ese día)."""
    side = (e.side or "").upper().strip()
    if side not in ("CALL", "PUT"):
        raise EntryError(f"Tipo inválido '{e.side}' (esperaba CALL o PUT).")
    if not e.underlying:
        raise EntryError("Falta el símbolo del subyacente.")

    spot = broker.get_underlying_price(e.underlying)
    expiry = broker.nearest_expiry(e.underlying)
    if not expiry:
        raise EntryError(f"{e.underlying}: sin vencimientos disponibles.")
    chain = broker.get_option_chain(e.underlying, expiry)
    if not chain:
        raise EntryError(f"{e.underlying}: chain vacío para {expiry}.")

    try:
        contract = selector.select(chain, side, spot, strategy=e.strategy)
    except NoLiquidContract as ex:
        raise EntryError(str(ex)) from ex

    unit = contract.ask * 100.0
    qty = int(e.inversion // unit) if unit > 0 else 0
    if qty < 1:
        raise EntryError(
            f"Inversión ${e.inversion:,.0f} no alcanza para 1 contrato de "
            f"{e.underlying} {side} (ask ${contract.ask:.2f} → ${unit:,.0f} c/u).")
    return side, spot, expiry, contract, qty


def preview_from_alert(broker: BrokerAdapter, store: Store, selector: ContractSelector,
                       risk: RiskGuard, e: AlertEntry) -> AlertPreview:
    """DRY-RUN: resuelve el contrato + costo + validación de riesgo SIN colocar orden.
    Devuelve un AlertPreview (no lanza). 'ok' = lista; 'blocked' = pasa selección pero
    falla riesgo/idempotencia; 'error' = no se pudo resolver el contrato."""
    try:
        side, spot, expiry, contract, qty = _resolve_contract(broker, selector, e)
    except EntryError as ex:
        return AlertPreview(status="error", underlying=e.underlying,
                            side=(e.side or "").upper(), reasons=[str(ex)])

    reasons: list[str] = []
    existing = store.get_position(contract.occ)
    if existing and existing.get("status") == "open":
        reasons.append(f"Ya hay una posición abierta en {contract.occ}.")
    try:
        account = broker.get_account()
        reasons.extend(risk.validate_entry(contract, qty, account))
    except Exception as ex:
        reasons.append(f"No se pudo validar la cuenta: {ex}")

    return AlertPreview(
        status="blocked" if reasons else "ok",
        underlying=e.underlying, side=side, occ=contract.occ, strike=contract.strike,
        expiry=expiry, bid=contract.bid, ask=contract.ask, spread=contract.spread,
        open_interest=contract.open_interest, qty=qty,
        cost=contract.ask * qty * 100.0, reasons=reasons)


def enter_from_alert(broker: BrokerAdapter, store: Store, selector: ContractSelector,
                     risk: RiskGuard, om: OrderManager, e: AlertEntry) -> Position:
    """Abre 1 posición para la alerta. Lanza EntryError con el motivo si no se puede.
    Re-resuelve + re-valida en el momento de ejecutar (autoritativo, aunque haya preview)."""
    side, spot, expiry, contract, qty = _resolve_contract(broker, selector, e)

    # idempotencia: no re-comprar el MISMO contrato si ya hay posición abierta.
    existing = store.get_position(contract.occ)
    if existing and existing.get("status") == "open":
        raise EntryError(f"Ya hay una posición abierta en {contract.occ}.")

    # validación de riesgo PRE-orden (mercado abierto, spread, OI, costo, breakers).
    account = broker.get_account()
    errs = risk.validate_entry(contract, qty, account)
    if errs:
        raise EntryError("Bloqueado por riesgo: " + " · ".join(errs))

    # compra. tag = alert_<id> para idempotencia/auditoría en el broker.
    store.audit("alert_entry", {"alert_id": e.alert_id, "underlying": e.underlying,
                                "side": side, "occ": contract.occ, "qty": qty,
                                "roi_target_pct": e.roi_target_pct, "expiry": expiry})
    pos = om.buy(contract, qty, e.roi_target_pct, idempotency_key=f"alert_{e.alert_id}")

    # auto-armar el take-profit → el daemon vende solo al llegar al Umbral de ROI.
    if e.arm_tp:
        store.set_tp_armed(pos.occ, True)
    return pos

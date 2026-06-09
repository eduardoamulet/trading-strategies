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

from dataclasses import dataclass

from brokers.base import BrokerAdapter
from core.models import Position
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


def enter_from_alert(broker: BrokerAdapter, store: Store, selector: ContractSelector,
                     risk: RiskGuard, om: OrderManager, e: AlertEntry) -> Position:
    """Abre 1 posición para la alerta. Lanza EntryError con el motivo si no se puede.
    Usa el vencimiento más cercano (0DTE si existe ese día)."""
    side = (e.side or "").upper().strip()
    if side not in ("CALL", "PUT"):
        raise EntryError(f"Tipo inválido '{e.side}' (esperaba CALL o PUT).")
    if not e.underlying:
        raise EntryError("Falta el símbolo del subyacente.")

    # 1) precio del subyacente + chain del vencimiento más cercano (0DTE si lo hay).
    spot = broker.get_underlying_price(e.underlying)
    expiry = broker.nearest_expiry(e.underlying)
    if not expiry:
        raise EntryError(f"{e.underlying}: sin vencimientos disponibles.")
    chain = broker.get_option_chain(e.underlying, expiry)
    if not chain:
        raise EntryError(f"{e.underlying}: chain vacío para {expiry}.")

    # 2) selección de contrato (misma filosofía que el backtest).
    try:
        contract = selector.select(chain, side, spot, strategy=e.strategy)
    except NoLiquidContract as ex:
        raise EntryError(str(ex)) from ex

    # 3) cantidad según la inversión (cada contrato cuesta ask × 100).
    unit = contract.ask * 100.0
    qty = int(e.inversion // unit) if unit > 0 else 0
    if qty < 1:
        raise EntryError(
            f"Inversión ${e.inversion:,.0f} no alcanza para 1 contrato de "
            f"{e.underlying} {side} (ask ${contract.ask:.2f} → ${unit:,.0f} c/u).")

    # 4) idempotencia: no re-comprar el MISMO contrato si ya hay posición abierta.
    existing = store.get_position(contract.occ)
    if existing and existing.get("status") == "open":
        raise EntryError(f"Ya hay una posición abierta en {contract.occ}.")

    # 5) validación de riesgo PRE-orden (mercado abierto, spread, OI, costo, breakers).
    account = broker.get_account()
    errs = risk.validate_entry(contract, qty, account)
    if errs:
        raise EntryError("Bloqueado por riesgo: " + " · ".join(errs))

    # 6) compra. tag = alert_<id> para idempotencia/auditoría en el broker.
    store.audit("alert_entry", {"alert_id": e.alert_id, "underlying": e.underlying,
                                "side": side, "occ": contract.occ, "qty": qty,
                                "roi_target_pct": e.roi_target_pct, "expiry": expiry})
    return om.buy(contract, qty, e.roi_target_pct, idempotency_key=f"alert_{e.alert_id}")

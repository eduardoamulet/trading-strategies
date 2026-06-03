"""OrderManager — compra/venta con órdenes LIMIT, manejo de fills parciales,
idempotencia y reprecio controlado en la salida."""
from __future__ import annotations

import threading
import time
from datetime import datetime

from brokers.base import BrokerAdapter
from core.models import Contract, Fill, OrderRequest, Position
from core.store import Store


class OrderManager:
    def __init__(self, broker: BrokerAdapter, store: Store):
        self.broker = broker
        self.store = store
        self._sell_locks: dict[str, threading.Lock] = {}

    # ---------- fills ----------
    def _await_fill(self, order_id: str, timeout: float = 30.0, poll: float = 1.0) -> Fill:
        deadline = time.time() + timeout
        last = self.broker.get_order(order_id)
        while time.time() < deadline:
            last = self.broker.get_order(order_id)
            if last.status in ("filled", "rejected", "canceled"):
                return last
            time.sleep(poll)
        return last  # 'partial' o 'pending' tras timeout

    # ---------- compra ----------
    def buy(self, contract: Contract, qty: int, roi_target_pct: float,
            idempotency_key: str) -> Position:
        req = OrderRequest(
            occ=contract.occ, underlying=contract.underlying,
            side="buy_to_open", qty=qty, type="limit",
            limit_price=contract.ask,           # marketable limit @ ask (NUNCA market)
            duration="day", tag=idempotency_key,
        )
        self.store.audit("order_submit", {"side": "buy", "occ": contract.occ,
                                          "qty": qty, "limit": contract.ask, "key": idempotency_key})
        res = self.broker.place_order(req)
        if res.status != "ok":
            self.store.audit("order_rejected", {"occ": contract.occ, "raw": res.raw})
            raise RuntimeError(f"Orden rechazada: {res.raw}")

        fill = self._await_fill(res.order_id)
        if fill.filled_qty == 0:
            self.broker.cancel_order(res.order_id)
            raise RuntimeError("No se llenó ningún contrato (cancelada).")
        if fill.status == "partial":
            # cancelar remanente, operar SOLO lo llenado
            self.broker.cancel_order(res.order_id)
            self.store.audit("partial_fill", {"occ": contract.occ,
                             "requested": qty, "filled": fill.filled_qty})

        pos = Position(
            occ=contract.occ, underlying=contract.underlying,
            qty=fill.filled_qty, entry_price=fill.avg_price,
            entry_time=fill.time or datetime.utcnow(),
            cost_total=fill.avg_price * fill.filled_qty * 100,
            order_id=res.order_id, roi_target_pct=roi_target_pct,
            current_price=fill.avg_price,
        )
        self.store.save_position(pos)
        return pos

    # ---------- venta (manual o auto TP) ----------
    def sell(self, occ: str, reason: str = "manual") -> bool:
        lock = self._sell_locks.setdefault(occ, threading.Lock())
        if not lock.acquire(blocking=False):
            return False  # venta ya en curso para este contrato
        try:
            pos = self.store.get_position(occ)
            if not pos or pos["status"] != "open":
                return False
            self.store.mark_closing(occ)
            qty = int(pos["qty"])

            # marketable limit @ bid, con reprecio controlado si no llena
            order_id = None
            filled = None
            for attempt in range(3):
                q = self.broker.get_quote(occ)
                price = round(max(q.bid - attempt * 0.01, 0.01), 2)  # 1 tick más agresivo c/intento
                req = OrderRequest(occ=occ, underlying=pos["underlying"],
                                   side="sell_to_close", qty=qty, type="limit",
                                   limit_price=price, duration="day",
                                   tag=f"tp_{pos['order_id']}_{attempt}")
                self.store.audit("order_submit", {"side": "sell", "occ": occ,
                                 "qty": qty, "limit": price, "reason": reason, "attempt": attempt})
                res = self.broker.place_order(req)
                if res.status != "ok":
                    continue
                order_id = res.order_id
                filled = self._await_fill(order_id, timeout=15)
                if filled.status == "filled":
                    break
                self.broker.cancel_order(order_id)  # reprecio

            if not filled or filled.filled_qty == 0:
                self.store.audit("sell_failed", {"occ": occ, "reason": reason})
                # revertir a 'open' para reintentar luego
                self.store.set_tp_armed(occ, False)
                with self.store._conn() as c:
                    c.execute("UPDATE positions SET status='open' WHERE occ=?", (occ,))
                return False

            exit_px = filled.avg_price
            proceeds = exit_px * filled.filled_qty * 100
            pnl_net = proceeds - pos["cost_total"]
            roi_final = (pnl_net / pos["cost_total"] * 100) if pos["cost_total"] else 0.0
            self.store.close_position(occ, exit_price=exit_px,
                                      exit_time=filled.time or datetime.utcnow(),
                                      roi_final=roi_final, pnl_net=pnl_net)
            return True
        finally:
            lock.release()

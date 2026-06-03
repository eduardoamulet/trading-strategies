"""PositionMonitor — recalcula ROI/PnL con el BID (precio realmente vendible)
y dispara la venta automática si TP está armado y ROI >= objetivo."""
from __future__ import annotations

from core.models import Quote
from core.store import Store


class PositionMonitor:
    def __init__(self, store: Store, order_manager):
        self.store = store
        self.om = order_manager

    def on_quote(self, q: Quote) -> None:
        pos = self.store.get_position(q.occ)
        if not pos or pos["status"] != "open":
            return
        # ROI conservador: usamos el BID (lo que REALMENTE cobrás al cerrar long).
        sell_px = q.bid if q.bid > 0 else q.last
        current_value = sell_px * pos["qty"] * 100
        cost = pos["cost_total"]
        roi = (current_value - cost) / cost * 100 if cost else 0.0
        pnl = current_value - cost
        self.store.update_live(q.occ, current_price=sell_px, roi_pct=roi, pnl=pnl)

        # AUTO take-profit
        if pos["tp_armed"] and roi >= pos["roi_target_pct"]:
            self.store.audit("tp_trigger", {"occ": q.occ, "roi": roi,
                                            "target": pos["roi_target_pct"]})
            self.om.sell(q.occ, reason="take_profit")

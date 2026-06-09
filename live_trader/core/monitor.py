"""PositionMonitor — recalcula ROI/PnL con el BID (precio realmente vendible)
y dispara la venta automática si TP está armado y ROI >= objetivo.

La REGLA de salida vive en strategy_core (núcleo compartido con el backtest)."""
from __future__ import annotations

# Núcleo compartido (raíz del repo). append = prioridad baja → no tapa 'settings' local.
import sys as _sys
from pathlib import Path as _Path
_ROOT = str(_Path(__file__).resolve().parent.parent.parent)
if _ROOT not in _sys.path:
    _sys.path.append(_ROOT)
import strategy_core  # noqa: E402

from core.models import Quote  # noqa: E402
from core.store import Store  # noqa: E402


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

        # AUTO take-profit — misma regla que el backtest (strategy_core.exit_decision).
        # stop_pct=-100 → el stop total lo maneja el EOD-flatten; acá solo el TP.
        if pos["tp_armed"]:
            decision = strategy_core.exit_decision(roi, pos["roi_target_pct"], stop_pct=-100.0)
            if decision == "take_profit":
                self.store.audit("tp_trigger", {"occ": q.occ, "roi": roi,
                                                "target": pos["roi_target_pct"]})
                self.om.sell(q.occ, reason="take_profit")

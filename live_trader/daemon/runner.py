"""Execution daemon — proceso HEADLESS que corre SIEMPRE (no depende de la UI).

Responsabilidades:
- Poll de quotes de las posiciones abiertas → actualizar ROI/PnL en la store.
- Disparar AUTO take-profit cuando ROI >= objetivo (vía PositionMonitor).
- EOD flatten: cerrar 0DTE antes de la hora límite (riesgo de asignación).
- Consumir comandos de la UI (arm/disarm TP, manual sell, kill switch).
- Reconexión con backoff; re-sync de posiciones desde el broker al arrancar.

Correr en una terminal separada:
    cd live_trader && py -m daemon.runner
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
# Solo live_trader/ al frente del path. NO agregar Traiding/ (tiene su propio
# settings.py de Polygon que colisionaría con live_trader/settings.py).
sys.path.insert(0, str(HERE))           # live_trader/

import settings
from brokers.tradier import TradierAdapter
from core.monitor import PositionMonitor
from core.order_manager import OrderManager
from core.risk import RiskGuard
from core.store import Store

_STOP = False


def _handle_sigint(*_):
    global _STOP
    _STOP = True
    print("\n[daemon] parando...", flush=True)


def main() -> int:
    signal.signal(signal.SIGINT, _handle_sigint)
    mode = "LIVE ⚠" if settings.LIVE_TRADING_ENABLED else "SANDBOX"
    print(f"[daemon] arrancando en modo {mode} ({settings.base_url()})", flush=True)

    store = Store(settings.DB_PATH)
    try:
        broker = TradierAdapter()
    except Exception as e:
        print(f"[daemon] ERROR init broker: {e}", flush=True)
        return 1
    om = OrderManager(broker, store)
    monitor = PositionMonitor(store, om)
    risk = RiskGuard(store)

    store.audit("daemon_start", {"mode": mode})
    _hb = json.dumps({"mode": mode, "pid": os.getpid()})
    store.set_meta("daemon_heartbeat", _hb)   # latido inicial (la UI lo ve enseguida)
    print("[daemon] OK. Ctrl+C para parar.", flush=True)

    while not _STOP:
        try:
            # 0) latido: la UI detecta que el monitoreo/auto-sell está vivo.
            store.set_meta("daemon_heartbeat", _hb)

            # 1) consumir comandos de la UI
            for cmd in store.pop_commands():
                _apply_command(cmd, store, om)

            # 2) poll de quotes + ROI + auto-TP
            open_pos = store.open_positions()
            for p in open_pos:
                try:
                    q = broker.get_quote(p["occ"])
                    monitor.on_quote(q)
                except Exception as e:
                    store.audit("quote_error", {"occ": p["occ"], "err": str(e)})

            # 3) EOD flatten — cerrar 0DTE antes de la hora límite
            if open_pos and risk.past_eod_flatten():
                for p in open_pos:
                    if p["status"] == "open":
                        store.audit("eod_flatten", {"occ": p["occ"]})
                        om.sell(p["occ"], reason="eod_flatten")

            time.sleep(settings.POLL_INTERVAL_SEC)
        except Exception as e:
            store.audit("daemon_loop_error", {"err": str(e)})
            time.sleep(min(settings.POLL_INTERVAL_SEC * 2, 10))

    store.audit("daemon_stop", {})
    print("[daemon] detenido.", flush=True)
    return 0


def _apply_command(cmd: dict, store: Store, om: OrderManager):
    kind = cmd.get("kind")
    occ = cmd.get("occ", "")
    store.audit("command", cmd)
    if kind == "arm_tp":
        store.set_tp_armed(occ, True)
    elif kind == "disarm_tp":
        store.set_tp_armed(occ, False)
    elif kind == "manual_sell":
        om.sell(occ, reason="manual")
    elif kind == "kill_switch":
        # desarmar TP de todo y cerrar todo a mercado-limit
        for p in store.open_positions():
            store.set_tp_armed(p["occ"], False)
            om.sell(p["occ"], reason="kill_switch")


if __name__ == "__main__":
    sys.exit(main())

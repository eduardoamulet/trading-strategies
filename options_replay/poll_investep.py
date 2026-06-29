"""Poller de señales Investep — loop infinito que cada N minutos baja las señales
nuevas de la API (login automático con las credenciales de signals_secrets.py) y las
carga a la base. Alimenta la 🔔 campanita de la app.

Pensado para correr DETACHED desde la página Tareas (sobrevive al cierre de la UI y
de la app). Se apaga matando el proceso (botón «Apagar»). El intervalo es un parámetro.

Uso:  python poll_investep.py --interval-min 1.0
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

try:   # Windows: con stdout redirigido a archivo el encoding por defecto (cp1252) no puede
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # con emojis → forzamos UTF-8
except Exception:
    pass

import external_signals as xs


def _log(msg: str) -> None:
    try:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass   # el poller NUNCA debe morir por un problema de encoding en el log


def main() -> int:
    ap = argparse.ArgumentParser(description="Poller de señales Investep")
    ap.add_argument("--interval-min", type=float, default=1.0,
                    help="Intervalo entre consultas, en minutos (default 1.0)")
    ap.add_argument("--days-back", type=int, default=1,
                    help="Cuántos días hacia atrás bajar en cada consulta (default 1 = hoy)")
    args = ap.parse_args()

    interval_s = max(15.0, args.interval_min * 60.0)   # piso de 15s de guarda
    _log(f"▶ Poller Investep arrancado · cada {args.interval_min:g} min "
         f"(={interval_s:.0f}s) · days_back={args.days_back}")

    while True:
        t0 = time.time()
        try:
            n = xs.fetch_from_api(days_back=args.days_back)
            _log(f"✓ fetch OK · {n} señal(es) nueva(s)")
        except xs.ScraperNotConfigured as e:
            _log(f"⚠ sin configurar (revisá signals_secrets.py): {e}")
        except Exception as e:                          # noqa: BLE001 — el poller no debe morir
            _log(f"✗ error: {type(e).__name__}: {e}")
        # dormir lo que reste del intervalo (descontando lo que tardó el fetch)
        time.sleep(max(1.0, interval_s - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())

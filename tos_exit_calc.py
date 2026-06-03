"""Calculadora de salida condicional para opciones en thinkorswim.

Convierte un objetivo de P/L% en el PRECIO de prima al que poner la orden:

    salida (+X%) = entrada * (1 + X/100)
    stop   (-Y%) = entrada * (1 - Y/100)

En una opción larga (CALL o PUT comprado) el P/L% es monótono con la prima,
así que "vender a +10% de P/L" == "SELL LIMIT a entrada*1.10". Equivalencia exacta.

Uso:
    py tos_exit_calc.py                 # tabla de referencia
    py tos_exit_calc.py 1.00 10         # entrada 1.00, target +10%
    py tos_exit_calc.py 1.35 10 --stop 20   # target +10% y stop -20%
"""
from __future__ import annotations

import argparse


def exit_price(entry: float, pct: float, tick: float = 0.01) -> float:
    """Precio de prima para un objetivo de +pct% (o -pct% si pct<0).
    Redondea al `tick` (default $0.01)."""
    raw = entry * (1.0 + pct / 100.0)
    return round(round(raw / tick) * tick, 2)


def pl_pct(entry: float, price: float) -> float:
    """P/L% resultante de vender en `price` habiendo entrado en `entry`."""
    if entry <= 0:
        return 0.0
    return (price / entry - 1.0) * 100.0


def _one(entry: float, target: float, stop: float | None, tick: float) -> None:
    ex = exit_price(entry, target, tick)
    print(f"\n  Entrada:        ${entry:.2f}")
    print(f"  Target +{target:g}%:    SELL LIMIT a ${ex:.2f}   (P/L real {pl_pct(entry, ex):+.1f}%)")
    if stop is not None:
        sp = exit_price(entry, -abs(stop), tick)
        print(f"  Stop  -{abs(stop):g}%:     SELL STOP  a ${sp:.2f}   (P/L real {pl_pct(entry, sp):+.1f}%)")
        print(f"\n  -> OCO: SELL LIMIT ${ex:.2f}  /  SELL STOP ${sp:.2f}  (1st Trgs OCO)")


def _table(tick: float) -> None:
    entries = [0.20, 0.35, 0.50, 0.75, 1.00, 1.35, 1.50, 2.00, 3.00, 5.00]
    targets = [5, 10, 15, 20, 25]
    head = "  Entrada | " + " | ".join(f"+{t:>2}%" for t in targets)
    print(f"\n  Precio de SELL LIMIT por objetivo de P/L% (redondeado a ${tick:.2f})\n")
    print(head)
    print("  " + "-" * (len(head) - 2))
    for e in entries:
        cells = " | ".join(f"{exit_price(e, t, tick):>4.2f}" for t in targets)
        print(f"   ${e:>4.2f} | {cells}")
    print("\n  (P/L% es exacto respecto al fill; el redondeo al tick puede mover el % unas decimas.)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Calculadora de salida condicional (thinkorswim).")
    ap.add_argument("entry", nargs="?", type=float, help="Prima de entrada (ej. 1.00)")
    ap.add_argument("target", nargs="?", type=float, default=10.0, help="Objetivo de P/L%% (default 10)")
    ap.add_argument("--stop", type=float, default=None, help="Stop de perdida en %% (ej. 20 = -20%%)")
    ap.add_argument("--tick", type=float, default=0.01, help="Incremento de precio (default 0.01)")
    args = ap.parse_args()

    if args.entry is None:
        _table(args.tick)
    else:
        _one(args.entry, args.target, args.stop, args.tick)


if __name__ == "__main__":
    main()

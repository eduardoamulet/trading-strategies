"""Exporta señales TR-UD-15m por ticker (modo APERTURA o INTRADÍA) a Excel + CSV.

Usa el detector Python (réplica EXACTA del Pine «Trend Reversal Up and Down BB 15m»)
sobre el 1-min cacheado de Polygon → 15m RTH.

Modo:
  - apertura (default): 1 señal/día al gap de 9:30.
  - --intraday: ≡ casilla «MODO INTRADIA · Detectar en TODA la sesión (cruces, no solo
    apertura)». Detecta cruces en cualquier vela de la sesión (con cooldown anti-cluster).

Salida en signals_export/:
  - <TICKER>[_INTRADIA]_TR-UD-15m_signals.xlsx   (uno por ticker)  + .csv
  - TODOS[_INTRADIA]_TR-UD-15m_signals.xlsx       (hoja TODAS + una por ticker) + .csv

Uso:
    py export_all_signals.py                                   # apertura, rango completo
    py export_all_signals.py --intraday                        # INTRADÍA, rango completo
    py export_all_signals.py --intraday --start 2025-01-01 --end 2026-06-24
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd  # noqa: E402

from strategies.run_detect import detect  # noqa: E402
from strategies.trend_reversal_bb_15m import TrendReversalBB15m  # noqa: E402

TICKERS = ["QQQ", "SPY", "IWM", "NVDA", "TSLA", "PLTR", "AMZN", "META", "MSFT", "GOOG", "AAPL"]

ap = argparse.ArgumentParser()
ap.add_argument("--start", default="2020-01-01")
ap.add_argument("--end", default="2026-12-31")
ap.add_argument("--intraday", action="store_true",
                help="MODO INTRADIA: detecta cruces en TODA la sesión (no solo apertura).")
args = ap.parse_args()

OUT = HERE / "signals_export"
OUT.mkdir(exist_ok=True)
TAG = "_INTRADIA" if args.intraday else ""
MODE = "INTRADÍA (toda la sesión)" if args.intraday else "apertura (9:30)"
EST = "TR-UD-15m intradia" if args.intraday else "TR-UD-15m"


def _csv_view(sig: pd.DataFrame) -> pd.DataFrame:
    """Columnas que lee la página «Backtesting de Señales de TradingView»."""
    out = sig[["ticker", "fecha", "hora_et", "direccion"]].copy()
    out["estrategia"] = EST
    return out


strat = TrendReversalBB15m({"intraday": args.intraday})
combined: dict[str, pd.DataFrame] = {}

print(f"Detectando señales TR-UD-15m · {MODE} · {args.start} → {args.end}\n")
for tk in TICKERS:
    sig = detect(tk, args.start, args.end, strat)
    n = len(sig)
    print(f"  {tk:5s}: {n:5d} señales")
    if n:
        sig.to_excel(OUT / f"{tk}{TAG}_TR-UD-15m_signals.xlsx", index=False, sheet_name=tk)
        _csv_view(sig).to_csv(OUT / f"{tk}{TAG}_TR-UD-15m_signals.csv", index=False)
        combined[tk] = sig

if combined:
    allsig = pd.concat(combined.values(), ignore_index=True)
    with pd.ExcelWriter(OUT / f"TODOS{TAG}_TR-UD-15m_signals.xlsx") as xw:
        allsig.to_excel(xw, sheet_name="TODAS", index=False)
        for tk, sig in combined.items():
            sig.to_excel(xw, sheet_name=tk, index=False)
    _csv_view(allsig).to_csv(OUT / f"TODOS{TAG}_TR-UD-15m_signals.csv", index=False)
    print(f"\nTOTAL: {len(allsig)} señales · {len(combined)} tickers · modo {MODE}")
    print(f"-> {OUT}")
else:
    print("\nNinguna señal generada (¿sin data en el rango?).")

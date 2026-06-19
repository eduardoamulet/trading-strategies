"""Reporte de COBERTURA OFFLINE: qué tenés para backtestear SIN Polygon.

Escanea data/quotes_minute (NBBO por minuto = fills honestos Fase 2) y reporta por ticker el
rango de fechas, días distintos con NBBO y contratos-día. Cruza con underlying (barras del
subyacente, necesarias para el spot/ATM) para confirmar que cada día es backtesteable. Puro
local: lee nombres de archivo, NO pega a Polygon.

Uso (desde options_replay/):  py coverage_report.py
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
_OCC = re.compile(r"^O_([A-Z]+)\d{6}[CP]\d{8}_(\d{4}-\d{2}-\d{2})$")


def scan_quotes_minute():
    """{ticker: (set(dates), n_files)} de quotes_minute/."""
    per = defaultdict(lambda: [set(), 0])
    d = DATA / "quotes_minute"
    if not d.exists():
        return per
    with os.scandir(d) as it:
        for e in it:
            if not e.name.endswith(".parquet"):
                continue
            m = _OCC.match(e.name[:-8])
            if not m:
                continue
            per[m.group(1)][0].add(m.group(2))
            per[m.group(1)][1] += 1
    return per


def underlying_days(ticker: str) -> set:
    """Días con barras 1-min del subyacente (excluye sufijos _15s/_30s)."""
    pat = re.compile(rf"^{re.escape(ticker)}_(\d{{4}}-\d{{2}}-\d{{2}})$")
    return {m.group(1) for p in (DATA / "underlying").glob(f"{ticker}_*.parquet")
            if (m := pat.match(p.stem))}


def _yrs(d0: str, d1: str) -> float:
    from datetime import date
    return (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    qm = scan_quotes_minute()
    if not qm:
        print("No hay quotes_minute cacheados.")
        return 0

    print("=" * 96)
    print("  COBERTURA OFFLINE — NBBO por minuto (fills Fase 2)  ·  lo que backtesteás SIN Polygon")
    print("=" * 96)
    hdr = (f"{'Ticker':<6}{'contratos-día':>15}{'días c/NBBO':>13}{'desde':>13}{'hasta':>13}"
           f"{'años':>7}{'subyac.días':>13}")
    print(hdr)
    print("-" * len(hdr))
    tot_files = 0
    rows = sorted(qm.items(), key=lambda kv: (-len(kv[1][0])))
    for tk, (dates, files) in rows:
        tot_files += files
        dd = sorted(dates)
        ud = underlying_days(tk)
        flag = "" if len(ud) >= len(dates) else f"  ⚠ faltan {len(dates) - len(ud)} días de subyac."
        print(f"{tk:<6}{files:>15,}{len(dates):>13,}{dd[0]:>13}{dd[-1]:>13}"
              f"{_yrs(dd[0], dd[-1]):>7.1f}{len(ud):>13,}{flag}")
    print("-" * len(hdr))
    print(f"{'TOTAL':<6}{tot_files:>15,} contratos-día NBBO  ·  {len(qm)} tickers")
    print()
    # Resumen "para qué alcanza"
    core = [t for t in ("QQQ", "SPY", "IWM") if t in qm]
    if core:
        spans = {t: sorted(qm[t][0]) for t in core}
        print("Núcleo 0DTE diario (mayor historia):")
        for t in core:
            s = spans[t]
            print(f"  · {t}: {len(qm[t][0]):,} días  {s[0]} → {s[-1]}  (~{_yrs(s[0], s[-1]):.1f} años)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""walkforward.py — ENDURECER el patrón sobre 4 AÑOS (QQQ/SPY/IWM, 2022-06→2026-06), atravesando
varios regímenes (bear 2022, recuperación 2023, bull 2024-25, reciente). Corre 100% de CACHÉ:
`entry_from_timeline=True` sirve el NBBO de entrada desde quotes_minute (que ya tenemos 4 años) →
sin pegar a Polygon. Genera P&L día-a-día (Fase 2, Refuerzo×2 um10 stop-80) + features pre-entrada,
guardando INCREMENTALMENTE (resiliente). El análisis walk-forward (folds + por régimen) va aparte.

Uso:  py walkforward.py            # 4 años completos → data/walkforward_days.csv
      py walkforward.py validate   # solo 2026-01→06 → data/walkforward_2026.csv (comparar vs pattern_finder)
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import time as _time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import numpy as np
import pandas as pd

import config
import signals_backtest as sbt
from adapter_polygon import PolygonAdapter
from downloader import Downloader

TICKERS = ["QQQ", "SPY", "IWM"]
ENTRY, SEARCH_WIN = "09:30", 4.0
INVERSION, CALL_PCT = 1000.0, 50.0
REFUERZO_MAX, REFUERZO_LOSS, UMBRAL, STOP = 2, 0.50, 10, -80
_DOW = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

VALIDATE = len(sys.argv) > 1 and sys.argv[1].lower().startswith("valid")
DATE_START = "2026-01-01" if VALIDATE else "2022-06-01"
DATE_END = "2026-06-18"
OUT = HERE / "data" / ("walkforward_2026.csv" if VALIDATE else "walkforward_days.csv")
FIELDS = ["ticker", "date", "year", "dow", "gain", "roi", "win", "abs_gap", "gap", "prev_rng", "or5"]


def _day_stats(df: pd.DataFrame) -> dict | None:
    if df is None or df.empty:
        return None
    t = df["timestamp"].dt.time
    reg = df[(t >= _time(9, 30)) & (t < _time(16, 0))]
    if reg.empty:
        return None
    open0930 = float(reg.iloc[0]["open"])
    hi, lo = float(reg["high"].max()), float(reg["low"].min())
    last = float(reg.iloc[-1]["close"])
    tt = reg["timestamp"].dt.time
    or5 = reg[(tt >= _time(9, 30)) & (tt <= _time(9, 34))]
    or5_rng = (float(or5["high"].max()) - float(or5["low"].min())) if not or5.empty else np.nan
    return {"open": open0930, "high": hi, "low": lo, "last": last, "or5_rng": or5_rng}


def _in_filter(abs_gap, dow):
    return (0.18 <= abs_gap <= 0.78) and dow not in ("Lun", "Vie")


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=600), HERE / "data")
    dl.resolution = "1min"
    dl.entry_from_timeline = True   # ← sirve el NBBO de entrada desde quotes_minute (sin Polygon)
    dl.offline = True               # ← estrictamente cache-only: si falta algo, saltea (nunca cuelga en Polygon)
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(DATE_START, DATE_END)]
    print(f"== WALK-FORWARD {'(validación 2026)' if VALIDATE else '4 AÑOS'} · {','.join(TICKERS)} · "
          f"{DATE_START}→{DATE_END} · {len(days)} días háb · Fase 2 · Refuerzo×{REFUERZO_MAX} um{UMBRAL} "
          f"stop{STOP} · OFFLINE ==", flush=True)
    if OUT.exists():
        OUT.unlink()
    t0 = time.time()
    header_done = False

    def flush(rows):
        nonlocal header_done
        if not rows:
            return
        with open(OUT, "a" if header_done else "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if not header_done:
                w.writeheader()
                header_done = True
            w.writerows(rows)

    for tk in TICKERS:
        prev = None
        by_year: dict[str, list] = {}
        t_tk = time.time()
        for _i, d in enumerate(days):
            if _i % 200 == 0 and _i:
                print(f"  {tk} … {d} ({_i}/{len(days)})", flush=True)
            try:
                udf = dl.underlying(tk, d)
            except Exception:
                udf = None
            st = _day_stats(udf)
            if st is None:
                continue
            abs_gap = prev_rng = gap = np.nan
            if prev is not None and prev["last"]:
                gap = (st["open"] - prev["last"]) / prev["last"] * 100.0
                abs_gap = abs(gap)
                prev_rng = (prev["high"] - prev["low"]) / prev["last"] * 100.0
            or5 = (st["or5_rng"] / st["open"] * 100.0) if st["open"] and not np.isnan(st["or5_rng"]) else np.nan
            prev = st
            if np.isnan(abs_gap):
                continue
            try:
                r = sbt.run_one(
                    dl, {"ticker": tk, "fecha": d, "hora": ENTRY, "tipo": "CALL y PUT (Refuerzo)"},
                    inversion=INVERSION, umbral_pct=UMBRAL, stop_pct=STOP,
                    entry_at_ask=True, exit_at_bid=True, nbbo_timeline=True,
                    selection_criterion="spread", call_pct=CALL_PCT,
                    refuerzo_loss_pct=REFUERZO_LOSS, refuerzo_max=REFUERZO_MAX,
                    search_window_min=SEARCH_WIN)
            except Exception:
                continue
            if r["status"] != "ok":
                continue
            it = r["iteration"]
            inv = it.invest_total
            roi = (it.gain_total / inv * 100.0) if inv else 0.0
            yr = d[:4]
            by_year.setdefault(yr, []).append(
                {"ticker": tk, "date": d, "year": yr, "dow": _DOW[pd.Timestamp(d).dayofweek],
                 "gain": it.gain_total, "roi": roi, "win": int(it.gain_total > 0),
                 "abs_gap": abs_gap, "gap": gap, "prev_rng": prev_rng, "or5": or5})
        # flush + resumen por año de este ticker (baseline vs filtro)
        for yr in sorted(by_year):
            rows = by_year[yr]
            flush(rows)
            base = sum(x["gain"] for x in rows)
            filt = [x for x in rows if _in_filter(x["abs_gap"], x["dow"])]
            fg = sum(x["gain"] for x in filt)
            fw = (sum(x["win"] for x in filt) / len(filt) * 100) if filt else 0
            print(f"  {tk} {yr}: n={len(rows):>3} base=${base:>+8,.0f} | filtro n={len(filt):>3} "
                  f"${fg:>+8,.0f} win={fw:>3.0f}%", flush=True)
        print(f"  {tk}: {sum(len(v) for v in by_year.values())} días en {(time.time()-t_tk)/60:.1f} min", flush=True)

    print(f"\n== listo en {(time.time()-t0)/60:.1f} min → {OUT.name} ==", flush=True)


if __name__ == "__main__":
    main()

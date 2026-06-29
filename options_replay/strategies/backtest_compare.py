"""Fase 4 — Backtest comparativo: las señales TR-UD-15m bajo la config actual vs los 3 fixes.

Fixes (del análisis de operaciones perdedoras):
  1) Strike ITM/ATM (selection_criterion='itm_first') en vez de OTM ('spread').
  2) Filtro R4-BB: solo señales con el open DENTRO de las Bandas de Bollinger.
  3) Confirmación: entrar 09:45 solo si la 1ª vela de 15m confirmó la dirección.

Fills honestos Fase 2 (entry@ask, exit@bid por minuto, NBBO offline). Stop -100%, sin cap de profit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # options_replay/ (adapter, downloader, ...)
sys.path.insert(0, str(HERE.parent.parent))   # repo root (config.py)

import config                                              # noqa: E402
from adapter_polygon import PolygonAdapter                 # noqa: E402
from downloader import Downloader                           # noqa: E402
from signals_backtest import run_one                        # noqa: E402
from strategies.run_detect import load_15m                  # noqa: E402
from strategies import indicators as ind                    # noqa: E402

DATA = HERE.parent / "data"


def _dl() -> Downloader:
    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY), DATA)
    dl.entry_from_timeline = True   # servir NBBO desde quotes_minute
    dl.offline = True               # estrictamente cache (sin Polygon)
    return dl


def _band_lookup(tickers, start, end) -> dict:
    """(ticker, fecha) -> (open, upper, lower, close_1aVela) de la barra de apertura."""
    out = {}
    for tk in tickers:
        b = load_15m(tk, start, end)
        if b.empty:
            continue
        basis, up, lo = ind.bollinger(b["close"])
        b = b.assign(upper=up, lower=lo)
        for _, r in b[b["is_opening"]].iterrows():
            out[(tk, str(r["session_date"]))] = (r["open"], r["upper"], r["lower"], r["close"])
    return out


def _annotate(sig: pd.DataFrame, band: dict) -> pd.DataFrame:
    def f(r):
        o, up, lo, cl = band.get((r["ticker"], r["fecha"]), (None, None, None, None))
        if o is None or pd.isna(up) or pd.isna(lo):
            return pd.Series({"in_bands": False, "conf": False})
        in_b = bool(lo <= o <= up)
        conf = bool(cl > o) if r["direccion"] == "CALL" else bool(cl < o)
        return pd.Series({"in_bands": in_b, "conf": conf})
    return sig.join(sig.apply(f, axis=1))


def _backtest(dl, sigs: pd.DataFrame, sel: str, hora: str) -> dict:
    pnls, errs, tiers = [], 0, []
    for _, r in sigs.iterrows():
        spec = {"ticker": r["ticker"], "fecha": r["fecha"], "hora": hora, "tipo": r["direccion"]}
        res = run_one(dl, spec, inversion=1000.0, umbral_pct=1000.0, stop_pct=-100.0,
                      selection_criterion=sel, nbbo_timeline=True,
                      entry_at_ask=True, exit_at_bid=True, dte=0)
        if res["status"] != "ok":
            errs += 1
            continue
        it = res["iteration"]
        pnls.append(float(it.gain_total))
        tiers.append(it.call_range_tier or it.put_range_tier or "")
    n = len(pnls)
    if n == 0:
        return {"n": 0, "errs": errs}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), -sum(losses)
    return {
        "n": n, "errs": errs,
        "win%": round(100 * len(wins) / n, 1),
        "total$": round(sum(pnls), 0),
        "avg$": round(sum(pnls) / n, 1),
        "PF": round(gp / gl, 2) if gl > 0 else float("inf"),
        "best$": round(max(pnls), 0), "worst$": round(min(pnls), 0),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default=str(DATA / "signals_tr_ud_15m.csv"))
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-06-23")
    ap.add_argument("--out", default=str(DATA / "backtest_compare_tr_ud_15m.csv"))
    a = ap.parse_args()

    sig = pd.read_csv(a.signals)
    sig["fecha"] = sig["fecha"].astype(str)
    band = _band_lookup(sorted(sig["ticker"].unique()), a.start, a.end)
    sig = _annotate(sig, band)
    print(f"Señales: {len(sig)} | con open DENTRO de bandas: {int(sig['in_bands'].sum())} | "
          f"+ 1ª vela confirma: {int((sig['in_bands'] & sig['conf']).sum())}\n", flush=True)

    dl = _dl()
    in_b = sig[sig["in_bands"]]
    in_b_conf = sig[sig["in_bands"] & sig["conf"]]
    configs = [
        ("1. OTM (baseline, 09:30)",        sig,        "spread",    "09:30"),
        ("2. ITM (09:30)",                  sig,        "itm_first", "09:30"),
        ("3. ITM + filtro BB (09:30)",      in_b,       "itm_first", "09:30"),
        ("4. ITM + BB + confirm 09:45",     in_b_conf,  "itm_first", "09:45"),
        ("5. OTM + filtro BB (09:30)",      in_b,       "spread",    "09:30"),
        ("6. OTM + BB + confirm 09:45",     in_b_conf,  "spread",    "09:45"),
    ]
    rows = []
    for name, sigs, sel, hora in configs:
        m = _backtest(dl, sigs, sel, hora)
        m["config"] = name
        rows.append(m)
        print(f"{name}\n   {m}\n", flush=True)

    res = pd.DataFrame(rows)[["config", "n", "win%", "total$", "avg$", "PF", "best$", "worst$", "errs"]]
    res.to_csv(a.out, index=False)
    print("=" * 70)
    print(res.to_string(index=False))


if __name__ == "__main__":
    main()

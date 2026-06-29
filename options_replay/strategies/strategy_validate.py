"""Fase 3 — Validación cruzada: señales de Pine (List of Trades export) vs Python.

Compara por (ticker, fecha): coincidencia de dirección y hora; reporta solo-Pine,
solo-Python y discrepancias de dirección. Reutilizable para cualquier estrategia.

Uso:
    py strategies/strategy_validate.py --pine SPY_export.xlsx --ticker SPY \
        --start 2026-01-01 --end 2026-06-23
    # o contra un CSV de Python ya generado:
    py strategies/strategy_validate.py --pine SPY_export.xlsx --ticker SPY --pyc data/signals_tr_ud_15m.csv
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_EXCHANGES = {"AMEX", "NASDAQ", "NYSE", "BATS", "ARCA", "CBOE"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return s.strip().lower()


def _ticker_from_filename(name: str) -> str | None:
    base = os.path.basename(name)
    toks = re.split(r"[_\-.\s]+", base)
    for i, t in enumerate(toks):
        if t.upper() in _EXCHANGES and i + 1 < len(toks):
            return toks[i + 1].upper()
    return None


def parse_pine_export(path: str, ticker: str | None = None) -> pd.DataFrame:
    """List of Trades (XLSX/CSV) de TradingView -> [ticker, fecha, hora, tipo(CALL/PUT)].
    Conserva las filas de ENTRADA (Entry long=CALL, Entry short=PUT)."""
    data = open(path, "rb").read()
    frames: list[pd.DataFrame] = []
    if path.lower().endswith((".xlsx", ".xls")):
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
        frames = list(sheets.values())
    else:
        frames = [pd.read_csv(io.BytesIO(data), sep=None, engine="python")]

    tk = (ticker or _ticker_from_filename(path) or "").upper()
    for df in frames:
        cols = {_norm(c): c for c in df.columns}
        type_col = next((cols[k] for k in cols if k in ("type", "tipo")), None)
        dt_col = next((cols[k] for k in cols
                       if k in ("date/time", "date and time", "datetime", "fecha/hora",
                                "fecha y hora", "fecha", "date")), None)
        if not type_col or not dt_col:
            continue
        d = df[[type_col, dt_col]].copy()
        d.columns = ["type", "dt"]
        d["typen"] = d["type"].map(_norm)
        d = d[d["typen"].str.contains("entry")]          # sólo entradas
        if d.empty:
            continue
        d["tipo"] = d["typen"].map(lambda t: "CALL" if "long" in t else ("PUT" if "short" in t else None))
        ts = pd.to_datetime(d["dt"], errors="coerce", format="mixed")
        out = pd.DataFrame({
            "ticker": tk, "fecha": ts.dt.strftime("%Y-%m-%d"),
            "hora": ts.dt.strftime("%H:%M"), "tipo": d["tipo"],
        }).dropna(subset=["fecha", "tipo"])
        return out.reset_index(drop=True)
    raise ValueError("No encontré una hoja con columnas Type + Date/Time (List of Trades).")


def _py_signals(ticker, start, end, pyc) -> pd.DataFrame:
    if pyc:
        df = pd.read_csv(pyc)
        df = df[df["ticker"].str.upper() == ticker.upper()]
        return df.rename(columns={"hora_et": "hora", "direccion": "tipo"})[["ticker", "fecha", "hora", "tipo"]]
    from strategies.run_detect import detect
    from strategies.trend_reversal_bb_15m import TrendReversalBB15m
    sig = detect(ticker, start, end, TrendReversalBB15m())
    return sig.rename(columns={"hora_et": "hora", "direccion": "tipo"})[["ticker", "fecha", "hora", "tipo"]]


def compare(pine: pd.DataFrame, py: pd.DataFrame, start="", end="") -> dict:
    def _key(df):
        d = df.copy()
        d["fecha"] = d["fecha"].astype(str)
        if start:
            d = d[d["fecha"] >= start]
        if end:
            d = d[d["fecha"] <= end]
        return d.set_index("fecha")
    P, Y = _key(pine), _key(py)
    fechas = sorted(set(P.index) | set(Y.index))
    match, dir_mismatch, only_pine, only_py = [], [], [], []
    for f in fechas:
        inP, inY = f in P.index, f in Y.index
        if inP and inY:
            tp, ty = P.loc[f, "tipo"], Y.loc[f, "tipo"]
            tp = tp if isinstance(tp, str) else tp.iloc[0]
            ty = ty if isinstance(ty, str) else ty.iloc[0]
            (match if tp == ty else dir_mismatch).append((f, tp, ty))
        elif inP:
            only_pine.append((f, P.loc[f, "tipo"]))
        else:
            only_py.append((f, Y.loc[f, "tipo"]))
    n = len(fechas)
    return {"n_fechas": n, "match": match, "dir_mismatch": dir_mismatch,
            "only_pine": only_pine, "only_python": only_py,
            "pct": round(100 * len(match) / n, 1) if n else 0.0}


def _print(rep: dict):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("== Validacion cruzada Pine vs Python ==")
    print(f"  fechas con senal (union): {rep['n_fechas']}")
    print(f"  [OK]    coinciden (fecha+direccion): {len(rep['match'])}  ({rep['pct']}%)")
    print(f"  [DIR!=] direccion distinta: {len(rep['dir_mismatch'])}")
    print(f"  [<PINE] solo Pine: {len(rep['only_pine'])}   [PY>] solo Python: {len(rep['only_python'])}")
    for f, tp, ty in rep["dir_mismatch"]:
        print(f"     MISMATCH {f}: Pine={tp} Python={ty}")
    for f, t in rep["only_pine"]:
        print(f"     SOLO-PINE   {f}: {t}")
    for f, t in rep["only_python"]:
        print(f"     SOLO-PYTHON {f}: {t}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pine", required=True, help="Archivo List of Trades (XLSX/CSV) de Pine")
    ap.add_argument("--ticker", default="")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--pyc", default="", help="CSV de señales Python ya generado (opcional)")
    args = ap.parse_args()
    pine = parse_pine_export(args.pine, args.ticker or None)
    tk = args.ticker or (pine["ticker"].iloc[0] if not pine.empty else "")
    py = _py_signals(tk, args.start, args.end, args.pyc)
    _print(compare(pine, py, args.start, args.end))
    return 0


if __name__ == "__main__":
    sys.exit(main())

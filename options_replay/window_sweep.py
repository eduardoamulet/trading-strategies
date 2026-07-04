"""Walk-forward rolling-origin para elegir la VENTANA del playbook (revisión TRIMESTRAL).

Para cada sesión t del almacén y cada ventana W: construye el veredicto SOLO con los W días
hábiles estrictamente anteriores a t (sin fuga por construcción: las posiciones son intradía)
y registra el ROI de cartera REALIZADO en t del escenario recomendado — únicamente si ese
día-semana estaba OPERAR + estado operable. Corre SIN histéresis ni churn/vol (mide la calidad
de la VENTANA; estado y calibración sí corren porque son parte del veredicto puro).

Reporta por ventana: sesiones evaluables (ventana completa ESTRICTA — con poca historia las W
grandes evalúan menos sesiones y no son directamente comparables), % operadas, ROI/día operado,
WR realizado, margen de calibración (WR realizado − P5 prometido medio; NEGATIVO = el P5 no es
un piso honesto → descartar esa W), ROI acumulado, max drawdown y churn crudo del campeón.

Criterio de decisión (política oficial): elegir la W con mejor ROI/día operado SUJETO a margen
de calibración ≥ 0 y churn bajo; ante empate, la W en el CENTRO de la meseta estable (principio
de meseta), no el pico.

Uso:
  python window_sweep.py                                  # W = 40,60,90,120,180
  python window_sweep.py --windows 60,90,120 --csv data/window_sweep.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import bt_store  # noqa: E402
from playbook_store import compute_weighted_verdict  # noqa: E402


def sweep(windows: list[int], *, half_life: int = 35, min_n: int = 16) -> pd.DataFrame:
    dates = bt_store.distinct_dates()
    if len(dates) <= min(windows) + 5:
        raise ValueError(f"almacén con {len(dates)} días — insuficiente para el sweep "
                         f"(mínimo {min(windows) + 6}).")
    df = bt_store.load_range(dates[0], dates[-1])

    # ROI de cartera realizado por (fecha, escenario) y día-semana de cada fecha — 1 sola pasada.
    from bt_analysis import detailed as det
    d = det.prepare(df)
    d["_f"] = d["date"].dt.strftime("%Y-%m-%d")
    g = d.groupby(["_f", "id"]).agg(pnl=("pnl", "sum"), inv=("inv", "sum"))
    g["roi"] = g["pnl"] / g["inv"].replace(0, np.nan) * 100.0
    realized = g["roi"].dropna().to_dict()                    # (fecha, id) → ROI cartera %
    wd_of = dict(zip(d["_f"], d["weekday"]))

    filas = []
    for W in windows:
        rois: list[float] = []
        p5s: list[float] = []
        campeones: dict = {}
        flips = n_eval = 0
        for i, t in enumerate(dates):
            if i < W:
                continue                                      # ventana completa ESTRICTA
            n_eval += 1
            win = dates[i - W:i]                              # solo días < t (sin fuga)
            per_day, _ = compute_weighted_verdict(df, win, half_life=half_life, min_n=min_n)
            info = (per_day or {}).get(wd_of.get(t)) or {}
            sid = info.get("scenario")
            if sid:
                if campeones.get(wd_of.get(t)) and campeones[wd_of.get(t)] != sid:
                    flips += 1
                campeones[wd_of.get(t)] = sid
            if info.get("recommendation") == "OPERAR" and info.get("estado") == "operable":
                r = realized.get((t, sid))
                if r is not None:
                    rois.append(float(r))
                    p5s.append(float(info.get("wr_p5") or 0))
        if not n_eval:
            filas.append({"W": W, "sesiones": 0})
            continue
        rr = pd.Series(rois, dtype=float)
        cum = rr.cumsum()
        dd = float((cum - cum.cummax()).min()) if len(rr) else 0.0
        wr = float((rr > 0).mean() * 100) if len(rr) else np.nan
        p5m = float(np.mean(p5s)) if p5s else np.nan
        filas.append({
            "W": W, "sesiones": n_eval, "operadas": len(rr),
            "% operadas": round(len(rr) / n_eval * 100, 1),
            "ROI/día op %": round(float(rr.mean()), 2) if len(rr) else np.nan,
            "WR realizado %": round(wr, 1) if wr == wr else np.nan,
            "P5 prometido %": round(p5m, 1) if p5m == p5m else np.nan,
            "margen calib": round(wr - p5m, 1) if (wr == wr and p5m == p5m) else np.nan,
            "ROI acum %": round(float(rr.sum()), 1) if len(rr) else 0.0,
            "max DD %": round(dd, 1),
            "churn (flips)": flips,
        })
    return pd.DataFrame(filas)


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward de ventanas del playbook (trimestral)")
    ap.add_argument("--windows", default="40,60,90,120,180",
                    help="Ventanas a comparar, separadas por coma (días hábiles).")
    ap.add_argument("--half-life", type=int, default=35)
    ap.add_argument("--min-n", type=int, default=16)
    ap.add_argument("--csv", default="", help="Ruta opcional para exportar la tabla en CSV.")
    args = ap.parse_args()
    windows = sorted({int(w) for w in args.windows.split(",") if w.strip()})

    out = sweep(windows, half_life=args.half_life, min_n=args.min_n)
    print(out.to_string(index=False))
    if args.csv:
        out.to_csv(args.csv, index=False)
        print(f"→ {args.csv}")
    print("\nLectura: mejor ROI/día operado SUJETO a margen calib ≥ 0 (el WR realizado no puede "
          "quedar bajo el P5 prometido) y churn bajo. Ante empate, el CENTRO de la meseta "
          "estable, no el pico. Las W con pocas «sesiones» todavía no son concluyentes.")


if __name__ == "__main__":
    main()

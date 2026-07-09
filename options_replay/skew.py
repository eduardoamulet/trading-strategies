"""SKEW de volatilidad diario desde los snapshots de cadena — termómetro de DEMANDA DE
PROTECCIÓN institucional (el segundo clasificador de régimen, junto al GEX).

    skew_pts = IV(put ~25Δ) − IV(call ~25Δ)      (en puntos de IV, ×100)

Skew alto/subiendo → pagan caro el seguro a la baja (miedo persistente, sesgo bajista de
fondo). Skew aplanándose → risk-on. Se computa sobre el VENCIMIENTO MÁS CERCANO capturado
(el universo 0-5 DTE del snapshot) con los deltas del propio snapshot; también deja la IV
ATM (~50Δ) como nivel general. Cache en skew_daily. LÍMITE honesto: es una foto 2×/día para
leer TENDENCIA entre días — no un gatillo intradía.

CLI:
    py skew.py                      → skew de HOY (apertura, QQQ/SPY/IWM)
    py skew.py --backfill           → computa todos los snapshots capturados
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "data" / "chain_snapshots.db"
TICKERS = ["QQQ", "SPY", "IWM"]


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS skew_daily (
        fecha TEXT NOT NULL, momento TEXT NOT NULL, ticker TEXT NOT NULL,
        skew_pts REAL,            -- IV(put 25Δ) − IV(call 25Δ), en puntos (×100)
        iv_put25 REAL, iv_call25 REAL, atm_iv REAL,
        expiration TEXT, n_contratos INTEGER, computado_en TEXT,
        PRIMARY KEY (fecha, momento, ticker))""")
    return con


def compute_skew(fecha: str, ticker: str, momento: str = "apertura",
                 path: Path = DB_PATH) -> dict | None:
    """Skew 25Δ del vencimiento más cercano del snapshot. None sin datos utilizables."""
    con = _connect(path)
    exp = con.execute(
        "SELECT MIN(expiration) FROM chain_snapshot WHERE fecha=? AND momento=? AND ticker=? "
        "AND expiration >= ? AND iv IS NOT NULL AND delta IS NOT NULL",
        (fecha, momento, ticker, fecha)).fetchone()[0]
    if not exp:
        return None
    filas = con.execute(
        "SELECT tipo, delta, iv FROM chain_snapshot WHERE fecha=? AND momento=? AND ticker=? "
        "AND expiration=? AND iv IS NOT NULL AND delta IS NOT NULL AND iv > 0",
        (fecha, momento, ticker, exp)).fetchall()
    if not filas:
        return None

    def _cercano(objetivo: float, lado: str):
        cand = [(abs(float(d) - objetivo), float(iv)) for t, d, iv in filas
                if str(t or "").lower().startswith(lado)]
        return min(cand)[1] if cand else None

    iv_p25 = _cercano(-0.25, "p")
    iv_c25 = _cercano(0.25, "c")
    # ATM: promedio de |Δ| más cercano a 0.50 de cada lado (el que exista).
    atm_c = _cercano(0.50, "c")
    atm_p = _cercano(-0.50, "p")
    atm = (sum(v for v in (atm_c, atm_p) if v is not None)
           / max(1, sum(1 for v in (atm_c, atm_p) if v is not None))) \
        if (atm_c is not None or atm_p is not None) else None
    if iv_p25 is None or iv_c25 is None:
        return None
    out = {
        "fecha": fecha, "momento": momento, "ticker": ticker,
        "skew_pts": round((iv_p25 - iv_c25) * 100.0, 2),
        "iv_put25": round(iv_p25, 4), "iv_call25": round(iv_c25, 4),
        "atm_iv": (round(atm, 4) if atm is not None else None),
        "expiration": exp, "n_contratos": len(filas),
        "computado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    cols = ",".join(out)
    con.execute(f"INSERT OR REPLACE INTO skew_daily ({cols}) VALUES "
                f"({','.join('?' for _ in out)})", list(out.values()))
    con.commit()
    return out


def leer_skew(fecha: str, ticker: str, momento: str = "apertura",
              path: Path = DB_PATH) -> dict | None:
    con = _connect(path)
    row = con.execute(
        "SELECT fecha, momento, ticker, skew_pts, iv_put25, iv_call25, atm_iv, expiration, "
        "n_contratos FROM skew_daily WHERE fecha=? AND momento=? AND ticker=?",
        (fecha, momento, ticker)).fetchone()
    if row:
        keys = ("fecha", "momento", "ticker", "skew_pts", "iv_put25", "iv_call25",
                "atm_iv", "expiration", "n_contratos")
        return dict(zip(keys, row))
    return compute_skew(fecha, ticker, momento, path=path)


def skew_mas_reciente(ticker: str, hasta_fecha: str, path: Path = DB_PATH) -> dict | None:
    con = _connect(path)
    row = con.execute(
        "SELECT fecha, momento FROM chain_snapshot WHERE ticker=? AND fecha<=? "
        "ORDER BY fecha DESC, CASE momento WHEN 'cierre' THEN 1 ELSE 0 END DESC LIMIT 1",
        (ticker, hasta_fecha)).fetchone()
    return leer_skew(row[0], ticker, row[1], path=path) if row else None


def backfill(path: Path = DB_PATH) -> int:
    con = _connect(path)
    todos = con.execute("SELECT DISTINCT fecha, momento, ticker FROM chain_snapshot").fetchall()
    hechos = set(con.execute("SELECT fecha, momento, ticker FROM skew_daily").fetchall())
    n = 0
    for f, m, t in sorted(todos):
        if (f, m, t) not in hechos and compute_skew(f, t, m, path=path):
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Skew 25Δ diario desde los snapshots")
    ap.add_argument("--fecha", default=None)
    ap.add_argument("--momento", default="apertura", choices=["apertura", "cierre"])
    ap.add_argument("--tickers", default=",".join(TICKERS))
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()
    if args.backfill:
        print(f"backfill: {backfill()} (fecha, momento, ticker) computados")
        return 0
    from datetime import date
    fecha = args.fecha or date.today().isoformat()
    for tk in [t.strip().upper() for t in args.tickers.split(",") if t.strip()]:
        s = leer_skew(fecha, tk, args.momento) or skew_mas_reciente(tk, fecha)
        if not s:
            print(f"{tk}: sin snapshot utilizable ≤ {fecha}")
            continue
        print(f"{tk} [{s['fecha']} {s['momento']} · venc {s['expiration']}]: "
              f"skew {s['skew_pts']:+.2f} pts (putIV {s['iv_put25']:.3f} − "
              f"callIV {s['iv_call25']:.3f}) · ATM {s['atm_iv']}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())

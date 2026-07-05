"""Captura diaria del Snapshot de la cadena de opciones → data/chain_snapshots.db.

LA BASE DE GREEKS/IV/OI PROPIA: Polygon solo expone el estado ACTUAL de la cadena (no existe
serie histórica REST de greeks/IV/OI) → cada día capturado es historia que no se puede comprar
retroactivamente. Lo corren dos Tareas Programadas (hora local = hora de NY en esta máquina):
  · «SignalForge Snapshot Apertura» — 09:35 (el OI de esta captura es el consolidado D-1, el bueno)
  · «SignalForge Snapshot Cierre»   — 15:45 (estado de greeks/IV al final del día)

Qué guarda: contratos con DTE ≤ MAX_DTE y strike dentro de ±BAND del spot (el universo 0-1DTE
que operamos + margen para GEX/skew). Append-only, PK (fecha, momento, ticker, occ) — recapturar
el mismo momento deduplica solo. `capturado_en` guarda el timestamp REAL (si la tarea corrió
tarde por laptop apagada, el análisis puede filtrarlo).

Uso manual:  python chain_snapshots.py --momento apertura [--force] [--tickers SPY,QQQ]
`--force` ignora el guard de día hábil (para pruebas en finde/feriado).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))               # config.py en la raíz Traiding
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # consolas cp1252 (tarea programada)

DB_PATH = HERE / "data" / "chain_snapshots.db"
MAX_DTE = 5           # vencimientos capturados: hoy .. hoy+5 (0DTE-semanales; GEX cercano)
BAND = 0.10           # strikes dentro de ±10 % del spot
TICKERS = ["SPY", "QQQ", "IWM", "DIA", "NVDA", "TSLA", "AMD", "COIN", "META", "AAPL", "SPX"]

_COLS = ["fecha", "momento", "ticker", "occ", "expiration", "tipo", "strike",
         "oi", "volumen", "iv", "delta", "gamma", "theta", "vega",
         "bid", "ask", "mid", "last_price", "spot", "capturado_en"]


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS chain_snapshot (
        fecha TEXT NOT NULL, momento TEXT NOT NULL, ticker TEXT NOT NULL, occ TEXT NOT NULL,
        expiration TEXT, tipo TEXT, strike REAL,
        oi INTEGER, volumen INTEGER,
        iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
        bid REAL, ask REAL, mid REAL, last_price REAL,
        spot REAL, capturado_en TEXT,
        PRIMARY KEY (fecha, momento, ticker, occ))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_snap_ticker ON chain_snapshot (ticker, fecha)")
    return con


def _es_dia_habil(iso: str) -> bool:
    from ucbatch.runner import _NYSE_HOLIDAYS
    d = date.fromisoformat(iso)
    return d.weekday() < 5 and iso not in _NYSE_HOLIDAYS


def _parse_item(item: dict, ticker: str, fecha: str, momento: str, ts: str) -> dict | None:
    """Snapshot v3 → fila plana. Devuelve None si faltan los campos identitarios."""
    det = item.get("details") or {}
    occ = det.get("ticker") or ""
    strike = det.get("strike_price")
    if not occ or strike is None:
        return None
    q = item.get("last_quote") or {}
    g = item.get("greeks") or {}
    day = item.get("day") or {}
    bid, ask = q.get("bid"), q.get("ask")
    mid = round((bid + ask) / 2, 4) if (bid is not None and ask is not None) else None
    return {"fecha": fecha, "momento": momento, "ticker": ticker, "occ": occ,
            "expiration": det.get("expiration_date"), "tipo": det.get("contract_type"),
            "strike": float(strike),
            "oi": item.get("open_interest"), "volumen": day.get("volume"),
            "iv": item.get("implied_volatility"),
            "delta": g.get("delta"), "gamma": g.get("gamma"),
            "theta": g.get("theta"), "vega": g.get("vega"),
            "bid": bid, "ask": ask, "mid": mid,
            "last_price": (item.get("last_trade") or {}).get("price"),
            # ETFs/acciones traen "price"; los ÍNDICES (SPX…) traen "value".
            "spot": ((item.get("underlying_asset") or {}).get("price")
                     or (item.get("underlying_asset") or {}).get("value")),
            "capturado_en": ts}


def capture(momento: str, tickers: list | None = None, db_path: Path = DB_PATH,
            force: bool = False, adapter=None) -> int:
    """Captura la cadena de todos los tickers y la persiste. Devuelve filas nuevas."""
    hoy = date.today().isoformat()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not force and not _es_dia_habil(hoy):
        print(f"{ts} snapshot {momento}: {hoy} no es día hábil NYSE — nada que capturar.")
        return 0
    if adapter is None:
        import config
        from adapter_polygon import PolygonAdapter
        adapter = PolygonAdapter(config.POLYGON_API_KEY,
                                 rate_limit_per_min=int(getattr(config,
                                     "POLYGON_RATE_LIMIT_PER_MIN", 600)))
    import pandas as pd
    exp_hasta = (pd.bdate_range(hoy, periods=MAX_DTE + 1)[-1]).date().isoformat()

    total_nuevas = 0
    with _connect(db_path) as con, con:
        for tk in (tickers or TICKERS):
            try:
                items = adapter.option_chain_snapshot(tk, expiration_gte=hoy,
                                                      expiration_lte=exp_hasta)
            except Exception as e:  # noqa: BLE001 — un ticker caído no tumba la captura
                print(f"{ts} snapshot {momento} {tk}: ERROR {type(e).__name__}: {e}")
                continue
            rows = [r for r in (_parse_item(i, tk, hoy, momento, ts) for i in items) if r]
            # banda de strikes ±BAND alrededor del spot (si el snapshot lo trae)
            spots = [r["spot"] for r in rows if r.get("spot")]
            if spots:
                spot = spots[0]
                rows = [r for r in rows if abs(r["strike"] - spot) <= spot * BAND]
            before = con.execute("SELECT COUNT(*) FROM chain_snapshot").fetchone()[0]
            con.executemany(
                f"INSERT OR IGNORE INTO chain_snapshot VALUES ({','.join('?' * len(_COLS))})",
                [tuple(r[c] for c in _COLS) for r in rows])
            nuevas = con.execute("SELECT COUNT(*) FROM chain_snapshot").fetchone()[0] - before
            total_nuevas += nuevas
            print(f"{ts} snapshot {momento} {tk}: {len(rows)} contratos (+{nuevas} nuevos)")
    print(f"{ts} snapshot {momento}: TOTAL +{total_nuevas} filas -> {db_path.name}")
    return total_nuevas


def main() -> None:
    ap = argparse.ArgumentParser(description="Captura del snapshot de la cadena (greeks/IV/OI).")
    ap.add_argument("--momento", required=True, choices=["apertura", "cierre"])
    ap.add_argument("--tickers", default="", help="Coma-separados (default: la lista estándar).")
    ap.add_argument("--force", action="store_true", help="Capturar aunque no sea día hábil.")
    a = ap.parse_args()
    tks = [t.strip().upper() for t in a.tickers.split(",") if t.strip()] or None
    capture(a.momento, tickers=tks, force=a.force)


if __name__ == "__main__":
    main()

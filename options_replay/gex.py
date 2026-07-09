"""GEX (Gamma Exposure) diario desde los snapshots de cadena — el clasificador de RÉGIMEN.

Convención «naive» estándar (SqueezeMetrics/SpotGamma): los dealers quedan LARGOS gamma por
las calls (flujo covered-call: el cliente vende, el dealer compra) y CORTOS por las puts (el
cliente compra protección, el dealer queda vendido). Por contrato, en $ por movimiento del 1%:

    GEX_contrato = gamma × OI × 100 × spot² × 0.01      (calls +, puts −)

GEX_total > 0 → dealers largos gamma → régimen de RANGO/pinning (amortiguan el movimiento).
GEX_total < 0 → dealers cortos gamma → régimen de TENDENCIA (sus coberturas la aceleran).

Niveles derivados:
  · flip (zero-gamma): nivel donde el GEX acumulado por strike (de abajo hacia arriba) cruza
    cero, interpolado entre strikes. Aproximación reconocida — NO se re-precia la superficie.
  · call_wall: strike con mayor GEX de calls (freno/imán arriba).
  · put_wall:  strike con mayor |GEX| de puts (freno/imán abajo).

Datos: data/chain_snapshots.db (capturas 09:35 apertura / 15:45 cierre; gamma/OI/strike/spot
por contrato, DTE ≤ 5 y strikes ±10% del spot — el núcleo donde vive la gamma). El OI de la
APERTURA es el consolidado D-1 (el bueno). Resultados cacheados en la tabla gex_daily.

LÍMITES (honestos): la convención asume de qué lado están los dealers (es la estándar, no una
verdad); el OI es T+1 (el OI del propio 0DTE se forma durante el día y no está); el universo
±10% recorta colas (irrelevantes en gamma, enormes en vega). Úsese como CLASIFICADOR de
régimen y niveles de referencia, no como oráculo.

CLI:
    py gex.py                      → GEX de HOY (apertura, QQQ/SPY/IWM)
    py gex.py --fecha 2026-07-07   → GEX de esa fecha
    py gex.py --backfill           → computa TODOS los (fecha, momento, ticker) capturados
    py gex.py --tickers SPY,SPX    → otros tickers del snapshot
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
    con.execute("""CREATE TABLE IF NOT EXISTS gex_daily (
        fecha TEXT NOT NULL, momento TEXT NOT NULL, ticker TEXT NOT NULL,
        gex_total_musd REAL,      -- Σ GEX en millones de $ por 1% de movimiento
        regimen TEXT,             -- 'rango (GEX+)' | 'tendencia (GEX-)'
        flip REAL,                -- nivel zero-gamma interpolado (None si no cruza)
        call_wall REAL, put_wall REAL,
        spot REAL, spot_vs_flip_pct REAL,
        n_contratos INTEGER, computado_en TEXT,
        PRIMARY KEY (fecha, momento, ticker))""")
    return con


def compute_gex(fecha: str, ticker: str, momento: str = "apertura",
                path: Path = DB_PATH) -> dict | None:
    """Computa el GEX de (fecha, momento, ticker) desde el snapshot y lo cachea en gex_daily.
    None si ese snapshot no existe o no tiene gamma/OI utilizables."""
    con = _connect(path)
    filas = con.execute(
        "SELECT strike, tipo, gamma, oi, spot FROM chain_snapshot "
        "WHERE fecha=? AND momento=? AND ticker=? AND gamma IS NOT NULL AND oi IS NOT NULL "
        "AND oi > 0 AND strike IS NOT NULL",
        (fecha, momento, ticker)).fetchall()
    if not filas:
        return None
    spot = next((f[4] for f in filas if f[4]), None)
    if not spot:
        return None
    spot = float(spot)

    por_strike: dict[float, float] = {}
    call_gex: dict[float, float] = {}
    put_gex: dict[float, float] = {}
    total = 0.0
    n = 0
    for strike, tipo, gamma, oi, _ in filas:
        try:
            g = float(gamma) * float(oi) * 100.0 * spot * spot * 0.01
        except (TypeError, ValueError):
            continue
        k = float(strike)
        es_call = str(tipo or "").lower().startswith("c")
        signed = g if es_call else -g
        total += signed
        por_strike[k] = por_strike.get(k, 0.0) + signed
        (call_gex if es_call else put_gex).setdefault(k, 0.0)
        if es_call:
            call_gex[k] += g
        else:
            put_gex[k] += g
        n += 1
    if not n:
        return None

    # flip: primer cruce por cero del acumulado por strike (interpolado).
    flip = None
    strikes = sorted(por_strike)
    cum = 0.0
    prev_k, prev_cum = None, None
    for k in strikes:
        cum += por_strike[k]
        if prev_cum is not None and (prev_cum < 0) != (cum < 0) and cum != prev_cum:
            # cruce entre prev_k y k → interpolación lineal sobre el acumulado
            frac = abs(prev_cum) / abs(cum - prev_cum)
            flip = round(prev_k + (k - prev_k) * frac, 2)
            break
        prev_k, prev_cum = k, cum

    call_wall = max(call_gex, key=call_gex.get) if call_gex else None
    put_wall = max(put_gex, key=put_gex.get) if put_gex else None
    out = {
        "fecha": fecha, "momento": momento, "ticker": ticker,
        "gex_total_musd": round(total / 1e6, 1),
        "regimen": "rango (GEX+)" if total > 0 else "tendencia (GEX-)",
        "flip": flip,
        "call_wall": call_wall, "put_wall": put_wall,
        "spot": round(spot, 2),
        "spot_vs_flip_pct": (round((spot - flip) / flip * 100.0, 2) if flip else None),
        "n_contratos": n,
        "computado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    cols = ",".join(out)
    con.execute(f"INSERT OR REPLACE INTO gex_daily ({cols}) VALUES "
                f"({','.join('?' for _ in out)})", list(out.values()))
    con.commit()
    return out


def leer_gex(fecha: str, ticker: str, momento: str = "apertura",
             path: Path = DB_PATH) -> dict | None:
    """Lee el GEX cacheado (o lo computa si el snapshot existe y aún no se calculó)."""
    con = _connect(path)
    row = con.execute(
        "SELECT fecha, momento, ticker, gex_total_musd, regimen, flip, call_wall, put_wall, "
        "spot, spot_vs_flip_pct, n_contratos FROM gex_daily "
        "WHERE fecha=? AND momento=? AND ticker=?", (fecha, momento, ticker)).fetchone()
    if row:
        keys = ("fecha", "momento", "ticker", "gex_total_musd", "regimen", "flip",
                "call_wall", "put_wall", "spot", "spot_vs_flip_pct", "n_contratos")
        return dict(zip(keys, row))
    return compute_gex(fecha, ticker, momento, path=path)


def gex_mas_reciente(ticker: str, hasta_fecha: str, path: Path = DB_PATH) -> dict | None:
    """El GEX más fresco disponible ≤ hasta_fecha (apertura de hoy si ya se capturó; si no,
    el cierre/apertura previo). Útil a las 09:31, cuando el snapshot de 09:35 aún no llegó —
    la referencia usada queda en fecha/momento del resultado."""
    con = _connect(path)
    row = con.execute(
        "SELECT fecha, momento FROM chain_snapshot WHERE ticker=? AND fecha<=? "
        "ORDER BY fecha DESC, CASE momento WHEN 'cierre' THEN 1 ELSE 0 END DESC LIMIT 1",
        (ticker, hasta_fecha)).fetchone()
    if not row:
        return None
    return leer_gex(row[0], ticker, row[1], path=path)


def backfill(path: Path = DB_PATH) -> int:
    """Computa el GEX de TODOS los (fecha, momento, ticker) capturados que falten."""
    con = _connect(path)
    todos = con.execute("SELECT DISTINCT fecha, momento, ticker FROM chain_snapshot").fetchall()
    hechos = set(con.execute("SELECT fecha, momento, ticker FROM gex_daily").fetchall())
    n = 0
    for f, m, t in sorted(todos):
        if (f, m, t) in hechos:
            continue
        if compute_gex(f, t, m, path=path):
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="GEX diario desde los snapshots de cadena")
    ap.add_argument("--fecha", default=None, help="YYYY-MM-DD (default: hoy)")
    ap.add_argument("--momento", default="apertura", choices=["apertura", "cierre"])
    ap.add_argument("--tickers", default=",".join(TICKERS))
    ap.add_argument("--backfill", action="store_true",
                    help="computar todos los snapshots capturados que falten")
    args = ap.parse_args()
    if args.backfill:
        n = backfill()
        print(f"backfill: {n} (fecha, momento, ticker) computados")
        return 0
    from datetime import date
    fecha = args.fecha or date.today().isoformat()
    for tk in [t.strip().upper() for t in args.tickers.split(",") if t.strip()]:
        g = leer_gex(fecha, tk, args.momento) or gex_mas_reciente(tk, fecha)
        if not g:
            print(f"{tk}: sin snapshot utilizable ≤ {fecha}")
            continue
        print(f"{tk} [{g['fecha']} {g['momento']}]: {g['regimen']} · "
              f"GEX {g['gex_total_musd']:+,.1f} M$/1% · flip {g['flip']} · "
              f"spot {g['spot']} ({g['spot_vs_flip_pct']:+.2f}% vs flip)"
              if g.get("spot_vs_flip_pct") is not None else
              f"{tk} [{g['fecha']} {g['momento']}]: {g['regimen']} · "
              f"GEX {g['gex_total_musd']:+,.1f} M$/1% · flip — · spot {g['spot']}")
        print(f"   call wall {g['call_wall']} · put wall {g['put_wall']} · "
              f"n={g['n_contratos']}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())

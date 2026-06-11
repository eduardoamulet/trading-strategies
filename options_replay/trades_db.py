"""Almacén SQLite del 'Registro de trades' (bitácora: checklist de requisitos + datos
del trade), modelado sobre signals_db.

Cada registro = 1 plan/trade del día. Los campos estructurados (checklist, grilla por
día, filas del trade) se guardan como JSON; algunos campos clave (fecha, ticker,
rentabilidad total) se denormalizan en columnas para listar/ordenar.

DB en data/trades.db (gitignored, *.db). Sin servidor: un archivo local.
"""
from __future__ import annotations

import json as _json
import sqlite3
import uuid as _uuid
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
DB_PATH = HERE / "data" / "trades.db"   # gitignored (*.db)

# Columnas físicas de la tabla (las JSON guardan el detalle del template).
COLUMNS = [
    "id", "fecha", "ticker", "rango_precio",
    "checklist_json", "grid_json", "trades_json", "notas",
    "rentabilidad_total", "creado_en", "actualizado_en",
]


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id                 TEXT PRIMARY KEY,
                fecha              TEXT,
                ticker             TEXT,
                rango_precio       TEXT,
                checklist_json     TEXT,
                grid_json          TEXT,
                trades_json        TEXT,
                notas              TEXT,
                rentabilidad_total REAL DEFAULT 0,
                creado_en          TEXT,
                actualizado_en     TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS ix_trades_fecha ON trades(fecha)")


def _dump(v) -> str:
    return _json.dumps(v, ensure_ascii=False, default=str)


def _row_from_rec(rec: dict, now_iso: str) -> dict:
    """dict de la UI → fila física (serializa los bloques estructurados a JSON)."""
    trades = rec.get("trades") or []
    rent_total = 0.0
    for t in trades:
        try:
            rent_total += float(t.get("Rentabilidad $") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "fecha": str(rec.get("fecha") or ""),
        "ticker": str(rec.get("ticker") or "").upper().strip(),
        "rango_precio": str(rec.get("rango_precio") or ""),
        "checklist_json": _dump(rec.get("checklist") or {}),
        "grid_json": _dump(rec.get("grid") or {}),
        "trades_json": _dump(trades),
        "notas": str(rec.get("notas") or ""),
        "rentabilidad_total": rent_total,
        "actualizado_en": now_iso,
    }


def add_trade(rec: dict, now_iso: str) -> str:
    """Inserta un registro NUEVO. Devuelve su id."""
    init_db()
    row = _row_from_rec(rec, now_iso)
    new_id = str(_uuid.uuid4())
    with _conn() as con:
        con.execute(
            "INSERT INTO trades (id, fecha, ticker, rango_precio, checklist_json, grid_json, "
            "trades_json, notas, rentabilidad_total, creado_en, actualizado_en) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, row["fecha"], row["ticker"], row["rango_precio"], row["checklist_json"],
             row["grid_json"], row["trades_json"], row["notas"], row["rentabilidad_total"],
             now_iso, now_iso),
        )
    return new_id


def update_trade(trade_id: str, rec: dict, now_iso: str) -> int:
    """Actualiza un registro existente (por id). Filas afectadas."""
    init_db()
    row = _row_from_rec(rec, now_iso)
    with _conn() as con:
        cur = con.execute(
            "UPDATE trades SET fecha=?, ticker=?, rango_precio=?, checklist_json=?, grid_json=?, "
            "trades_json=?, notas=?, rentabilidad_total=?, actualizado_en=? WHERE id=?",
            (row["fecha"], row["ticker"], row["rango_precio"], row["checklist_json"],
             row["grid_json"], row["trades_json"], row["notas"], row["rentabilidad_total"],
             now_iso, str(trade_id)),
        )
        return cur.rowcount


def delete_trades(ids) -> int:
    """Borra los registros cuyos id estén en `ids`. Devuelve cuántos borró."""
    ids = [str(i) for i in (ids or []) if str(i)]
    if not ids:
        return 0
    init_db()
    with _conn() as con:
        ph = ",".join("?" * len(ids))
        cur = con.execute(f"DELETE FROM trades WHERE id IN ({ph})", ids)
        return cur.rowcount


def get_trade(trade_id: str) -> dict | None:
    """Un registro por id, con los bloques JSON ya deserializados."""
    init_db()
    with _conn() as con:
        r = con.execute("SELECT * FROM trades WHERE id=?", (str(trade_id),)).fetchone()
    if r is None:
        return None
    d = dict(r)
    for k_src, k_dst in (("checklist_json", "checklist"), ("grid_json", "grid"),
                         ("trades_json", "trades")):
        try:
            d[k_dst] = _json.loads(d.get(k_src) or ("[]" if k_dst == "trades" else "{}"))
        except Exception:
            d[k_dst] = [] if k_dst == "trades" else {}
    return d


def load_trades() -> pd.DataFrame:
    """Todos los registros (más recientes primero) para el historial."""
    init_db()
    with _conn() as con:
        df = pd.read_sql_query(
            "SELECT id, fecha, ticker, rango_precio, trades_json, rentabilidad_total, "
            "creado_en, actualizado_en FROM trades ORDER BY fecha DESC, creado_en DESC",
            con,
        )
    return df


def count() -> int:
    init_db()
    with _conn() as con:
        return int(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

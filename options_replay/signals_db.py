"""Almacén SQLite de señales/alertas.

Por qué SQLite (vs Parquet): ingreso INCREMENTAL (cada email trae alertas nuevas),
DEDUP por clave única, UPDATE de estado/ganancia por fila, y queries — todo en un
archivo local sin servidor. Consistente con live_trader (que ya usa SQLite).

DB en data/signals.db (gitignored). Tabla `alerts` con UNIQUE(accion,fecha,hora,
estrategia,tipo) para que reprocesar el mismo correo no duplique.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

HERE = Path(__file__).parent
DB_PATH = HERE / "data" / "signals.db"   # gitignored (*.db)

# Columnas expuestas (espejo del Historial de Señales + metadata de ingesta).
COLUMNS = [
    "accion", "hora", "fecha", "estrategia", "cumplimiento", "tipo",
    "estado", "ganancia", "fuente", "message_id", "recibido_en", "importado_en",
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
            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                accion       TEXT NOT NULL,
                hora         TEXT,
                fecha        TEXT,
                estrategia   TEXT,
                cumplimiento REAL,
                tipo         TEXT,
                estado       TEXT,
                ganancia     REAL DEFAULT 0,
                fuente       TEXT,
                message_id   TEXT,
                recibido_en  TEXT,
                importado_en TEXT,
                UNIQUE(accion, fecha, hora, estrategia, tipo)
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS ix_alerts_fecha ON alerts(fecha)")


def _na(v):
    """NaT/NaN/NA de pandas → None (SQLite no soporta esos tipos)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def upsert_signals(df: pd.DataFrame) -> int:
    """Inserta las señales nuevas (dedup por la clave única). Devuelve cuántas
    filas NUEVAS se insertaron (las duplicadas se ignoran)."""
    if df is None or df.empty:
        return 0
    init_db()
    _cols = ("accion", "hora", "fecha", "estrategia", "cumplimiento", "tipo", "estado",
             "ganancia", "fuente", "message_id", "recibido_en", "importado_en")
    inserted = 0
    with _conn() as con:
        for _, r in df.iterrows():
            vals = [_na(r.get(c)) for c in _cols]
            if vals[7] is None:            # ganancia → 0 por defecto
                vals[7] = 0.0
            cur = con.execute(
                "INSERT OR IGNORE INTO alerts "
                "(accion,hora,fecha,estrategia,cumplimiento,tipo,estado,ganancia,"
                "fuente,message_id,recibido_en,importado_en) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                vals,
            )
            inserted += cur.rowcount
    return inserted


def load_signals() -> pd.DataFrame:
    """Todas las señales guardadas (más recientes primero)."""
    init_db()
    with _conn() as con:
        df = pd.read_sql_query(
            "SELECT accion,hora,fecha,estrategia,cumplimiento,tipo,estado,ganancia,"
            "fuente,message_id,recibido_en,importado_en "
            "FROM alerts ORDER BY fecha DESC, hora DESC",
            con,
        )
    return df


def count() -> int:
    init_db()
    with _conn() as con:
        return int(con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])


def update_status(accion: str, fecha: str, hora: str, estrategia: str, tipo: str,
                  estado: Optional[str] = None, ganancia: Optional[float] = None) -> int:
    """Actualiza estado/ganancia de una señal puntual. Devuelve filas afectadas."""
    init_db()
    sets, vals = [], []
    if estado is not None:
        sets.append("estado=?"); vals.append(estado)
    if ganancia is not None:
        sets.append("ganancia=?"); vals.append(ganancia)
    if not sets:
        return 0
    vals += [accion, fecha, hora, estrategia, tipo]
    with _conn() as con:
        cur = con.execute(
            f"UPDATE alerts SET {', '.join(sets)} "
            "WHERE accion=? AND fecha=? AND hora=? AND estrategia=? AND tipo=?",
            vals,
        )
        return cur.rowcount

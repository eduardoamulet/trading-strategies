"""Almacén SQLite de señales/alertas (Historial de Señales de investepacademyia).

Por qué SQLite: ingreso INCREMENTAL, DEDUP por `id` (UUID estable de cada señal),
UPDATE de estado/ganancia por fila, y queries — todo en un archivo local sin
servidor. Consistente con live_trader.

DB en data/signals.db (gitignored). Dedup en DOS niveles para que reimportar (o
mezclar fuentes) NO duplique ni pise tus ediciones locales (estado/ganancia/notas):
  1) PRIMARY KEY `id` (token del gráfico) — INSERT OR IGNORE.
  2) CONTENIDO (symbol·tipo·estrategia_raw·fecha·hora) — si ya existe una señal con
     el mismo contenido, se omite aunque traiga otro `id` (p.ej. la URL de la imagen
     del email cambia entre envíos y generaría un id distinto).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

HERE = Path(__file__).parent
DB_PATH = HERE / "data" / "signals.db"   # gitignored (*.db)

# Columnas expuestas (modelo de la API + metadata de ingesta).
COLUMNS = [
    "id", "symbol", "tipo", "estrategia", "estrategia_raw", "probabilidad",
    "fecha", "hora", "estado", "ganancia", "is_active", "criterios", "criterios_json",
    "chart_url", "creado_en", "fuente", "importado_en",
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
                id             TEXT PRIMARY KEY,
                symbol         TEXT NOT NULL,
                tipo           TEXT,
                estrategia     TEXT,
                estrategia_raw TEXT,
                probabilidad   REAL,
                fecha          TEXT,
                hora           TEXT,
                estado         TEXT,
                ganancia       REAL DEFAULT 0,
                is_active      INTEGER,
                criterios      TEXT,
                criterios_json TEXT,
                chart_url      TEXT,
                creado_en      TEXT,
                fuente         TEXT,
                importado_en   TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS ix_alerts_fecha ON alerts(fecha)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_alerts_content "
                    "ON alerts(symbol, tipo, estrategia_raw, fecha, hora)")
        try:  # migración para DBs viejas
            con.execute("ALTER TABLE alerts ADD COLUMN criterios_json TEXT")
        except sqlite3.OperationalError:
            pass


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
    """Inserta SOLO las señales nuevas. Devuelve cuántas son NUEVAS. Una señal se
    considera repetida (y se OMITE) si:
      - ya existe su `id` (PRIMARY KEY), o
      - ya existe otra con el MISMO contenido (symbol·tipo·estrategia_raw·fecha·hora),
        aunque tenga un `id` distinto.
    Las existentes no se pisan (preserva tus ediciones de estado/ganancia)."""
    if df is None or df.empty:
        return 0
    init_db()
    inserted = 0
    with _conn() as con:
        for _, r in df.iterrows():
            # Dedup por CONTENIDO: misma señal con otro id (p.ej. la imagen del email
            # cambió de URL) → no la duplicamos. `IS` compara NULL de forma segura.
            dup = con.execute(
                "SELECT 1 FROM alerts WHERE symbol IS ? AND tipo IS ? AND "
                "estrategia_raw IS ? AND fecha IS ? AND hora IS ? LIMIT 1",
                (_na(r.get("symbol")), _na(r.get("tipo")), _na(r.get("estrategia_raw")),
                 _na(r.get("fecha")), _na(r.get("hora"))),
            ).fetchone()
            if dup is not None:
                continue
            vals = [_na(r.get(c)) for c in COLUMNS]
            if vals[COLUMNS.index("ganancia")] is None:
                vals[COLUMNS.index("ganancia")] = 0.0
            cur = con.execute(
                f"INSERT OR IGNORE INTO alerts ({','.join(COLUMNS)}) "
                f"VALUES ({','.join('?' * len(COLUMNS))})",
                vals,
            )
            inserted += cur.rowcount
    return inserted


def load_signals() -> pd.DataFrame:
    """Todas las señales guardadas (más recientes primero)."""
    init_db()
    with _conn() as con:
        df = pd.read_sql_query(
            f"SELECT {','.join(COLUMNS)} FROM alerts "
            "ORDER BY fecha DESC, hora DESC, creado_en DESC",
            con,
        )
    return df


def count() -> int:
    init_db()
    with _conn() as con:
        return int(con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])


def update_user_fields(signal_id: str, estado: Optional[str] = None,
                       ganancia: Optional[float] = None) -> int:
    """Actualiza estado/ganancia de una señal puntual (por id). Filas afectadas."""
    init_db()
    sets, vals = [], []
    if estado is not None:
        sets.append("estado=?"); vals.append(estado)
    if ganancia is not None:
        sets.append("ganancia=?"); vals.append(ganancia)
    if not sets:
        return 0
    vals.append(signal_id)
    with _conn() as con:
        cur = con.execute(f"UPDATE alerts SET {', '.join(sets)} WHERE id=?", vals)
        return cur.rowcount

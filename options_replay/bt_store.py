"""Almacén ACUMULADO de resultados de backtesting — la base de la actualización incremental.

Principio: cada fila de un results DETALLADO es un HECHO INMUTABLE — el resultado de
(combinación, escenario, fecha, ticker) no depende de ningún otro día. Por eso el almacén es
APPEND-ONLY con clave primaria (combination, fecha, ticker, id): ingestar el mismo día dos veces
(o files con rangos solapados) deduplica solo, y el playbook se recalcula agregando sobre el
almacén (milisegundos) sin re-backtestear nada.

`combination` SEGREGA los universos de escenarios (Combinaciones de Backtesting): el C001 de una
combinación no es el C001 de otra — sin esta clave, el veredicto mezclaría condiciones distintas
bajo el mismo ID. Las filas históricas (previas a las combinaciones) pertenecen al template
estático de 480 → combinación «tpl480» (la migración es automática y única).

SQLite en data/bt_results.db (mismo patrón que ticker_prefs.db / signals.db / trades.db).
Columnas = las CANÓNICAS del loader de bt_analysis (core A..N + enriquecidas), así el mismo
DataFrame sirve para det.prepare()/las agregaciones existentes sin mapear nada.

`engine_version` viaja en cada fila: si el motor de backtest cambia, la revalidación de
integridad (recomputar una muestra vieja y comparar) detecta el drift antes de mezclar peras
con manzanas.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from bt_analysis import loader as _ldr

HERE = Path(__file__).parent
DB_PATH = HERE / "data" / "bt_results.db"

# Bump manual cuando cambie la LÓGICA del motor de backtest (ucbatch/engine): las filas nuevas
# quedan marcadas y la mezcla de versiones es detectable.
ENGINE_VERSION = "2026-07-03"

# Combinación de las filas históricas (pre-Combinaciones): el template estático de 480.
LEGACY_COMBINATION = "tpl480"

_CORE = list(_ldr._RESULT_COLS)                        # id, ticker, fecha, inv, ganancia, roi_*…
_ENRICH = sorted(set(_ldr._ENRICH_HEADER_MAP.values()))
_META = ["engine_version", "source_file", "insertado_en"]
COLUMNS = _CORE + _ENRICH + _META + ["combination"]


def _create(con: sqlite3.Connection) -> None:
    cols = ",\n        ".join(f'"{c}"' for c in COLUMNS)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS bt_results (
        {cols},
        PRIMARY KEY (combination, fecha, ticker, id)
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_bt_fecha ON bt_results (fecha)")


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name='bt_results'").fetchone()
    if exists:
        cols = [r[1] for r in con.execute("PRAGMA table_info(bt_results)")]
        if "combination" not in cols:
            # MIGRACIÓN ÚNICA: PK (fecha,ticker,id) → (combination,fecha,ticker,id).
            # Todo lo previo proviene del template 480 → combination = LEGACY_COMBINATION.
            old_cols = ", ".join(f'"{c}"' for c in cols)
            con.execute("ALTER TABLE bt_results RENAME TO bt_results_old")
            _create(con)
            con.execute(f"INSERT INTO bt_results ({old_cols}, combination) "
                        f"SELECT {old_cols}, '{LEGACY_COMBINATION}' FROM bt_results_old")
            con.execute("DROP TABLE bt_results_old")
            con.commit()
    else:
        _create(con)
    return con


def ingest_df(df: pd.DataFrame, *, source_file: str = "",
              engine_version: str = ENGINE_VERSION,
              combination: str = LEGACY_COMBINATION, path: Path = DB_PATH) -> int:
    """Agrega filas canónicas (formato del loader, granularidad DETALLADA) al almacén, bajo la
    `combination` dada. INSERT OR IGNORE → los duplicados por (combination, fecha, ticker, id)
    se saltan. Devuelve cuántas entraron."""
    if df is None or df.empty:
        return 0
    d = df.copy()
    for c in COLUMNS:
        if c not in d.columns:
            d[c] = None
    d["fecha"] = d["fecha"].astype(str).str[:10]
    d["engine_version"] = engine_version
    d["source_file"] = str(source_file)
    d["insertado_en"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    d["combination"] = str(combination)
    d = d[COLUMNS]
    # NaN → None para que SQLite guarde NULL (no el string 'nan').
    d = d.astype(object).where(pd.notna(d), None)
    with _connect(path) as con:
        before = con.execute("SELECT COUNT(*) FROM bt_results").fetchone()[0]
        con.executemany(
            f"INSERT OR IGNORE INTO bt_results VALUES ({','.join('?' * len(COLUMNS))})",
            d.itertuples(index=False, name=None))
        return con.execute("SELECT COUNT(*) FROM bt_results").fetchone()[0] - before


def ingest_results_file(xlsx_path, *, engine_version: str = ENGINE_VERSION,
                        combination: str = LEGACY_COMBINATION, path: Path = DB_PATH) -> int:
    """Ingesta un results file DETALLADO (el xlsx que produce el batch) bajo la `combination`.
    Rechaza los agregados (no aportan filas por día). Devuelve filas nuevas."""
    rdf, gran = _ldr.load_results(str(xlsx_path))
    if gran != _ldr.DETAILED:
        raise ValueError(f"{Path(str(xlsx_path)).name}: granularidad «{gran}» — el almacén solo "
                         "acepta results DETALLADOS (1 fila por ticker×día×escenario).")
    return ingest_df(rdf, source_file=Path(str(xlsx_path)).name,
                     engine_version=engine_version, combination=combination, path=path)


def last_date(combination: str = LEGACY_COMBINATION, path: Path = DB_PATH) -> str | None:
    with _connect(path) as con:
        v = con.execute("SELECT MAX(fecha) FROM bt_results WHERE combination=?",
                        (combination,)).fetchone()[0]
    return v


def coverage(combination: str = LEGACY_COMBINATION, path: Path = DB_PATH) -> dict:
    """Resumen del almacén PARA la combinación: filas, rango, días, tickers, versiones."""
    with _connect(path) as con:
        n, d0, d1, nd = con.execute(
            "SELECT COUNT(*), MIN(fecha), MAX(fecha), COUNT(DISTINCT fecha) FROM bt_results "
            "WHERE combination=?", (combination,)).fetchone()
        tks = [r[0] for r in con.execute(
            "SELECT DISTINCT ticker FROM bt_results WHERE combination=? ORDER BY ticker",
            (combination,))]
        vers = [r[0] for r in con.execute(
            "SELECT DISTINCT engine_version FROM bt_results WHERE combination=?",
            (combination,))]
    return {"filas": n, "desde": d0, "hasta": d1, "dias": nd, "tickers": tks,
            "engine_versions": vers, "combination": combination}


def load_range(desde: str | None = None, hasta: str | None = None,
               combination: str = LEGACY_COMBINATION, path: Path = DB_PATH) -> pd.DataFrame:
    """Filas canónicas de la combinación en [desde, hasta] (inclusive; None = sin tope). El
    DataFrame tiene las columnas de loader.load_results → sirve directo para det.prepare()."""
    q, args = "SELECT * FROM bt_results WHERE combination=?", [str(combination)]
    if desde:
        q += " AND fecha >= ?"
        args.append(str(desde)[:10])
    if hasta:
        q += " AND fecha <= ?"
        args.append(str(hasta)[:10])
    with _connect(path) as con:
        df = pd.read_sql_query(q, con, params=args)
    return df


def distinct_dates(combination: str = LEGACY_COMBINATION, path: Path = DB_PATH) -> list[str]:
    with _connect(path) as con:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT fecha FROM bt_results WHERE combination=? ORDER BY fecha",
            (combination,))]


def combinations_summary(path: Path = DB_PATH) -> list[dict]:
    """Qué combinaciones tienen historia en el almacén (para la página Playbook)."""
    with _connect(path) as con:
        rows = con.execute(
            "SELECT combination, COUNT(*), MIN(fecha), MAX(fecha), COUNT(DISTINCT fecha) "
            "FROM bt_results GROUP BY combination ORDER BY combination").fetchall()
    return [{"combination": r[0], "filas": r[1], "desde": r[2], "hasta": r[3], "dias": r[4]}
            for r in rows]

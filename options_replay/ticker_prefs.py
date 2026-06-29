"""Preferencias de tickers + calendario de vencimiento más temprano — persistente en SQLite.

Tabla `ticker_prefs`:
  ticker (PK) · orden · preferencia (0/1) · exp_lun..exp_vie (texto display) · updated_at

- `orden`: los 11 prioritarios primero (1..11), luego el resto. Fija el orden de la tabla.
- `preferencia`: checkbox editable por el usuario (arranca en 1 para los 11 prioritarios).
- `exp_*`: vencimiento más temprano entrando ese día de la semana ("mismo día" / "Vie (+3)" / "—").
  Se calcula con `dl.nearest_expiry` sobre una semana representativa y se cachea acá.

La BD vive en `data/ticker_prefs.db` (gitignored). `seed()` usa INSERT OR IGNORE → NO pisa
ediciones del usuario; solo agrega tickers nuevos.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "ticker_prefs.db"

# 11 prioritarios (van primero + checkbox marcado por defecto), en este orden:
PRIORITY = ["QQQ", "SPY", "IWM", "NVDA", "TSLA", "PLTR", "AMZN", "META", "MSFT", "GOOG", "AAPL"]
# El resto (en el orden pedido por el usuario):
REST = ["AAL", "AMD", "AVGO", "AXP", "BA", "BABA", "C", "CCL", "COIN", "CVS", "DAL", "DASH",
        "DIA", "GLD", "HD", "HOOD", "LI", "LOW", "LYFT", "MA", "MRNA", "MU", "NFLX", "NIO",
        "OKLO", "ORCL", "PFE", "PYPL", "QCOM", "RCL", "SLV", "SOXL", "SPX", "TNA", "UBER",
        "URA", "USO", "V", "WMT", "XPEV"]
ALL_TICKERS = PRIORITY + REST

# Semana representativa (Lun-Vie, sin feriados) para inferir el calendario de vencimientos.
_WEEK = [("exp_lun", "2026-05-18"), ("exp_mar", "2026-05-19"), ("exp_mie", "2026-05-20"),
         ("exp_jue", "2026-05-21"), ("exp_vie", "2026-05-22")]
_DOW = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")   # lectores + 1 escritor concurrentes
    except Exception:
        pass
    return con


def init_db() -> None:
    with closing(_conn()) as con, con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ticker_prefs (
                ticker      TEXT PRIMARY KEY,
                orden       INTEGER,
                preferencia INTEGER DEFAULT 0,
                exp_lun     TEXT,
                exp_mar     TEXT,
                exp_mie     TEXT,
                exp_jue     TEXT,
                exp_vie     TEXT,
                updated_at  TEXT
            )""")


def seed() -> None:
    """Inserta los tickers que falten (INSERT OR IGNORE → no pisa ediciones del usuario)."""
    init_db()
    now = _dt.datetime.now().isoformat(timespec="seconds")
    rows = [(tk, i, 1 if tk in PRIORITY else 0, now)
            for i, tk in enumerate(ALL_TICKERS, start=1)]
    with closing(_conn()) as con, con:
        con.executemany(
            "INSERT OR IGNORE INTO ticker_prefs (ticker, orden, preferencia, updated_at) "
            "VALUES (?,?,?,?)", rows)


def load() -> pd.DataFrame:
    """DataFrame ordenado por `orden`. Columnas exp_* vacías → '—'."""
    with closing(_conn()) as con, con:
        df = pd.read_sql_query("SELECT * FROM ticker_prefs ORDER BY orden", con)
    if df.empty:
        return df
    df["preferencia"] = df["preferencia"].fillna(0).astype(int).astype(bool)
    for c in ("exp_lun", "exp_mar", "exp_mie", "exp_jue", "exp_vie"):
        df[c] = df[c].fillna("—").replace("", "—")
    df["es_top"] = df["orden"] <= len(PRIORITY)
    return df


def save_preferencias(mapping: dict) -> int:
    """mapping {ticker: bool}. Actualiza la columna preferencia. Devuelve filas tocadas."""
    now = _dt.datetime.now().isoformat(timespec="seconds")
    rows = [(1 if bool(v) else 0, now, str(tk).strip().upper()) for tk, v in mapping.items()]
    with closing(_conn()) as con, con:
        con.executemany(
            "UPDATE ticker_prefs SET preferencia=?, updated_at=? WHERE ticker=?", rows)
    return len(rows)


def preferred_tickers() -> list:
    with closing(_conn()) as con, con:
        return [r["ticker"] for r in con.execute(
            "SELECT ticker FROM ticker_prefs WHERE preferencia=1 ORDER BY orden")]


def _cell(dl, ticker: str, date_str: str) -> str:
    """Vencimiento más temprano entrando en `date_str`, como texto display."""
    try:
        ne = dl.nearest_expiry(ticker, date_str)
    except Exception:
        ne = None
    if not ne:
        return "—"
    try:
        d0 = _dt.date.fromisoformat(date_str)
        d1 = _dt.date.fromisoformat(str(ne)[:10])
        dd = (d1 - d0).days
        return "mismo día" if dd == 0 else f"{_DOW[d1.weekday()]} (+{dd})"
    except Exception:
        return str(ne)


def compute_expiries(dl, tickers=None, week=None) -> dict:
    """Calcula el calendario de vencimientos (best-effort) y lo guarda en la BD.
    Devuelve {ok: n_con_datos, total: n}. Los que fallan quedan en '—'."""
    init_db()
    tickers = tickers or ALL_TICKERS
    week = week or _WEEK
    now = _dt.datetime.now().isoformat(timespec="seconds")
    ok = 0
    con = _conn()
    try:
        for tk in tickers:
            cells = {col: _cell(dl, tk, date) for col, date in week}   # API/cache: SIN lock
            if any(v != "—" for v in cells.values()):
                ok += 1
            con.execute(
                "UPDATE ticker_prefs SET exp_lun=?, exp_mar=?, exp_mie=?, exp_jue=?, exp_vie=?, "
                "updated_at=? WHERE ticker=?",
                (cells["exp_lun"], cells["exp_mar"], cells["exp_mie"], cells["exp_jue"],
                 cells["exp_vie"], now, tk))
            con.commit()   # commit POR ticker → no retiene el lock durante toda la corrida
    finally:
        con.close()
    return {"ok": ok, "total": len(tickers)}

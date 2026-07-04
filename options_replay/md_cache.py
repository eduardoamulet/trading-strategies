"""Cache persistente de señales del Market Direction Engine (data/md_cache.db).

La señal histórica es DETERMINISTA por (ticker, fecha, hora) para una versión dada del motor
(MD_ENGINE_VERSION): se computa una vez (~2 s) y después es una lectura de milisegundos. Lo usa
el batch (ucbatch/runner._direction_fields), que antes recomputaba ~6 s por día (3 tickers) en
CADA corrida — incremental diario, verify y sweeps incluidos.

Multiproceso-seguro: WAL + busy_timeout + INSERT OR REPLACE (la señal es determinista, así que
una carrera entre workers escribe el mismo valor). Al subir MD_ENGINE_VERSION las filas viejas
quedan huérfanas y se purgan lazy en el primer miss.

La huella del "sin datos" (score 50 · confianza 0 · NEUTRAL) NO se cachea: puede deberse a un
cache de subyacente incompleto que mañana sí existe — congelarla sería un falso permanente.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "data" / "md_cache.db"

_FIELDS = ("md_action", "md_score", "md_confidence", "md_trend")


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS md_signal (
        ver    TEXT NOT NULL,
        ticker TEXT NOT NULL,
        fecha  TEXT NOT NULL,
        hora   TEXT NOT NULL,
        md_action TEXT, md_score REAL, md_confidence REAL, md_trend TEXT,
        PRIMARY KEY (ver, ticker, fecha, hora))""")
    return c


def get(ticker: str, fecha: str, hora: str, db_path: Optional[Path] = None) -> Optional[dict]:
    import market_direction.engine as _md
    with _connect(db_path) as c:
        row = c.execute(
            "SELECT md_action, md_score, md_confidence, md_trend FROM md_signal "
            "WHERE ver=? AND ticker=? AND fecha=? AND hora=?",
            (_md.MD_ENGINE_VERSION, str(ticker).upper(), str(fecha), str(hora))).fetchone()
    return dict(zip(_FIELDS, row)) if row else None


def put(ticker: str, fecha: str, hora: str, fields: dict,
        db_path: Optional[Path] = None) -> None:
    import market_direction.engine as _md
    with _connect(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO md_signal (ver, ticker, fecha, hora, "
            "md_action, md_score, md_confidence, md_trend) VALUES (?,?,?,?,?,?,?,?)",
            (_md.MD_ENGINE_VERSION, str(ticker).upper(), str(fecha), str(hora),
             *(fields.get(k) for k in _FIELDS)))


def _es_huella_sin_datos(fields: dict) -> bool:
    """La firma exacta de TradeSignal.no_trade(50, 0, NEUTRAL) del camino «sin datos»."""
    return (fields.get("md_confidence") == 0.0 and fields.get("md_score") == 50.0
            and str(fields.get("md_trend") or "").upper() in ("NEUTRAL", "TREND.NEUTRAL"))


def get_or_compute(ticker: str, fecha: str, hora: str, provider=None,
                   db_path: Optional[Path] = None) -> dict:
    """Lee del cache; en miss computa con el motor real, guarda y devuelve. Mismos redondeos
    que producía el batch (score .1 / confianza .3) → las filas del results no cambian."""
    hit = get(ticker, fecha, hora, db_path=db_path)
    if hit is not None:
        return hit
    import market_direction.engine as _md
    sig = _md.market_direction_engine(str(ticker).upper(), str(fecha), str(hora),
                                      provider=provider)
    fields = {"md_action": sig.action.value, "md_score": round(float(sig.score), 1),
              "md_confidence": round(float(sig.confidence), 3), "md_trend": sig.trend.value}
    if not _es_huella_sin_datos(fields):
        with _connect(db_path) as c:                     # purga lazy de versiones viejas
            c.execute("DELETE FROM md_signal WHERE ver != ?", (_md.MD_ENGINE_VERSION,))
        put(ticker, fecha, hora, fields, db_path=db_path)
    return fields

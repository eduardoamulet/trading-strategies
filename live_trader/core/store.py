"""Store SQLite (WAL) — única fuente de verdad compartida entre daemon y UI.

Tres tablas:
- positions: posiciones abiertas/cerradas + estado live (ROI/PnL)
- audit_log: append-only de cada acción (inmutable)
- commands: comandos de la UI al daemon (arm/disarm TP, kill switch, manual sell)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Position


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    occ TEXT PRIMARY KEY, underlying TEXT, qty INTEGER,
                    entry_price REAL, entry_time TEXT, cost_total REAL,
                    order_id TEXT, roi_target_pct REAL, side TEXT,
                    tp_armed INTEGER DEFAULT 0,
                    current_price REAL DEFAULT 0, roi_pct REAL DEFAULT 0, pnl REAL DEFAULT 0,
                    status TEXT DEFAULT 'open',
                    exit_price REAL, exit_time TEXT, roi_final REAL, pnl_net REAL
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, event TEXT, payload TEXT
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, kind TEXT, occ TEXT, payload TEXT, consumed INTEGER DEFAULT 0
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT, ts TEXT
                )""")

    # ---------- audit (append-only) ----------
    def audit(self, event: str, payload: dict):
        with self._conn() as c:
            c.execute("INSERT INTO audit_log (ts, event, payload) VALUES (?,?,?)",
                      (datetime.utcnow().isoformat(), event, json.dumps(payload, default=str)))

    # ---------- positions ----------
    def save_position(self, p: Position):
        with self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO positions
                (occ, underlying, qty, entry_price, entry_time, cost_total, order_id,
                 roi_target_pct, side, tp_armed, current_price, roi_pct, pnl, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p.occ, p.underlying, p.qty, p.entry_price, p.entry_time.isoformat(),
                 p.cost_total, p.order_id, p.roi_target_pct, p.side, int(p.tp_armed),
                 p.current_price, p.roi_pct, p.pnl, p.status))
        self.audit("position_open", {"occ": p.occ, "qty": p.qty, "entry": p.entry_price})

    def update_live(self, occ: str, current_price: float, roi_pct: float, pnl: float):
        with self._conn() as c:
            c.execute("UPDATE positions SET current_price=?, roi_pct=?, pnl=? WHERE occ=?",
                      (current_price, roi_pct, pnl, occ))

    def set_tp_armed(self, occ: str, armed: bool):
        with self._conn() as c:
            c.execute("UPDATE positions SET tp_armed=? WHERE occ=?", (int(armed), occ))
        self.audit("tp_armed" if armed else "tp_disarmed", {"occ": occ})

    def mark_closing(self, occ: str):
        with self._conn() as c:
            c.execute("UPDATE positions SET status='closing' WHERE occ=? AND status='open'", (occ,))

    def close_position(self, occ: str, exit_price: float, exit_time: datetime,
                       roi_final: float, pnl_net: float):
        with self._conn() as c:
            c.execute("""UPDATE positions SET status='closed', tp_armed=0,
                         exit_price=?, exit_time=?, roi_final=?, pnl_net=? WHERE occ=?""",
                      (exit_price, exit_time.isoformat(), roi_final, pnl_net, occ))
        self.audit("position_close", {"occ": occ, "exit": exit_price, "roi": roi_final, "pnl": pnl_net})

    def open_positions(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM positions WHERE status IN ('open','closing')").fetchall()]

    def all_positions(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM positions ORDER BY entry_time DESC").fetchall()]

    def get_position(self, occ: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM positions WHERE occ=?", (occ,)).fetchone()
            return dict(r) if r else None

    # ---------- commands (UI → daemon) ----------
    def push_command(self, kind: str, occ: str = "", payload: dict | None = None):
        with self._conn() as c:
            c.execute("INSERT INTO commands (ts, kind, occ, payload) VALUES (?,?,?,?)",
                      (datetime.utcnow().isoformat(), kind, occ, json.dumps(payload or {}, default=str)))

    def pop_commands(self) -> list[dict]:
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM commands WHERE consumed=0 ORDER BY id").fetchall()]
            if rows:
                c.execute("UPDATE commands SET consumed=1 WHERE consumed=0")
            return rows

    def recent_audit(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    # ---------- meta (kv: latido del daemon, etc.) ----------
    def set_meta(self, key: str, value: str = ""):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value, ts) VALUES (?,?,?)",
                      (key, value, datetime.utcnow().isoformat()))

    def get_meta(self, key: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM meta WHERE key=?", (key,)).fetchone()
            return dict(r) if r else None

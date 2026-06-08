"""Almacén SQLite de usuarios (sección administrativa).

Seguridad: las contraseñas se guardan HASHEADAS con PBKDF2-HMAC-SHA256 + salt por
usuario (nunca en texto plano). La DB (data/users.db) está gitignored. La
verificación usa comparación de tiempo constante.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

HERE = Path(__file__).parent
DB_PATH = HERE / "data" / "users.db"   # gitignored (*.db)
ROLES = ["admin", "usuario"]
_ITERATIONS = 200_000


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre        TEXT,
                email         TEXT UNIQUE NOT NULL,
                rol           TEXT DEFAULT 'usuario',
                activo        INTEGER DEFAULT 1,
                salt          TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                creado_en     TEXT
            )
            """
        )
        con.execute("CREATE TABLE IF NOT EXISTS settings (clave TEXT PRIMARY KEY, valor TEXT)")


def get_setting(clave: str, default: str = "") -> str:
    init_db()
    with _conn() as con:
        row = con.execute("SELECT valor FROM settings WHERE clave=?", (clave,)).fetchone()
    return row["valor"] if row else default


def set_setting(clave: str, valor: str) -> None:
    init_db()
    with _conn() as con:
        con.execute(
            "INSERT INTO settings (clave,valor) VALUES (?,?) "
            "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
            (clave, "" if valor is None else str(valor)),
        )


def get_profile() -> dict:
    """Datos del perfil del dueño de la app (nombre/apellido/país + notificaciones)."""
    return {
        "nombre": get_setting("perfil_nombre"),
        "apellido": get_setting("perfil_apellido"),
        "pais": get_setting("perfil_pais", "Estados Unidos"),
        "notif_email": get_setting("perfil_notif_email", "1") == "1",
    }


def save_profile(nombre: str, apellido: str, pais: str, notif_email: bool = True) -> None:
    set_setting("perfil_nombre", nombre)
    set_setting("perfil_apellido", apellido)
    set_setting("perfil_pais", pais)
    set_setting("perfil_notif_email", "1" if notif_email else "0")


def _hash(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return salt.hex(), h.hex()


def add_user(nombre: str, email: str, password: str, rol: str = "usuario",
             activo: bool = True, creado_en: Optional[str] = None) -> int:
    """Crea un usuario (contraseña hasheada). Lanza ValueError si el email existe o
    faltan datos. Devuelve el id."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Email inválido.")
    if not password or len(password) < 6:
        raise ValueError("La contraseña debe tener al menos 6 caracteres.")
    if rol not in ROLES:
        rol = "usuario"
    salt, phash = _hash(password)
    init_db()
    try:
        with _conn() as con:
            cur = con.execute(
                "INSERT INTO users (nombre,email,rol,activo,salt,password_hash,creado_en) "
                "VALUES (?,?,?,?,?,?,?)",
                ((nombre or "").strip(), email, rol, 1 if activo else 0, salt, phash, creado_en),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        raise ValueError(f"Ya existe un usuario con el email {email}.")


def list_users() -> pd.DataFrame:
    """Usuarios SIN datos sensibles (sin salt/hash)."""
    init_db()
    with _conn() as con:
        return pd.read_sql_query(
            "SELECT id,nombre,email,rol,activo,creado_en FROM users ORDER BY id", con)


def count() -> int:
    init_db()
    with _conn() as con:
        return int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def set_role(user_id: int, rol: str) -> int:
    if rol not in ROLES:
        raise ValueError("Rol inválido.")
    with _conn() as con:
        return con.execute("UPDATE users SET rol=? WHERE id=?", (rol, user_id)).rowcount


def set_active(user_id: int, activo: bool) -> int:
    with _conn() as con:
        return con.execute("UPDATE users SET activo=? WHERE id=?",
                           (1 if activo else 0, user_id)).rowcount


def set_password(user_id: int, password: str) -> int:
    if not password or len(password) < 6:
        raise ValueError("La contraseña debe tener al menos 6 caracteres.")
    salt, phash = _hash(password)
    with _conn() as con:
        return con.execute("UPDATE users SET salt=?, password_hash=? WHERE id=?",
                           (salt, phash, user_id)).rowcount


def delete_user(user_id: int) -> int:
    with _conn() as con:
        return con.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount


def verify(email: str, password: str) -> Optional[dict]:
    """Valida credenciales (para un futuro login). Devuelve el usuario (sin hash) si
    coinciden y está activo; None si no."""
    email = (email or "").strip().lower()
    init_db()
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not row["activo"]:
        return None
    _, calc = _hash(password, bytes.fromhex(row["salt"]))
    if hmac.compare_digest(calc, row["password_hash"]):
        return {"id": row["id"], "nombre": row["nombre"], "email": row["email"],
                "rol": row["rol"]}
    return None

"""Importador de señales (investepacademyia.com — Historial de Señales).

Parsea el payload JSON del Historial de Señales (el mismo que devuelve la API / que
puede venir en el email) y lo guarda en SQLite (dedup por id). Dos vías:
  - PEGAR EL JSON (manual) → funciona ya.
  - INGESTA AUTOMÁTICA por email (IMAP) → pendiente de app password + remitente.

Seguridad: sin credenciales hardcodeadas. Cualquier secreto (app password) irá en
un archivo gitignored que el usuario completa.
"""
from __future__ import annotations

import json as _json
from datetime import datetime
from typing import Union

import pandas as pd

import signals_db as db

_ET_TZ = "America/New_York"   # el sitio muestra horarios en hora del Este

COLUMNS = db.COLUMNS

# strategyName del payload → etiqueta amigable (como en el sitio).
STRAT_LABEL = {
    "trend-reversal": "Cambio de Tendencia en Hora",
    "trend-reversal-15m": "Cambio de Tendencia en 15m",
}


class ScraperNotConfigured(RuntimeError):
    """La ingesta automática (email/API) aún no está conectada."""


def _to_et(iso_utc) -> tuple:
    """ISO UTC ('2026-06-08T13:24:47Z') → (fecha ISO, 'HH:MM') en hora del Este.
    Usa pandas para la conversión tz (robusto en Windows sin tzdata de stdlib)."""
    if not iso_utc:
        return (None, None)
    try:
        ts = pd.Timestamp(iso_utc)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert(_ET_TZ)
        return (ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M"))
    except Exception:
        return (None, None)


def parse_api_payload(payload: Union[str, dict, list]) -> pd.DataFrame:
    """Convierte el Historial de Señales (dict {items:[...]}, lista de items, o texto
    JSON) en un DataFrame con db.COLUMNS."""
    if isinstance(payload, str):
        payload = _json.loads(payload)
    if isinstance(payload, dict):
        items = payload.get("items", [])
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        cd = it.get("criteriaData") or {}
        n_ok = sum(1 for i in (1, 2, 3, 4) if cd.get(f"criterio{i}"))
        fecha, hora = _to_et(it.get("createdAt") or it.get("lastNotificationAt"))
        prob = it.get("probability")
        gain = it.get("userGain")
        used = it.get("userUsedSignal")
        rows.append({
            "id": it.get("id"),
            "symbol": it.get("symbol"),
            "tipo": it.get("signalType"),
            "estrategia": STRAT_LABEL.get(it.get("strategyName"), it.get("strategyName")),
            "estrategia_raw": it.get("strategyName"),
            "probabilidad": float(prob) if prob not in (None, "") else None,
            "fecha": fecha,
            "hora": hora,
            "estado": "Por definir" if used in (None, "") else str(used),
            "ganancia": float(gain) if gain not in (None, "") else 0.0,
            "is_active": 1 if it.get("isActive") else 0,
            "criterios": f"{n_ok}/4",
            "chart_url": it.get("chartUrl"),
            "creado_en": it.get("createdAt"),
            "fuente": "api",
            "importado_en": None,
        })
    return pd.DataFrame(rows, columns=COLUMNS) if rows else pd.DataFrame(columns=COLUMNS)


def import_payload(payload, now_iso: str = None) -> int:
    """Parsea y hace upsert (dedup por id). Devuelve cuántas señales NUEVAS entraron."""
    df = parse_api_payload(payload)
    if not df.empty and now_iso is not None:
        df["importado_en"] = now_iso
    return db.upsert_signals(df)


def load_signals() -> pd.DataFrame:
    return db.load_signals()


# ── Ingesta automática por EMAIL (IMAP) — pendiente ──────────────────────────
IMAP_HOST = "imap.gmail.com"


def fetch_from_email(max_emails: int = 50) -> int:
    """A futuro: lee el Gmail vía IMAP, parsea los correos de alerta y hace upsert.
    Pendiente: remitente/asunto + Gmail App Password (gitignored)."""
    raise ScraperNotConfigured(
        "La ingesta automática por email todavía no está conectada. Por ahora pegá el "
        "JSON del Historial de Señales (abajo) y se importa al instante. Para "
        "automatizarlo necesito el remitente/asunto del correo + un Gmail App Password "
        "en un archivo gitignored (lo generás vos)."
    )

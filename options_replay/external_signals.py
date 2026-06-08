"""Importador de señales externas (investepacademyia.com/app/alertas).

Arquitectura:
- Las señales importadas se guardan en un almacén local Parquet (gitignored) para
  que persistan entre sesiones y queden disponibles para el backtester.
- `fetch_and_store()` es el "scraper": se autentica al sitio, baja el Historial de
  Señales, lo parsea y lo guarda. La parte de AUTENTICACIÓN + ENDPOINT está marcada
  como pendiente: se completa una vez que se inspecciona cómo /app/alertas sirve y
  protege sus datos (se reproduce esa request). Hasta entonces lanza
  ScraperNotConfigured con instrucciones.

Seguridad: este módulo NUNCA debe contener credenciales hardcodeadas. El secreto de
acceso (token/cookie/credenciales) vive en un archivo gitignored que el usuario
completa; acá solo se lee.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

import signals_db as db

HERE = Path(__file__).parent
BASE_URL = "https://investepacademyia.com"
ALERTAS_URL = f"{BASE_URL}/app/alertas"

# Esquema de una señal (espejo de la tabla "Historial de Señales" del sitio).
COLUMNS = [
    "accion",        # ticker (USO, BA, ...)
    "hora",          # "09:24"
    "fecha",         # ISO "2026-06-05"
    "estrategia",    # "Cambio de Tendencia en 15m"
    "cumplimiento",  # % (90.0)
    "tipo",          # "CALL" | "PUT"
    "estado",        # "Por definir" | ...
    "ganancia",      # USD (0.0)
    "fuente",        # "investepacademyia" | "demo"
    "importado_en",  # timestamp de la importación (se sella afuera)
]


class ScraperNotConfigured(RuntimeError):
    """El fetch en vivo aún no está conectado (falta endpoint/auth del sitio)."""


def _demo_signals() -> pd.DataFrame:
    """Las 10 señales de ejemplo (página 1 de las capturas) — placeholder hasta
    que el botón de importación en vivo esté conectado. fuente='demo'."""
    rows = [
        ("USO",  "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 90.0, "PUT",  "Por definir", 0.0),
        ("BA",   "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 90.0, "PUT",  "Por definir", 0.0),
        ("HOOD", "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 85.0, "PUT",  "Por definir", 0.0),
        ("V",    "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 90.0, "CALL", "Por definir", 0.0),
        ("WMT",  "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 85.0, "CALL", "Por definir", 0.0),
        ("AVGO", "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 90.0, "PUT",  "Por definir", 0.0),
        ("MSFT", "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 85.0, "CALL", "Por definir", 0.0),
        ("TSLA", "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 85.0, "CALL", "Por definir", 0.0),
        ("NFLX", "09:24", "2026-06-05", "Cambio de Tendencia en 15m", 85.0, "CALL", "Por definir", 0.0),
        ("V",    "09:54", "2026-06-04", "Cambio de Tendencia en Hora", 90.0, "CALL", "Por definir", 0.0),
    ]
    df = pd.DataFrame(rows, columns=COLUMNS[:8])
    df["fuente"] = "demo"
    df["importado_en"] = pd.NaT
    return df[COLUMNS]


def load_signals() -> pd.DataFrame:
    """Señales guardadas en SQLite. Si la DB está vacía, devuelve las demo (sin
    guardarlas) para que la página no se vea vacía hasta la 1ª importación real."""
    if db.count() > 0:
        return db.load_signals()
    return _demo_signals()


def store_signals(df: pd.DataFrame) -> int:
    """Upsert (dedup) de señales en SQLite. Devuelve cuántas son nuevas."""
    return db.upsert_signals(df)


def parse_signals(payload, fmt: str = "auto") -> pd.DataFrame:
    """Convierte la respuesta cruda del sitio (JSON de su API o HTML de la tabla)
    en un DataFrame con `COLUMNS`. Se completa al conocer el formato real de
    /app/alertas. Placeholder por ahora."""
    raise ScraperNotConfigured(
        "El parser todavía no conoce el formato real de la respuesta de "
        "/app/alertas. Inspeccioná el sitio para definirlo."
    )


def fetch_and_store(now_iso: Optional[str] = None) -> int:
    """SCRAPER en vivo: autentica al sitio, baja el Historial de Señales, lo parsea
    y lo guarda en el almacén. Devuelve la cantidad de señales importadas.

    PENDIENTE de conexión: requiere (1) el endpoint/JSON real de /app/alertas y
    (2) el mecanismo de autenticación (token/cookie/credenciales en archivo
    gitignored). Hasta entonces lanza ScraperNotConfigured.
    """
    raise ScraperNotConfigured(
        "El importador en vivo todavía no está conectado al sitio. Falta inspeccionar "
        "cómo /app/alertas trae y protege sus datos (conectá Claude for Chrome y abrí "
        "esa página logueada, o pasá la request desde DevTools → Network). Una vez "
        "hecho, este botón traerá las señales reales. Mientras tanto se muestran las "
        "señales de ejemplo."
    )


# ── Ingesta por EMAIL (recomendado) ──────────────────────────────────────────
# Flujo: el sitio te manda un email por cada alerta nueva → un poller IMAP lee tu
# Gmail (filtrando por remitente/asunto), parsea cada correo y hace upsert en SQLite.
# Auth: Gmail APP PASSWORD en archivo gitignored (NO tu clave principal); este módulo
# solo lo lee. Pendiente: (1) email de ejemplo (parser), (2) remitente/asunto, (3) app
# password.
IMAP_HOST = "imap.gmail.com"


def parse_alert_email(raw_bytes: bytes) -> pd.DataFrame:
    """Parsea UN email de alerta → DataFrame de señales (COLUMNS). Se completa con
    un email de ejemplo real (su estructura HTML/texto)."""
    raise ScraperNotConfigured(
        "Falta un email de alerta de ejemplo para escribir el parser."
    )


def fetch_from_email(max_emails: int = 50) -> int:
    """Lee el Gmail vía IMAP, parsea los emails de alerta nuevos y hace upsert en
    SQLite. Devuelve cuántas señales NUEVAS se importaron.
    Pendiente: app password (gitignored) + remitente/asunto + parser."""
    raise ScraperNotConfigured(
        "La ingesta por email todavía no está conectada. Necesito: (1) un email de "
        "alerta de EJEMPLO (para el parser), (2) el remitente/asunto de esos correos, "
        "(3) un Gmail App Password en archivo gitignored (lo generás vos; yo no lo "
        "toco). Con eso, un poller IMAP lee tu Gmail y mete las alertas en la app."
    )

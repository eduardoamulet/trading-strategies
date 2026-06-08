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

HERE = Path(__file__).parent
STORE_PATH = HERE / "data" / "external_signals.parquet"   # gitignored (*.parquet)
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
    """Lee el almacén local. Si no existe, devuelve las señales demo (sin guardar)."""
    if STORE_PATH.exists():
        try:
            df = pd.read_parquet(STORE_PATH)
            for c in COLUMNS:
                if c not in df.columns:
                    df[c] = None
            return df[COLUMNS]
        except Exception:
            pass
    return _demo_signals()


def save_signals(df: pd.DataFrame) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df[COLUMNS].to_parquet(STORE_PATH, index=False)


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

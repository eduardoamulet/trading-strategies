"""Configuración del live_trader (renombrado de config.py → settings.py para
evitar colisión con el config.py de Polygon de options_replay cuando ambos
corren en el mismo proceso Streamlit del trading_suite).

Credenciales: NO hardcodear tokens reales acá. Se leen de variables de entorno
o de live_trader/secrets.py (en .gitignore).

Por defecto TODO apunta a SANDBOX (paper money).
"""
from __future__ import annotations

import os
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent

# ===========================================================================
# Entorno — SANDBOX por defecto. NO cambiar sin haber probado en paper.
# ===========================================================================
LIVE_TRADING_ENABLED = False   # ⚠ True = órdenes con dinero real. Mantener False.

SANDBOX_BASE_URL = "https://sandbox.tradier.com/v1"
LIVE_BASE_URL = "https://api.tradier.com/v1"

def base_url() -> str:
    return LIVE_BASE_URL if LIVE_TRADING_ENABLED else SANDBOX_BASE_URL


# ===========================================================================
# Credenciales (env vars o live_trader/secrets.py, no versionado).
# ===========================================================================
def _from_secrets(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v:
        return v
    # live_trader/secrets.py — carga por ruta para no colisionar con el módulo
    # stdlib `secrets`.
    secrets_path = _PKG_DIR / "secrets.py"
    if secrets_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_lt_secrets", secrets_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            return getattr(mod, name, default)
        except Exception:
            return default
    return default


TRADIER_SANDBOX_TOKEN = _from_secrets("TRADIER_SANDBOX_TOKEN", "")
TRADIER_SANDBOX_ACCOUNT_ID = _from_secrets("TRADIER_SANDBOX_ACCOUNT_ID", "")
TRADIER_LIVE_TOKEN = _from_secrets("TRADIER_LIVE_TOKEN", "")
TRADIER_LIVE_ACCOUNT_ID = _from_secrets("TRADIER_LIVE_ACCOUNT_ID", "")

def token() -> str:
    return TRADIER_LIVE_TOKEN if LIVE_TRADING_ENABLED else TRADIER_SANDBOX_TOKEN

def account_id() -> str:
    return TRADIER_LIVE_ACCOUNT_ID if LIVE_TRADING_ENABLED else TRADIER_SANDBOX_ACCOUNT_ID


# ===========================================================================
# Schwab Trader API (Fase 2 — thinkorswim real). OAuth2 (authorization code).
# ⚠ Para individuos NO hay sandbox: las órdenes son SIEMPRE dinero real → por eso
# SchwabBrokerAdapter.place_order queda HARD-GATED por LIVE_TRADING_ENABLED.
# Andamiaje SIN VERIFICAR hasta tener App Key/Secret aprobados.
# ===========================================================================
SCHWAB_API_BASE = "https://api.schwabapi.com"
SCHWAB_APP_KEY = _from_secrets("SCHWAB_APP_KEY", "")
SCHWAB_APP_SECRET = _from_secrets("SCHWAB_APP_SECRET", "")
# Debe coincidir EXACTO con el Callback URL registrado en el portal de Schwab.
SCHWAB_CALLBACK_URL = _from_secrets("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
# Cache del token OAuth (access ~30min + refresh ~7 días). Gitignored. NO versionar.
SCHWAB_TOKEN_PATH = str(_PKG_DIR / "secrets_schwab_token.json")


# ===========================================================================
# Límites de riesgo (validados PRE-orden en core/risk.py)
# ===========================================================================
RISK = {
    "max_position_pct": 0.10,
    "max_orders_per_day": 20,
    "daily_loss_limit_pct": 0.05,
    "max_slippage_pct": 0.05,
    "min_open_interest": 100,   # gate de liquidez principal (siempre poblado)
    "min_volume": 0,            # 0 = filtro off. El volumen al inicio de sesión
                                # es naturalmente bajo y el sandbox lo da en 0.
                                # OI es el indicador confiable. Subir solo si querés
                                # exigir flujo intradía ya formado (no al open).
    "require_confirm_live": True,
    "eod_flatten_time": "15:55",
}

SPREAD_BUCKETS = [
    {"price_min": 0,    "price_max": 100,     "max_spread": 0.03},
    {"price_min": 100,  "price_max": 300,     "max_spread": 0.05},
    {"price_min": 300,  "price_max": 600,     "max_spread": 0.10},
    {"price_min": 600,  "price_max": 1200,    "max_spread": 0.25},
    {"price_min": 1200, "price_max": 1_000_000, "max_spread": 0.50},
]

POLL_INTERVAL_SEC = 3.0
# Absoluto (basado en la ubicación del paquete) → daemon y UI comparten la
# MISMA DB sin importar el CWD desde el que se lancen.
DB_PATH = str(_PKG_DIR / "data" / "live_trader.db")

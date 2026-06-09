# Copiá este archivo a live_trader/secrets.py y completá tus credenciales.
# secrets.py está en .gitignore — NO se versiona.
#
# Sacá el token de sandbox en: https://dashboard.tradier.com/  (Sandbox → API Access)

TRADIER_SANDBOX_TOKEN = "tu_token_sandbox_aca"
TRADIER_SANDBOX_ACCOUNT_ID = "tu_account_id_sandbox"  # ej. VA00000000

# Solo si algún día operás en vivo (NO recomendado sin meses de paper):
TRADIER_LIVE_TOKEN = ""
TRADIER_LIVE_ACCOUNT_ID = ""

# ---------------------------------------------------------------------------
# Schwab Trader API (Fase 2 — thinkorswim real). Sacá App Key/Secret en:
#   https://developer.schwab.com/  → tu app → "Keys"
# El Callback URL debe coincidir EXACTO con el registrado en la app de Schwab.
# ⚠ Schwab NO tiene sandbox para individuos: son órdenes de DINERO REAL. El
# adapter bloquea place_order salvo que LIVE_TRADING_ENABLED=True (Fase 3).
# ---------------------------------------------------------------------------
SCHWAB_APP_KEY = ""
SCHWAB_APP_SECRET = ""
SCHWAB_CALLBACK_URL = "https://127.0.0.1"

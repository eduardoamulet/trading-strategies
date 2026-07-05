"""Template for config.py — copy to `config.py` and fill in your real keys.

DO NOT commit `config.py` to git. It is excluded via `.gitignore`.

Get free Alpaca paper trading credentials at:
  https://app.alpaca.markets/signup
"""
ALPACA_API_KEY    = "YOUR_ALPACA_API_KEY"
ALPACA_API_SECRET = "YOUR_ALPACA_API_SECRET"
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets/v2"

# Rate limit hacia Polygon (requests/min). Options Advanced = ilimitado → 6000 prudente;
# planes menores → 600.
POLYGON_RATE_LIMIT_PER_MIN = 6000

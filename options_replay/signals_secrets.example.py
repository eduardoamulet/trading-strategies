"""PLANTILLA — copiá este archivo a `signals_secrets.py` (gitignored) y completá tus
credenciales de Investep para la ingesta de alertas por API (la ÚNICA vía; el canal por
email/IMAP se eliminó el 2026-07-03).

NO subas signals_secrets.py a git (ya está en .gitignore).
"""

# ── Investep API (ingesta directa por API) ───────────────────────────────────────
# RECOMENDADO: usuario + clave → la app se loguea sola (POST /auth/login) y saca un
# token JWT fresco en cada fetch. El token de Investep vence ~1h, por eso NO conviene fijarlo.
INVESTEP_USER = "tu_usuario_o_email"
INVESTEP_PASSWORD = "tu_clave"
# Alternativa temporal (sin poner la clave): pegá un Bearer token; vence ~1h y lo re-pegás.
# INVESTEP_TOKEN = "eyJ..."

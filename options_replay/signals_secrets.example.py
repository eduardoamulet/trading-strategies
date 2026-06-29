"""PLANTILLA — copiá este archivo a `signals_secrets.py` (gitignored) y completá tus
datos para la ingesta automática de alertas por Gmail (IMAP).

NO subas signals_secrets.py a git (ya está en .gitignore).

Cómo obtener el Gmail App Password (no es tu contraseña normal):
  1. Cuenta de Google → Seguridad.
  2. Activá la Verificación en 2 pasos (si no está).
  3. Buscá "Contraseñas de aplicaciones" → generá una para "Correo".
  4. Pegá acá los 16 caracteres (con o sin espacios).
La podés revocar cuando quieras desde el mismo lugar.
"""

GMAIL_USER = "tu_correo@gmail.com"
GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"

# ── Investep API (ingesta directa por API · alternativa al email) ───────────────
# RECOMENDADO: usuario + clave → la app se loguea sola (POST /auth/login) y saca un
# token JWT fresco en cada fetch. El token de Investep vence ~1h, por eso NO conviene fijarlo.
INVESTEP_USER = "tu_usuario_o_email"
INVESTEP_PASSWORD = "tu_clave"
# Alternativa temporal (sin poner la clave): pegá un Bearer token; vence ~1h y lo re-pegás.
# INVESTEP_TOKEN = "eyJ..."

"""Importador de señales (investepacademyia.com — Historial de Señales).

Parser del email HTML de alerta ("N señales activas") → esquema db.COLUMNS, con dedup
por `id` estable (parse_alert_email).

Dos vías de ingesta:
  - Subir/pegar un .eml      → import_email()             (manual)
  - Poller IMAP a Gmail      → fetch_from_email()          (automático)

El `id` se deriva del token único del gráfico → la misma señal no se duplica entre
envíos. Secretos (Gmail app password) en signals_secrets.py (gitignored); este módulo
solo los lee, nunca hardcodea credenciales.
"""
from __future__ import annotations

import email as _email
import hashlib as _hashlib
import json as _json
import re as _re
from email import policy as _policy

import pandas as pd

import signals_db as db

IMAP_HOST = "imap.gmail.com"
EMAIL_SENDER = "investepacademyia.com"   # filtro IMAP

COLUMNS = db.COLUMNS

# strategyName / clase del badge → etiqueta amigable (como en el sitio/email).
STRAT_LABEL = {
    "trend-reversal": "Cambio de Tendencia en Hora",
    "trend-reversal-15m": "Cambio de Tendencia en 15m",
    "magnet-effect": "Efecto Imán",
    "midpoint-bounce": "Rebote Punto Medio",
}
# Estados posibles de una señal (como en el sitio).
ESTADOS = ["Por definir", "Aprovechada", "No aprovechada"]
_MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
          "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}


class ScraperNotConfigured(RuntimeError):
    """La ingesta automática (email/API) aún no está conectada."""


# ── Helpers ──────────────────────────────────────────────────────────────────
def _signal_id(chart_url, symbol, strat_raw, tipo, fecha, hora, uuid=None) -> str:
    """ID estable para dedup. Usa el token único del nombre del gráfico (idéntico en
    la API y en el email) → la misma señal dedupea entre fuentes. Si no hay gráfico,
    usa el UUID (API) o un hash del contenido (email)."""
    if chart_url:
        base = str(chart_url).rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if base:
            return base
    if uuid:
        return str(uuid)
    key = f"{symbol}|{strat_raw}|{tipo}|{fecha}|{hora}"
    return "h-" + _hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


# ── Parser del email HTML de alerta ──────────────────────────────────────────
def _html_from_email(raw) -> str:
    """Extrae el cuerpo HTML (o texto) de un .eml crudo, decodificando MIME/QP."""
    data = raw.encode("utf-8", "replace") if isinstance(raw, str) else raw
    try:
        msg = _email.message_from_bytes(data, policy=_policy.default)
        html = text = None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and html is None:
                html = part.get_content()
            elif ct == "text/plain" and text is None:
                text = part.get_content()
        if html or text:
            return html or text
    except Exception:
        pass
    return raw if isinstance(raw, str) else data.decode("utf-8", "replace")


def parse_alert_email(raw) -> pd.DataFrame:
    """Email HTML de alerta ('N señales activas') → DataFrame de señales."""
    html = _html_from_email(raw)
    # Timestamp del lote ("Señales generadas: 8 jun 2026, 13:56") — ya en ET.
    fecha = hora = None
    mt = _re.search(r"generadas?:\s*(\d+)\s+([A-Za-zñÑ]+)\.?\s+(\d{4}),?\s*(\d{1,2}):(\d{2})", html)
    if mt:
        d, mon, y, hh, mm = mt.groups()
        mn = _MESES.get(mon[:3].lower())
        if mn:
            fecha, hora = f"{y}-{mn:02d}-{int(d):02d}", f"{int(hh):02d}:{mm}"

    rows = []
    for ch in html.split('class="signal-card"')[1:]:
        ms = _re.search(r'signal-symbol"[^>]*>\s*([A-Z.]+)', ch)
        if not ms:
            continue
        symbol = ms.group(1)
        mtipo = _re.search(r'>\s*(PUT|CALL)\s*\(', ch)
        tipo = mtipo.group(1) if mtipo else None
        mprob = _re.search(r'signal-probability"[^>]*>\s*(\d+)\s*%?', ch)
        prob = float(mprob.group(1)) if mprob else None
        msb = _re.search(r'strategy-badge\s+([\w-]+)"[^>]*>\s*([^<]+?)\s*</span>', ch)
        strat_raw = msb.group(1) if msb else None
        strat_label = (msb.group(2).strip() if msb
                       else STRAT_LABEL.get(strat_raw, strat_raw))
        crit_list = []
        for cls, txt in _re.findall(r'class="(pass|fail)"[^>]*>([^<]+)</span>', ch):
            name = _re.sub(r'\s*(?:&#10004;|&#10008;|✔|✘)\s*$', '', txt).strip()
            if name:
                crit_list.append({"nombre": name, "ok": cls == "pass"})
        n_pass = sum(1 for c in crit_list if c["ok"])
        crit = f"{n_pass}/{len(crit_list)}" if crit_list else None
        mi = _re.search(r'<img[^>]+src="([^"]+)"', ch)
        chart = mi.group(1) if mi else None
        rows.append({
            "id": _signal_id(chart, symbol, strat_raw, tipo, fecha, hora),
            "symbol": symbol, "tipo": tipo,
            "estrategia": strat_label, "estrategia_raw": strat_raw,
            "probabilidad": prob, "fecha": fecha, "hora": hora,
            "estado": "Por definir", "ganancia": 0.0, "is_active": 1,
            "criterios": crit,
            "criterios_json": _json.dumps(crit_list, ensure_ascii=False),
            "chart_url": chart, "creado_en": None,
            "fuente": "email", "importado_en": None,
        })
    return pd.DataFrame(rows, columns=COLUMNS) if rows else pd.DataFrame(columns=COLUMNS)


# ── Imports (upsert dedup por id) ────────────────────────────────────────────
def _clamp_hora(h):
    """Hora ANTES de las 09:30 (pre-market) → '09:30' (apertura). Sobrescribe el valor."""
    s = str(h or "").strip()
    try:
        hh, mm = s.split(":")[:2]
        if (int(hh), int(mm)) < (9, 30):
            return "09:30"
    except Exception:
        pass
    return s


def _stamp(df, now_iso):
    if df.empty:
        return df
    df = df.copy()
    if now_iso is not None:
        df["importado_en"] = now_iso
    df["hora"] = df["hora"].apply(_clamp_hora)   # pre-09:30 → 09:30 (apertura)
    return df


def import_email(raw, now_iso=None) -> int:
    """Parsea un email .eml y hace upsert. Devuelve cuántas señales NUEVAS entraron."""
    return db.upsert_signals(_stamp(parse_alert_email(raw), now_iso))


def load_signals() -> pd.DataFrame:
    return db.load_signals()


# ── Poller IMAP a Gmail (automático) ─────────────────────────────────────────
def _gmail_secrets():
    try:
        import signals_secrets as s  # gitignored
        return getattr(s, "GMAIL_USER", None), getattr(s, "GMAIL_APP_PASSWORD", None)
    except Exception:
        return (None, None)


def fetch_from_email(max_emails: int = 50, now_iso=None) -> int:
    """Lee Gmail vía IMAP, parsea los emails de alerta de investepacademyia y hace
    upsert (dedup por id). Devuelve cuántas señales NUEVAS importó.
    Requiere signals_secrets.py con GMAIL_USER + GMAIL_APP_PASSWORD (gitignored)."""
    import imaplib
    user, pwd = _gmail_secrets()
    if not user or not pwd:
        raise ScraperNotConfigured(
            "Falta configurar Gmail: creá options_replay/signals_secrets.py con "
            "GMAIL_USER='tu@gmail.com' y GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx' "
            "(App Password de Google; va gitignored). Después este botón lee tu bandeja."
        )
    total_new = 0
    M = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        M.login(user, pwd)
        M.select("INBOX")
        typ, data = M.search(None, f'(FROM "{EMAIL_SENDER}")')
        ids = data[0].split() if data and data[0] else []
        for eid in reversed(ids[-max_emails:]):
            typ, msgdata = M.fetch(eid, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            df = _stamp(parse_alert_email(msgdata[0][1]), now_iso)
            total_new += db.upsert_signals(df)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return total_new

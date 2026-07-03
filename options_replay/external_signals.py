"""Importador de señales (investepacademyia.com — Historial de Señales).

ÚNICA vía de ingesta: la API directa (/signals/history, Bearer JWT con auto-login) →
fetch_from_api(). El canal por email/IMAP se ELIMINÓ (2026-07-03) a pedido del usuario:
las alertas entran SOLO por API.

El `id` se deriva del token único del gráfico (o del UUID de la API) → la misma señal
no se duplica entre bajadas. Credenciales en signals_secrets.py (gitignored); este
módulo solo las lee, nunca hardcodea credenciales.
"""
from __future__ import annotations

import hashlib as _hashlib
import json as _json

import pandas as pd

import signals_db as db

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


class ScraperNotConfigured(RuntimeError):
    """La ingesta automática (API) aún no está conectada."""


# ── Helpers ──────────────────────────────────────────────────────────────────
def _signal_id(chart_url, symbol, strat_raw, tipo, fecha, hora, uuid=None) -> str:
    """ID estable para dedup entre bajadas. Usa el token único del nombre del gráfico;
    si no hay gráfico, el UUID de la API; como último recurso, un hash del contenido."""
    if chart_url:
        base = str(chart_url).rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if base:
            return base
    if uuid:
        return str(uuid)
    key = f"{symbol}|{strat_raw}|{tipo}|{fecha}|{hora}"
    return "h-" + _hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


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


def load_signals() -> pd.DataFrame:
    return db.load_signals()


# ── API directa (investepacademyia.com /signals/history) ─────────────────────
API_BASE = "https://api.investepacademyia.com"


def parse_api_signals(items) -> pd.DataFrame:
    """Items del JSON de /signals/history → DataFrame con el esquema db.COLUMNS.
    Dedup por el token del chartUrl (o el UUID) → no duplica entre bajadas."""
    rows = []
    for it in items or []:
        symbol = it.get("symbol")
        tipo = it.get("signalType")
        strat_raw = it.get("strategyName")
        strat_label = STRAT_LABEL.get(strat_raw, strat_raw)
        try:
            prob = float(it.get("probability")) if it.get("probability") not in (None, "") else None
        except Exception:
            prob = None
        # createdAt viene en UTC ("...Z") → lo pasamos a ET (hora del mercado).
        fecha = hora = None
        ca = it.get("createdAt")
        if ca:
            try:
                _ts = pd.Timestamp(ca)
                _ts = _ts.tz_localize("UTC") if _ts.tz is None else _ts
                _et = _ts.tz_convert("America/New_York")
                fecha, hora = _et.strftime("%Y-%m-%d"), _et.strftime("%H:%M")
            except Exception:
                pass
        chart = it.get("chartUrl")
        cd = it.get("criteriaData") or {}
        crit_list = [{"nombre": f"Criterio {n}", "ok": bool(cd.get(f"criterio{n}"))}
                     for n in (1, 2, 3, 4) if f"criterio{n}" in cd]
        n_pass = sum(1 for c in crit_list if c["ok"])
        crit = f"{n_pass}/{len(crit_list)}" if crit_list else None
        rows.append({
            "id": _signal_id(chart, symbol, strat_raw, tipo, fecha, hora, uuid=it.get("id")),
            "symbol": symbol, "tipo": tipo,
            "estrategia": strat_label, "estrategia_raw": strat_raw,
            "probabilidad": prob, "fecha": fecha, "hora": hora,
            "estado": "Por definir", "ganancia": 0.0,
            "is_active": int(bool(it.get("isActive"))),
            "criterios": crit,
            "criterios_json": _json.dumps(crit_list, ensure_ascii=False),
            "chart_url": chart, "creado_en": ca,
            "fuente": "api", "importado_en": None,
        })
    return pd.DataFrame(rows, columns=COLUMNS) if rows else pd.DataFrame(columns=COLUMNS)


def _api_secrets():
    """Credenciales/token de la API desde signals_secrets.py (gitignored)."""
    try:
        import signals_secrets as s  # gitignored
        return (getattr(s, "INVESTEP_USER", None),
                getattr(s, "INVESTEP_PASSWORD", None),
                getattr(s, "INVESTEP_TOKEN", None))
    except Exception:
        return (None, None, None)


def _api_login(user, password) -> str:
    """Login a investepacademyia.com (POST /auth/login, body {usernameOrEmail, password}) → JWT.
    El token vence ~1h, así que se pide en cada fetch → siempre fresco (auto-refresh, sin pegar token)."""
    import requests
    r = requests.post(f"{API_BASE}/auth/login", timeout=30,
                      headers={"accept": "application/json", "content-type": "application/json",
                               "origin": "https://investepacademyia.com",
                               "referer": "https://investepacademyia.com/"},
                      json={"usernameOrEmail": user, "password": password})
    if r.status_code in (400, 401, 403):
        raise ScraperNotConfigured(
            f"Login Investep rechazado ({r.status_code}). Revisá INVESTEP_USER / INVESTEP_PASSWORD "
            "en signals_secrets.py.")
    r.raise_for_status()
    data = r.json() or {}
    _inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    tok = (data.get("accessToken") or data.get("token") or data.get("access_token")
           or _inner.get("accessToken") or _inner.get("token"))
    if not tok:
        raise ScraperNotConfigured(
            f"Login OK pero no ubico el token en la respuesta (claves: {list(data.keys())}). "
            "Pasame la respuesta del login para mapear la clave correcta.")
    return tok


def fetch_from_api(token=None, days_back=7, start_date=None, end_date=None,
                   limit=100, max_pages=20, now_iso=None) -> int:
    """Baja señales de la API /signals/history (paginado), parsea y hace upsert (dedup por id).
    `token` = Bearer JWT; si None lo lee de signals_secrets.INVESTEP_TOKEN. Devuelve cuántas NUEVAS.
    OJO: el token de Investep vence en ~1h → para auto-refresh hace falta el login (ver _api_login)."""
    import requests
    _u, _p, _tok = _api_secrets()
    if token is None:
        token = _tok
    if not token and _u and _p:
        token = _api_login(_u, _p)   # sin token pero con credenciales → login (token fresco, auto)
    if not token:
        raise ScraperNotConfigured(
            "Falta autenticación de Investep en options_replay/signals_secrets.py: poné "
            "INVESTEP_USER + INVESTEP_PASSWORD (recomendado: auto-refresh sin pegar token) o un "
            "INVESTEP_TOKEN temporal (Bearer, vence ~1h).")
    if start_date is None or end_date is None:
        _end = pd.Timestamp.now(tz="UTC")
        _start = _end - pd.Timedelta(days=days_back)
        start_date = start_date or _start.strftime("%Y-%m-%dT00:00:00Z")
        end_date = end_date or _end.strftime("%Y-%m-%dT23:59:59Z")
    headers = {"accept": "application/json", "authorization": f"Bearer {token}",
               "origin": "https://investepacademyia.com", "referer": "https://investepacademyia.com/"}
    all_items = []
    for page in range(1, max_pages + 1):
        r = requests.get(f"{API_BASE}/signals/history", headers=headers, timeout=30,
                         params={"page": page, "limit": limit,
                                 "startDate": start_date, "endDate": end_date})
        if r.status_code == 401:
            raise ScraperNotConfigured("Token Investep vencido/inválido (401). Renová INVESTEP_TOKEN.")
        r.raise_for_status()
        items = (r.json() or {}).get("items", [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < limit:
            break
    return db.upsert_signals(_stamp(parse_api_signals(all_items), now_iso))

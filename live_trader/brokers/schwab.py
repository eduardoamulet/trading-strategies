"""Adapter de Schwab Trader API (sucesora de la TD Ameritrade API; respalda thinkorswim).

╔══════════════════════════════════════════════════════════════════════════════╗
║  ⚠ ANDAMIAJE SIN VERIFICAR — Fase 2.                                          ║
║  Escrito contra los endpoints PÚBLICOS documentados de Schwab, pero NO se      ║
║  pudo probar todavía (falta la aprobación del App Key/Secret). Al aprobarte:   ║
║  1) Completá live_trader/secrets.py (SCHWAB_APP_KEY/SECRET/CALLBACK).          ║
║  2) Corré la autorización una vez:  py -m brokers.schwab authorize             ║
║  3) Verificá lectura:               py -m brokers.schwab check                 ║
║  4) Recién ahí ajustamos el parseo de respuestas si algo difiere.              ║
║                                                                                ║
║  ⚠ Schwab NO tiene sandbox para individuos: las órdenes son DINERO REAL.       ║
║  Por eso place_order está HARD-GATED por settings.LIVE_TRADING_ENABLED=False.  ║
╚══════════════════════════════════════════════════════════════════════════════╝

Docs: https://developer.schwab.com/  (Market Data API + Accounts and Trading API)
OAuth2 authorization-code flow: access token ~30 min, refresh token ~7 días.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

import settings
from brokers.base import BrokerAdapter, BrokerError
from core.models import Account, Contract, Fill, OrderRequest, OrderResult, Quote


# ===========================================================================
# Conversión de símbolo OCC ↔ formato Schwab
# Schwab usa 21 chars: root(6, relleno con espacios) + YYMMDD + C/P + strike(8 = $×1000).
#   OCC   "SPY260609C00738000"   (root sin relleno)
#   Schwab"SPY   260609C00738000" (root a 6 con espacios)
# ===========================================================================
def _occ_root(occ: str) -> str:
    i = 0
    while i < len(occ) and occ[i].isalpha():
        i += 1
    return occ[:i]


def occ_to_schwab(occ: str) -> str:
    """OCC estándar → símbolo Schwab (root relleno a 6 con espacios)."""
    root = _occ_root(occ)
    rest = occ[len(root):]                       # YYMMDD + C/P + 8 dígitos
    return f"{root:<6}{rest}"


def schwab_to_occ(sym: str) -> str:
    """Símbolo Schwab → OCC estándar (saca el relleno de espacios del root)."""
    return sym.replace(" ", "")


# ===========================================================================
# OAuth2 — manejo de tokens (cache en archivo gitignored)
# ===========================================================================
class SchwabAuth:
    """Maneja el flujo OAuth2 y la renovación del access token.

    El refresh token dura ~7 días → hay que re-autorizar semanalmente (limitación
    de Schwab, no del código). access_token() renueva solo mientras el refresh siga vivo.
    """

    AUTH_URL = "/v1/oauth/authorize"
    TOKEN_URL = "/v1/oauth/token"

    def __init__(self, app_key: str, app_secret: str, callback_url: str, token_path: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self.callback_url = callback_url
        self.token_path = Path(token_path)
        self._tok: dict = self._load()

    # ---- persistencia ----
    def _load(self) -> dict:
        if self.token_path.exists():
            try:
                return json.loads(self.token_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self, tok: dict) -> None:
        tok = dict(tok)
        # marca de expiración absoluta para saber cuándo renovar
        if "expires_in" in tok:
            tok["_access_expires_at"] = time.time() + float(tok["expires_in"]) - 60
        self._tok = tok
        # escritura atómica
        tmp = self.token_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tok, indent=2), encoding="utf-8")
        tmp.replace(self.token_path)

    def _basic_header(self) -> str:
        raw = f"{self.app_key}:{self.app_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    # ---- flujo de autorización (una vez) ----
    def authorization_url(self) -> str:
        q = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.app_key,
            "redirect_uri": self.callback_url,
        })
        return f"{settings.SCHWAB_API_BASE}{self.AUTH_URL}?{q}"

    def exchange_code(self, code: str) -> dict:
        """Intercambia el 'code' (de la URL de redirect) por access+refresh token."""
        r = requests.post(
            settings.SCHWAB_API_BASE + self.TOKEN_URL,
            headers={"Authorization": self._basic_header(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": self.callback_url},
            timeout=20,
        )
        r.raise_for_status()
        tok = r.json()
        self._save(tok)
        return tok

    def refresh(self) -> dict:
        rt = self._tok.get("refresh_token")
        if not rt:
            raise BrokerError("Sin refresh_token — corré la autorización primero "
                              "(py -m brokers.schwab authorize).")
        r = requests.post(
            settings.SCHWAB_API_BASE + self.TOKEN_URL,
            headers={"Authorization": self._basic_header(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": rt},
            timeout=20,
        )
        if r.status_code >= 400:
            raise BrokerError(f"Refresh falló ({r.status_code}): {r.text[:160]} — "
                              "probablemente el refresh token expiró (~7 días). Re-autorizá.")
        tok = r.json()
        # Schwab devuelve refresh_token nuevo en el refresh; si no, conservá el viejo.
        tok.setdefault("refresh_token", rt)
        self._save(tok)
        return tok

    def access_token(self) -> str:
        if not self._tok.get("access_token"):
            raise BrokerError("Sin access_token — corré: py -m brokers.schwab authorize")
        if time.time() >= self._tok.get("_access_expires_at", 0):
            self.refresh()
        return self._tok["access_token"]


# ===========================================================================
# Adapter
# ===========================================================================
class SchwabBrokerAdapter(BrokerAdapter):
    MD = "/marketdata/v1"     # market data
    TR = "/trader/v1"         # accounts & trading

    def __init__(self):
        if not settings.SCHWAB_APP_KEY or not settings.SCHWAB_APP_SECRET:
            raise BrokerError(
                "Faltan SCHWAB_APP_KEY/SCHWAB_APP_SECRET. Ponelos en live_trader/secrets.py "
                "(copiá de secrets.example.py). Andamiaje de Fase 2 — sin verificar.")
        self.base = settings.SCHWAB_API_BASE
        self.auth = SchwabAuth(settings.SCHWAB_APP_KEY, settings.SCHWAB_APP_SECRET,
                               settings.SCHWAB_CALLBACK_URL, settings.SCHWAB_TOKEN_PATH)
        self._s = requests.Session()
        self._account_hash: Optional[str] = None

    # ---------- HTTP ----------
    def _req(self, method: str, path: str, params=None, json_body=None, retries=3) -> requests.Response:
        url = self.base + path
        last = None
        for a in range(retries):
            headers = {"Authorization": f"Bearer {self.auth.access_token()}",
                       "Accept": "application/json"}
            try:
                r = self._s.request(method, url, params=params, json=json_body,
                                    headers=headers, timeout=20)
                if r.status_code == 401:
                    self.auth.refresh()                 # token venció → renovar y reintentar
                    last = BrokerError("401"); continue
                if r.status_code == 429 or r.status_code >= 500:
                    last = BrokerError(f"{r.status_code} {r.text[:120]}")
                    time.sleep(2 ** a); continue
                r.raise_for_status()
                return r
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e; time.sleep(2 ** a)
        raise last or BrokerError("retries exhausted")

    def _json(self, *a, **k) -> dict:
        return self._req(*a, **k).json()

    # ---------- account hash (Schwab usa números de cuenta cifrados en las URLs) ----------
    def _hash(self) -> str:
        if self._account_hash:
            return self._account_hash
        d = self._json("GET", f"{self.TR}/accounts/accountNumbers")
        # [{"accountNumber": "...", "hashValue": "..."}]
        if not d:
            raise BrokerError("La API no devolvió cuentas (accountNumbers).")
        self._account_hash = d[0]["hashValue"]
        return self._account_hash

    # ---------- market data ----------
    def nearest_expiry(self, ticker: str) -> Optional[str]:
        # expirationchain: lista de vencimientos disponibles para el subyacente.
        d = self._json("GET", f"{self.MD}/expirationchain", params={"symbol": ticker})
        exps = d.get("expirationList") or []
        dates = sorted({e.get("expirationDate") for e in exps if e.get("expirationDate")})
        return dates[0] if dates else None

    def get_option_chain(self, ticker: str, expiry: Optional[str] = None) -> list[Contract]:
        params = {"symbol": ticker, "contractType": "ALL"}
        if expiry:
            params["fromDate"] = expiry
            params["toDate"] = expiry
        d = self._json("GET", f"{self.MD}/chains", params=params)
        out: list[Contract] = []
        for right, key in (("C", "callExpDateMap"), ("P", "putExpDateMap")):
            exp_map = d.get(key) or {}
            for exp_key, strikes in exp_map.items():          # "2026-06-09:0" -> {...}
                exp_date = exp_key.split(":")[0]
                for strike_str, legs in strikes.items():
                    for o in legs:
                        out.append(Contract(
                            occ=schwab_to_occ(o.get("symbol", "")),
                            underlying=ticker, strike=float(strike_str), expiry=exp_date,
                            right=right,
                            bid=float(o.get("bid") or 0.0), ask=float(o.get("ask") or 0.0),
                            last=float(o.get("last") or o.get("mark") or 0.0),
                            volume=int(o.get("totalVolume") or 0),
                            open_interest=int(o.get("openInterest") or 0),
                            delta=o.get("delta"),
                        ))
        return out

    def get_underlying_price(self, ticker: str) -> float:
        d = self._json("GET", f"{self.MD}/{urllib.parse.quote(ticker)}/quotes")
        node = d.get(ticker) or {}
        q = node.get("quote") or {}
        return float(q.get("lastPrice") or q.get("closePrice") or 0.0)

    def get_quote(self, occ_symbol: str) -> Quote:
        schwab_sym = occ_to_schwab(occ_symbol)
        d = self._json("GET", f"{self.MD}/quotes",
                       params={"symbols": schwab_sym, "indicative": "false"})
        node = d.get(schwab_sym) or d.get(occ_symbol) or {}
        q = node.get("quote") or {}
        if not q:
            raise BrokerError(f"Sin quote para {occ_symbol}")
        return Quote(
            occ=occ_symbol,
            bid=float(q.get("bidPrice") or 0.0), ask=float(q.get("askPrice") or 0.0),
            last=float(q.get("lastPrice") or 0.0),
            volume=int(q.get("totalVolume") or 0),
            open_interest=int(q.get("openInterest") or 0),
            delta=q.get("delta"), ts=datetime.utcnow(),
        )

    def get_account(self) -> Account:
        d = self._json("GET", f"{self.TR}/accounts/{self._hash()}")
        sec = d.get("securitiesAccount") or {}
        bal = sec.get("currentBalances") or {}
        equity = float(bal.get("liquidationValue") or bal.get("equity") or 0.0)
        bp = float(bal.get("buyingPower") or bal.get("cashAvailableForTrading") or 0.0)
        opt_bp = float(bal.get("optionBuyingPower") or bp)
        return Account(equity=equity, buying_power=bp, option_buying_power=opt_bp)

    # ---------- orders ----------
    def place_order(self, req: OrderRequest) -> OrderResult:
        # ╔═══════════════════════════════════════════════════════════════════╗
        # ║  HARD GATE — Schwab = DINERO REAL (sin sandbox para individuos).    ║
        # ╚═══════════════════════════════════════════════════════════════════╝
        if not settings.LIVE_TRADING_ENABLED:
            raise BrokerError(
                "place_order BLOQUEADO: Schwab opera con dinero REAL y "
                "LIVE_TRADING_ENABLED=False (intencional). Para operar en vivo se requiere "
                "habilitarlo explícitamente + confirmación manual (Fase 3). Mientras tanto, "
                "usá el bróker paper (Tradier sandbox).")
        if req.type != "limit":
            raise BrokerError("Solo se permiten órdenes LIMIT.")
        instruction = "BUY_TO_OPEN" if req.side == "buy_to_open" else "SELL_TO_CLOSE"
        body = {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "duration": "DAY" if req.duration == "day" else req.duration.upper(),
            "price": f"{req.limit_price:.2f}",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": instruction,
                "quantity": int(req.qty),
                "instrument": {"symbol": occ_to_schwab(req.occ), "assetType": "OPTION"},
            }],
        }
        r = self._req("POST", f"{self.TR}/accounts/{self._hash()}/orders", json_body=body)
        # Schwab devuelve 201 y el order id en el header Location (.../orders/{id})
        oid = ""
        loc = r.headers.get("Location", "")
        if loc:
            oid = loc.rstrip("/").split("/")[-1]
        return OrderResult(order_id=oid, status="ok" if oid else "rejected",
                           raw={"location": loc, "status_code": r.status_code})

    def get_order(self, order_id: str) -> Fill:
        d = self._json("GET", f"{self.TR}/accounts/{self._hash()}/orders/{order_id}")
        st = (d.get("status") or "PENDING_ACTIVATION").upper()
        filled = int(float(d.get("filledQuantity") or 0))
        # precio promedio desde las patas de ejecución
        avg, n = 0.0, 0
        for act in d.get("orderActivityCollection") or []:
            for leg in act.get("executionLegs") or []:
                px = float(leg.get("price") or 0.0)
                if px:
                    avg += px; n += 1
        avg = (avg / n) if n else 0.0
        status_map = {
            "FILLED": "filled", "PARTIALLY_FILLED": "partial",
            "WORKING": "pending", "QUEUED": "pending", "ACCEPTED": "pending",
            "PENDING_ACTIVATION": "pending", "AWAITING_MANUAL_REVIEW": "pending",
            "CANCELED": "canceled", "REJECTED": "rejected", "EXPIRED": "canceled",
        }
        return Fill(order_id=order_id, status=status_map.get(st, "pending"),
                    filled_qty=filled, avg_price=avg, time=datetime.utcnow())

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._req("DELETE", f"{self.TR}/accounts/{self._hash()}/orders/{order_id}")
            return True
        except Exception:
            return False


# ===========================================================================
# Utilidades de línea de comandos para la puesta a punto (corré VOS, no se loguea solo).
#   py -m brokers.schwab authorize   → imprime la URL, pegás el redirect, guarda token
#   py -m brokers.schwab check       → prueba lectura (cuenta + quote SPY), sin operar
# ===========================================================================
def _cli_authorize() -> None:
    auth = SchwabAuth(settings.SCHWAB_APP_KEY, settings.SCHWAB_APP_SECRET,
                      settings.SCHWAB_CALLBACK_URL, settings.SCHWAB_TOKEN_PATH)
    print("\n1) Abrí esta URL en el navegador, logueate en Schwab y autorizá:\n")
    print("   " + auth.authorization_url())
    print("\n2) Te va a redirigir a tu Callback URL con '?code=...' en la barra.")
    print("   Pegá acá la URL COMPLETA del redirect (o solo el valor de 'code'):\n")
    pasted = input("   redirect/code> ").strip()
    if "code=" in pasted:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [""])[0]
    else:
        code = pasted
    if not code:
        print("   ✗ No se encontró el 'code'.")
        return
    auth.exchange_code(code)
    print(f"   ✓ Token guardado en {settings.SCHWAB_TOKEN_PATH} (gitignored).")


def _cli_check() -> None:
    b = SchwabBrokerAdapter()
    acct = b.get_account()
    print(f"Cuenta: equity ${acct.equity:,.2f} · buying power ${acct.buying_power:,.2f}")
    print(f"SPY last: ${b.get_underlying_price('SPY'):.2f}")
    exp = b.nearest_expiry("SPY")
    chain = b.get_option_chain("SPY", exp)
    print(f"SPY exp {exp}: {len(chain)} contratos en el chain.")
    print("✓ Lectura OK (no se colocó ninguna orden).")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "authorize":
        _cli_authorize()
    elif cmd == "check":
        _cli_check()
    else:
        print("Uso: py -m brokers.schwab [authorize|check]")

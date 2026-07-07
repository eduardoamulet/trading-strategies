"""Adapter de Alpaca (REST). Apunta a PAPER por defecto (ver settings.py).

Docs: https://docs.alpaca.markets/  (Trading API + Market Data API de opciones)
Paper:   https://paper-api.alpaca.markets   (órdenes simuladas con NBBO real)
Data:    https://data.alpaca.markets        (feed 'indicative' gratis; 'opra' con suscripción)

Por qué Alpaca además de Tradier: el sandbox de Tradier llena órdenes de forma poco
realista y sus quotes van con 15 min de delay — sirve para plomería, no para calibrar
slippage. El paper de Alpaca simula fills contra el NBBO real → es la Fase 2 del plan
(medir slippage real vs el supuesto del backtest: comprar al ask / marcar al bid).

Requisitos de la cuenta: options approved level ≥ 2 (comprar calls/puts).
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests

import settings
from brokers.base import BrokerAdapter, BrokerError
from core.models import Account, Contract, Fill, OrderRequest, OrderResult, Quote


def _parse_occ(occ: str) -> tuple[str, str, str, float]:
    """OCC 'QQQ260107C00623000' → (root, expiry 'YYYY-MM-DD', right 'C'|'P', strike).
    Evita una 2ª llamada al endpoint de contratos: todo viaja en el símbolo."""
    m = re.match(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$", occ.strip().upper())
    if not m:
        raise BrokerError(f"OCC inválido: {occ!r}")
    root, ymd, right, k = m.groups()
    expiry = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}"
    return root, expiry, right, int(k) / 1000.0


def _clean_tag(tag) -> str:
    # client_order_id de Alpaca: hasta 48 chars; conservador con el charset.
    return re.sub(r"[^A-Za-z0-9-]", "-", str(tag or ""))[:48]


class AlpacaAdapter(BrokerAdapter):
    def __init__(self):
        self.base = settings.alpaca_base_url()          # trading (paper o live)
        self.data_base = settings.ALPACA_DATA_BASE_URL  # market data (mismo host siempre)
        self._feed = settings.ALPACA_DATA_FEED          # 'indicative' (gratis) | 'opra'
        key, secret = settings.alpaca_key_id(), settings.alpaca_secret()
        if not key or not secret:
            raise BrokerError("Faltan ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET. "
                              "Ponelos en env vars o en live_trader/secrets.py")
        self._s = requests.Session()
        self._s.headers.update({
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        })

    # ---------- HTTP con retry/backoff (mismo contrato que TradierAdapter._req) ----------
    def _req(self, method: str, url: str, params=None, json_body=None, retries=3) -> dict:
        last = None
        for a in range(retries):
            try:
                r = self._s.request(method, url, params=params, json=json_body, timeout=20)
                if r.status_code == 429 or r.status_code >= 500:
                    last = BrokerError(f"{r.status_code} {r.text[:120]}")
                    time.sleep(2 ** a)
                    continue
                if r.status_code >= 400:   # 4xx: surface el detalle de Alpaca
                    raise BrokerError(f"{r.status_code} {r.text[:300]}")
                if r.status_code == 204 or not r.text:
                    return {}
                return r.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                time.sleep(2 ** a)
        raise last or BrokerError("retries exhausted")

    def _trade(self, method: str, path: str, params=None, json_body=None) -> dict:
        return self._req(method, self.base + path, params=params, json_body=json_body)

    def _data(self, path: str, params=None) -> dict:
        return self._req("GET", self.data_base + path, params=params)

    # ---------- market data ----------
    def nearest_expiry(self, ticker: str) -> Optional[str]:
        """Alpaca no tiene endpoint de expiraciones → se deriva del listado de contratos
        en una ventana corta (0DTE en SPY/QQQ/IWM existe casi todos los días hábiles)."""
        hoy = date.today()
        for dias in (7, 31):
            d = self._trade("GET", "/v2/options/contracts", params={
                "underlying_symbols": ticker,
                "expiration_date_gte": hoy.isoformat(),
                "expiration_date_lte": (hoy + timedelta(days=dias)).isoformat(),
                "limit": 500,
            })
            cts = d.get("option_contracts") or []
            if cts:
                return min(c["expiration_date"] for c in cts)
        return None

    def _contracts(self, ticker: str, expiry: str) -> dict:
        """{occ: open_interest} del vencimiento (paginado). El OI no viene en los
        snapshots de data — y es el gate de liquidez principal de core/risk.py."""
        oi: dict[str, int] = {}
        token = None
        while True:
            params = {"underlying_symbols": ticker, "expiration_date": expiry, "limit": 500}
            if token:
                params["page_token"] = token
            d = self._trade("GET", "/v2/options/contracts", params=params)
            for c in d.get("option_contracts") or []:
                oi[c["symbol"]] = int(float(c.get("open_interest") or 0))
            token = d.get("next_page_token")
            if not token:
                return oi

    def _snapshots(self, ticker: str, expiry: str) -> dict:
        """{occ: snapshot} con latestQuote/latestTrade/greeks (paginado). Si el feed
        configurado no está permitido (sin suscripción OPRA), cae a 'indicative'."""
        out: dict[str, dict] = {}
        feed = self._feed
        token = None
        while True:
            params = {"feed": feed, "limit": 1000, "expiration_date": expiry}
            if token:
                params["page_token"] = token
            try:
                d = self._data(f"/v1beta1/options/snapshots/{ticker}", params=params)
            except BrokerError as e:
                if feed != "indicative" and ("403" in str(e) or "subscription" in str(e).lower()):
                    feed, token = "indicative", None   # sin OPRA → feed indicativo y de cero
                    out.clear()
                    continue
                raise
            out.update(d.get("snapshots") or {})
            token = d.get("next_page_token")
            if not token:
                return out

    def get_option_chain(self, ticker: str, expiry: Optional[str] = None) -> list[Contract]:
        if expiry is None:
            expiry = self.nearest_expiry(ticker)
        if not expiry:
            raise BrokerError(f"Sin expiraciones para {ticker}")
        oi_por_occ = self._contracts(ticker, expiry)
        snaps = self._snapshots(ticker, expiry)
        out: list[Contract] = []
        for occ, s in snaps.items():
            try:
                _, exp, right, strike = _parse_occ(occ)
            except BrokerError:
                continue
            q = s.get("latestQuote") or {}
            t = s.get("latestTrade") or {}
            g = s.get("greeks") or {}
            day = s.get("dailyBar") or {}
            out.append(Contract(
                occ=occ, underlying=ticker,
                strike=strike, expiry=exp, right=right,
                bid=float(q.get("bp") or 0.0), ask=float(q.get("ap") or 0.0),
                last=float(t.get("p") or 0.0),
                volume=int(day.get("v") or 0),
                open_interest=int(oi_por_occ.get(occ, 0)),
                delta=g.get("delta"),
            ))
        out.sort(key=lambda c: (c.right, c.strike))
        return out

    def get_underlying_price(self, ticker: str) -> float:
        # feed 'iex' = gratis (suficiente para ATM/ITM); 'sip' requiere suscripción.
        d = self._data(f"/v2/stocks/{ticker}/trades/latest", params={"feed": "iex"})
        p = float((d.get("trade") or {}).get("p") or 0.0)
        if p:
            return p
        d = self._data(f"/v2/stocks/{ticker}/snapshot", params={"feed": "iex"})
        return float(((d.get("latestTrade") or {}).get("p")) or 0.0)

    def get_quote(self, occ_symbol: str) -> Quote:
        # snapshots?symbols= trae quote + greeks en una llamada; fallback a quotes/latest.
        try:
            d = self._data("/v1beta1/options/snapshots",
                           params={"symbols": occ_symbol, "feed": self._feed})
            s = (d.get("snapshots") or {}).get(occ_symbol) or {}
        except BrokerError:
            s = {}
        if not s:
            d = self._data("/v1beta1/options/quotes/latest",
                           params={"symbols": occ_symbol, "feed": self._feed})
            q = (d.get("quotes") or {}).get(occ_symbol)
            if not q:
                raise BrokerError(f"Sin quote para {occ_symbol}")
            return Quote(occ=occ_symbol, bid=float(q.get("bp") or 0.0),
                         ask=float(q.get("ap") or 0.0), last=0.0, ts=datetime.utcnow())
        q = s.get("latestQuote") or {}
        t = s.get("latestTrade") or {}
        g = s.get("greeks") or {}
        day = s.get("dailyBar") or {}
        return Quote(
            occ=occ_symbol,
            bid=float(q.get("bp") or 0.0), ask=float(q.get("ap") or 0.0),
            last=float(t.get("p") or 0.0),
            volume=int(day.get("v") or 0),
            delta=g.get("delta"), ts=datetime.utcnow(),
        )

    # ---------- account ----------
    def get_account(self) -> Account:
        d = self._trade("GET", "/v2/account")
        equity = float(d.get("equity") or 0.0)
        bp = float(d.get("buying_power") or 0.0)
        obp = float(d.get("options_buying_power") or bp or 0.0)
        return Account(equity=equity, buying_power=bp, option_buying_power=obp)

    # ---------- orders ----------
    def place_order(self, req: OrderRequest) -> OrderResult:
        if req.type != "limit":
            raise BrokerError("Solo se permiten órdenes LIMIT (slippage).")
        # side de la casa (buy_to_open/sell_to_close) → side + position_intent de Alpaca.
        _side = "buy" if req.side.startswith("buy") else "sell"
        body = {
            "symbol": req.occ,
            "qty": str(req.qty),
            "side": _side,
            "position_intent": req.side,          # buy_to_open / sell_to_close
            "type": "limit",
            "limit_price": f"{req.limit_price:.2f}",
            "time_in_force": "day",               # opciones: solo 'day'
            "client_order_id": _clean_tag(req.tag) or None,
        }
        body = {k: v for k, v in body.items() if v is not None}
        d = self._trade("POST", "/v2/orders", json_body=body)
        oid = str(d.get("id") or "")
        status = str(d.get("status") or "")
        return OrderResult(order_id=oid,
                           status="ok" if oid and status != "rejected" else "rejected",
                           raw=d)

    _STATUS_MAP = {
        "filled": "filled", "partially_filled": "partial",
        "new": "pending", "accepted": "pending", "pending_new": "pending",
        "accepted_for_bidding": "pending", "held": "pending", "calculated": "pending",
        "canceled": "canceled", "expired": "canceled", "done_for_day": "canceled",
        "pending_cancel": "pending", "pending_replace": "pending",
        "replaced": "pending", "stopped": "pending", "suspended": "pending",
        "rejected": "rejected",
    }

    def get_order(self, order_id: str) -> Fill:
        d = self._trade("GET", f"/v2/orders/{order_id}")
        st = str(d.get("status") or "pending")
        return Fill(order_id=order_id,
                    status=self._STATUS_MAP.get(st, "pending"),
                    filled_qty=int(float(d.get("filled_qty") or 0)),
                    avg_price=float(d.get("filled_avg_price") or 0.0),
                    time=datetime.utcnow())

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._trade("DELETE", f"/v2/orders/{order_id}")
            return True
        except Exception:
            return False

"""Adapter de Tradier (REST). Apunta a SANDBOX por defecto (ver settings.py).

Docs: https://documentation.tradier.com/brokerage-api
Sandbox: https://sandbox.tradier.com/v1  (paper money, quotes con delay 15min)
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Optional

import requests

import settings
from brokers.base import BrokerAdapter, BrokerError
from core.models import Account, Contract, Fill, OrderRequest, OrderResult, Quote


def _occ_underlying(occ: str) -> str:
    # OCC: ROOT(1-6) + YYMMDD + C/P + strike*1000. El root son las letras iniciales.
    i = 0
    while i < len(occ) and occ[i].isalpha():
        i += 1
    return occ[:i]


def _clean_tag(tag) -> str:
    # Tradier SOLO acepta letras, números y guiones en el tag; un "_" (u otro símbolo)
    # devuelve 400 "Invalid parameter, tag: contains invalid characters". Sanitizamos.
    return re.sub(r"[^A-Za-z0-9-]", "-", str(tag or ""))[:255]


class TradierAdapter(BrokerAdapter):
    def __init__(self):
        self.base = settings.base_url()
        self._token = settings.token()
        self._account = settings.account_id()
        if not self._token:
            raise BrokerError(
                "Falta TRADIER_SANDBOX_TOKEN. Ponelo en env var o en live_trader/secrets.py")
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        })

    # ---------- HTTP con retry/backoff ----------
    def _req(self, method: str, path: str, params=None, data=None, retries=3) -> dict:
        url = self.base + path
        last = None
        for a in range(retries):
            try:
                r = self._s.request(method, url, params=params, data=data, timeout=20)
                if r.status_code == 429 or r.status_code >= 500:
                    last = BrokerError(f"{r.status_code} {r.text[:120]}")
                    time.sleep(2 ** a)
                    continue
                if r.status_code >= 400:   # 4xx: surface el detalle de Tradier (no el genérico)
                    raise BrokerError(f"{r.status_code} {r.text[:300]}")
                return r.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                time.sleep(2 ** a)
        raise last or BrokerError("retries exhausted")

    # ---------- market data ----------
    def nearest_expiry(self, ticker: str) -> Optional[str]:
        d = self._req("GET", "/markets/options/expirations",
                      params={"symbol": ticker, "includeAllRoots": "true"})
        exps = (d.get("expirations") or {}).get("date")
        if not exps:
            return None
        return exps[0] if isinstance(exps, list) else exps

    def get_option_chain(self, ticker: str, expiry: Optional[str] = None) -> list[Contract]:
        if expiry is None:
            expiry = self.nearest_expiry(ticker)
        if not expiry:
            raise BrokerError(f"Sin expiraciones para {ticker}")
        d = self._req("GET", "/markets/options/chains",
                      params={"symbol": ticker, "expiration": expiry, "greeks": "true"})
        opts = (d.get("options") or {}).get("option") or []
        if isinstance(opts, dict):
            opts = [opts]
        out: list[Contract] = []
        for o in opts:
            greeks = o.get("greeks") or {}
            out.append(Contract(
                occ=o["symbol"], underlying=ticker,
                strike=float(o["strike"]), expiry=o["expiration_date"],
                right="C" if o["option_type"] == "call" else "P",
                bid=float(o.get("bid") or 0.0), ask=float(o.get("ask") or 0.0),
                last=float(o.get("last") or 0.0),
                volume=int(o.get("volume") or 0),
                open_interest=int(o.get("open_interest") or 0),
                delta=greeks.get("delta"),
            ))
        return out

    def get_underlying_price(self, ticker: str) -> float:
        d = self._req("GET", "/markets/quotes", params={"symbols": ticker})
        q = (d.get("quotes") or {}).get("quote")
        if isinstance(q, list):
            q = q[0]
        return float(q.get("last") or q.get("close") or 0.0)

    def get_quote(self, occ_symbol: str) -> Quote:
        d = self._req("GET", "/markets/quotes",
                      params={"symbols": occ_symbol, "greeks": "true"})
        q = (d.get("quotes") or {}).get("quote")
        if isinstance(q, list):
            q = q[0]
        if not q:
            raise BrokerError(f"Sin quote para {occ_symbol}")
        greeks = q.get("greeks") or {}
        return Quote(
            occ=occ_symbol,
            bid=float(q.get("bid") or 0.0), ask=float(q.get("ask") or 0.0),
            last=float(q.get("last") or 0.0),
            volume=int(q.get("volume") or 0),
            open_interest=int(q.get("open_interest") or 0),
            delta=greeks.get("delta"), ts=datetime.utcnow(),
        )

    # ---------- account ----------
    def get_account(self) -> Account:
        d = self._req("GET", f"/accounts/{self._account}/balances")
        b = d.get("balances") or {}
        equity = float(b.get("total_equity") or b.get("equity") or 0.0)
        # option_buying_power varía según tipo de cuenta (margin/cash)
        margin = b.get("margin") or {}
        cash = b.get("cash") or {}
        bp = float(margin.get("option_buying_power")
                   or cash.get("cash_available")
                   or b.get("total_cash") or 0.0)
        return Account(equity=equity, buying_power=bp, option_buying_power=bp)

    # ---------- orders ----------
    def place_order(self, req: OrderRequest) -> OrderResult:
        if req.type != "limit":
            raise BrokerError("Solo se permiten órdenes LIMIT (slippage).")
        data = {
            "class": "option",
            "symbol": req.underlying,
            "option_symbol": req.occ,
            "side": req.side,                 # buy_to_open / sell_to_close
            "quantity": str(req.qty),
            "type": "limit",
            "duration": req.duration,
            "price": f"{req.limit_price:.2f}",
            "tag": _clean_tag(req.tag),
        }
        d = self._req("POST", f"/accounts/{self._account}/orders", data=data)
        o = d.get("order") or {}
        status = o.get("status", "error")
        oid = str(o.get("id", ""))
        return OrderResult(order_id=oid,
                           status="ok" if oid and status != "rejected" else "rejected",
                           raw=d)

    def get_order(self, order_id: str) -> Fill:
        d = self._req("GET", f"/accounts/{self._account}/orders/{order_id}")
        o = d.get("order") or {}
        st = o.get("status", "pending")
        exec_qty = int(float(o.get("exec_quantity") or 0))
        avg = float(o.get("avg_fill_price") or 0.0)
        # mapear estados de Tradier → nuestros
        status_map = {
            "filled": "filled", "partially_filled": "partial", "open": "pending",
            "pending": "pending", "canceled": "canceled", "rejected": "rejected",
            "expired": "canceled",
        }
        return Fill(order_id=order_id, status=status_map.get(st, "pending"),
                    filled_qty=exec_qty, avg_price=avg, time=datetime.utcnow())

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._req("DELETE", f"/accounts/{self._account}/orders/{order_id}")
            return True
        except Exception:
            return False

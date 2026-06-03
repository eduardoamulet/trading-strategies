"""RiskGuard — validaciones PRE-orden + circuit breakers. NADA va al broker
sin pasar validate_entry() sin errores."""
from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import settings
from core.models import Account, Contract

ET = ZoneInfo("America/New_York")


class RiskGuard:
    def __init__(self, store):
        self.store = store
        self.r = settings.RISK

    # ---------- helpers ----------
    @staticmethod
    def is_market_open(now_et: datetime | None = None) -> bool:
        now = now_et or datetime.now(ET)
        if now.weekday() >= 5:
            return False
        return dtime(9, 30) <= now.time() <= dtime(16, 0)

    def past_eod_flatten(self, now_et: datetime | None = None) -> bool:
        now = now_et or datetime.now(ET)
        hh, mm = map(int, self.r["eod_flatten_time"].split(":"))
        return now.time() >= dtime(hh, mm)

    def _daily_realized_pnl(self) -> float:
        today = datetime.now(ET).date().isoformat()
        return sum(
            (p.get("pnl_net") or 0.0)
            for p in self.store.all_positions()
            if p.get("status") == "closed" and (p.get("exit_time") or "").startswith(today)
        )

    def _daily_order_count(self) -> int:
        today = datetime.now(ET).date().isoformat()
        return sum(1 for p in self.store.all_positions()
                   if (p.get("entry_time") or "").startswith(today))

    def circuit_breaker_tripped(self, account: Account) -> bool:
        loss_limit = -abs(self.r["daily_loss_limit_pct"]) * account.equity
        return self._daily_realized_pnl() <= loss_limit

    # ---------- la validación principal ----------
    def validate_entry(self, c: Contract, qty: int, account: Account) -> list[str]:
        errs: list[str] = []
        if not self.is_market_open():
            errs.append("Mercado cerrado.")
        if self.past_eod_flatten():
            errs.append(f"Pasada la hora de EOD flatten ({self.r['eod_flatten_time']} ET) — "
                        "no abrir 0DTE (riesgo de asignación).")
        if c.bid <= 0 or c.ask <= 0:
            errs.append("Contrato sin quote válido (bid/ask en 0).")
        if c.spread > self._bucket_max_spread(account, c):
            errs.append(f"Spread ${c.spread:.2f} excede el máximo del bucket.")
        if c.open_interest < self.r["min_open_interest"]:
            errs.append(f"Open Interest {c.open_interest} < mínimo {self.r['min_open_interest']}.")
        if c.volume < self.r["min_volume"]:
            errs.append(f"Volumen {c.volume} < mínimo {self.r['min_volume']}.")
        cost = c.ask * qty * 100
        if cost > account.buying_power:
            errs.append(f"Costo ${cost:,.2f} > buying power ${account.buying_power:,.2f}.")
        if account.equity > 0 and cost > account.equity * self.r["max_position_pct"]:
            errs.append(f"Costo ${cost:,.2f} excede {self.r['max_position_pct']:.0%} del equity.")
        if self._daily_order_count() >= self.r["max_orders_per_day"]:
            errs.append(f"Límite de órdenes diarias ({self.r['max_orders_per_day']}) alcanzado.")
        if self.circuit_breaker_tripped(account):
            errs.append("CIRCUIT BREAKER: pérdida diaria máxima alcanzada — entradas bloqueadas.")
        return errs

    def _bucket_max_spread(self, account: Account, c: Contract) -> float:
        # usa el precio del subyacente si lo tuviéramos; aproximamos con strike.
        spot = c.strike
        for b in settings.SPREAD_BUCKETS:
            if b["price_min"] <= spot < b["price_max"]:
                return b["max_spread"]
        return float("inf")

    def quote_drifted(self, ref_ask: float, current_ask: float) -> bool:
        """True si el ask se movió más que max_slippage_pct entre selección y envío."""
        if ref_ask <= 0:
            return False
        return abs(current_ask - ref_ask) / ref_ask > self.r["max_slippage_pct"]

"""Selección de contrato — misma filosofía que el backtest (engine.py):
filtro de spread por bucket de precio + liquidez mínima + cercanía a ATM/1-ITM,
priorizando (menor spread, cercanía a ITM, mayor open interest)."""
from __future__ import annotations

import settings
from core.models import Contract


class NoLiquidContract(Exception):
    pass


class ContractSelector:
    def __init__(self, min_oi: int | None = None, min_vol: int | None = None,
                 buckets: list | None = None):
        self.min_oi = settings.RISK["min_open_interest"] if min_oi is None else min_oi
        self.min_vol = settings.RISK["min_volume"] if min_vol is None else min_vol
        self.buckets = buckets or settings.SPREAD_BUCKETS

    def _max_spread(self, spot: float) -> float:
        for b in self.buckets:
            if b["price_min"] <= spot < b["price_max"]:
                return b["max_spread"]
        return float("inf")

    def select(self, chain: list[Contract], side: str, spot: float,
               strategy: str = "atm") -> Contract:
        """side: 'CALL'|'PUT'. strategy: 'atm' (más cercano al spot) | 'itm' (1-ITM)."""
        right = "C" if side.upper() == "CALL" else "P"
        max_spread = self._max_spread(spot)
        liquid = [
            c for c in chain
            if c.right == right
            and c.bid > 0 and c.ask > 0
            and c.spread <= max_spread
            and c.open_interest >= self.min_oi
            and c.volume >= self.min_vol
        ]
        if not liquid:
            raise NoLiquidContract(
                f"Ningún {side} con spread<=${max_spread:.2f}, OI>={self.min_oi}, "
                f"vol>={self.min_vol} (spot ${spot:.2f}).")

        def itm_rank(c: Contract) -> float:
            depth = (spot - c.strike) if right == "C" else (c.strike - spot)
            if strategy == "itm":
                return depth if depth >= 0 else abs(depth) + 1e6  # 1-ITM, OTM al final
            return abs(c.strike - spot)                            # ATM: más cercano

        # Prioridad: (1) menor spread (a centavo), (2) ATM/1-ITM, (3) mayor OI
        return min(liquid, key=lambda c: (round(c.spread, 2), itm_rank(c), -c.open_interest))

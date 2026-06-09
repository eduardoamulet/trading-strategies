"""Selección de contrato — usa el NÚCLEO COMPARTIDO (strategy_core) para que la regla
sea IDÉNTICA a la del backtest (options_replay/engine.py, criterio "Opción 1"):
filtro de spread por bucket de precio + liquidez mínima (OI/vol) + orden
(menor spread → cercanía ATM/1-ITM → mayor open interest)."""
from __future__ import annotations

# Núcleo compartido (raíz del repo). append = prioridad baja → no tapa 'settings' local.
import sys as _sys
from pathlib import Path as _Path
_ROOT = str(_Path(__file__).resolve().parent.parent.parent)
if _ROOT not in _sys.path:
    _sys.path.append(_ROOT)
import strategy_core  # noqa: E402

import settings  # noqa: E402
from core.models import Contract  # noqa: E402


class NoLiquidContract(Exception):
    pass


class ContractSelector:
    def __init__(self, min_oi: int | None = None, min_vol: int | None = None,
                 buckets: list | None = None):
        self.min_oi = settings.RISK["min_open_interest"] if min_oi is None else min_oi
        self.min_vol = settings.RISK["min_volume"] if min_vol is None else min_vol
        self.buckets = buckets or settings.SPREAD_BUCKETS

    def _max_spread(self, spot: float) -> float:
        # Misma compuerta de spread que el engine (núcleo compartido).
        return strategy_core.max_spread_for_price(spot, self.buckets)

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
            and strategy_core.passes_liquidity(c.open_interest, c.volume,
                                                self.min_oi, self.min_vol)
        ]
        if not liquid:
            raise NoLiquidContract(
                f"Ningún {side} con spread<=${max_spread:.2f}, OI>={self.min_oi}, "
                f"vol>={self.min_vol} (spot ${spot:.2f}).")

        # Orden idéntico al engine (Opción 1): (menor spread, cercanía ATM/1-ITM, mayor OI).
        mode = "itm" if strategy == "itm" else "atm"
        return min(liquid, key=lambda c: strategy_core.selection_key(
            c.spread, strategy_core.itm_depth(c.strike, spot, side),
            c.open_interest, mode=mode))

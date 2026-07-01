"""`LevelCalculator` — entry / stop / target sobre el SUBYACENTE, escalados por el ATR del ticker.

Stop = k·ATR; target = R·stop (R:R = 2 por defecto). Como usa el ATR del propio ticker, IWM/NVDA
salen con niveles más anchos que SPY/DIA de forma AUTOMÁTICA (más volátil → más recorrido). Si el
ATR falta, cae a una fracción del rango diario típico del perfil.
"""
from __future__ import annotations

import numpy as np

from ..domain import Action

_NAN = float("nan")


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and np.isfinite(x)


class LevelCalculator:
    STOP_ATR_MULT = 4.0     # stop = 4·ATR(1m) — más allá del ruido intradía
    RR = 2.0                # target = 2·stop  → R:R 2

    def levels(self, action: Action, price: float, atr: float, daily_range_pct: float = 1.0):
        """(entry, stop, target, risk_reward). Para NO_TRADE → (price, None, None, None)."""
        if not (_finite(price) and price > 0):
            return price, None, None, None
        a = atr if (_finite(atr) and atr > 0) else 0.05 * (daily_range_pct / 100.0) * price
        stop_dist = self.STOP_ATR_MULT * a
        target_dist = self.RR * stop_dist
        if action == Action.CALL:
            return price, price - stop_dist, price + target_dist, self.RR
        if action == Action.PUT:
            return price, price + stop_dist, price - target_dist, self.RR
        return price, None, None, None

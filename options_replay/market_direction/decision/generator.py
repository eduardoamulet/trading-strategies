"""`SignalGenerator` — convierte el score en la decisión final (`TradeSignal`).

  · Acción: score ≥ 75 → CALL · ≤ 25 → PUT · resto NO TRADE (+ el precio debe estar del lado
    correcto del VWAP).
  · Filtro de VOLATILIDAD (objetivo "gran movimiento"): si el ATR implica un rango diario < 60% del
    típico del ticker, el día viene chato → NO TRADE aunque la dirección sea clara.
  · Confianza: vía la costura `Calibrator` (heurístico hoy → empírico tras el backtest).
  · Niveles: `LevelCalculator` (entry/stop/target por ATR del ticker, R:R ≥ 2).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from ..calibration import make_calibrator
from ..domain import (Action, Confirmation, DirectionScore, FeatureSet, Strength,
                      TradeSignal)
from ..domain import ticker_profile as tp
from .levels import LevelCalculator

_RTH_MIN = 390


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and np.isfinite(x)


class SignalGenerator:
    CALL_THRESHOLD = 75.0
    PUT_THRESHOLD = 25.0
    MIN_VOL_RATIO = 0.60      # el día debe implicar ≥60% del rango diario típico del ticker

    def __init__(self, calibrator=None, levels: Optional[LevelCalculator] = None,
                 vol_filter: bool = True):
        self._cal = calibrator or make_calibrator()
        self._lev = levels or LevelCalculator()
        self._vol_filter = vol_filter      # False → no bloquea por vol (da CALL/PUT según score+VWAP)

    def _vol_ok(self, features: FeatureSet, profile):
        """(ok, nota). El día viene chato si el ATR implica un rango diario < MIN_VOL_RATIO×típico."""
        if profile is None or not (_finite(features.atr) and features.atr > 0
                                   and _finite(features.close) and features.close > 0):
            return True, ""
        implied_daily_pct = features.atr * math.sqrt(_RTH_MIN) / features.close * 100.0
        if implied_daily_pct < self.MIN_VOL_RATIO * profile.daily_range_pct:
            return False, (f"Volatilidad baja: rango implícito {implied_daily_pct:.2f}% < "
                           f"{self.MIN_VOL_RATIO:.0%} del típico ({profile.daily_range_pct:.2f}%)")
        return True, ""

    def generate(self, score: DirectionScore, features: FeatureSet,
                 confirmation: Optional[Confirmation] = None,
                 ticker: str = "", date: str = "") -> TradeSignal:
        prof = tp.get(ticker)
        trend = Strength.from_score(score.score).to_trend()
        confidence = self._cal.confidence(score, features, confirmation)
        reasons = list(score.reasons)
        entry_time = f"{features.hour:02d}:{features.minute:02d}"

        vol_ok, vol_note = self._vol_ok(features, prof) if self._vol_filter else (True, "")
        if not vol_ok:
            reasons.append(vol_note)

        if score.score >= self.CALL_THRESHOLD and vol_ok and features.above_vwap:
            action = Action.CALL
        elif score.score <= self.PUT_THRESHOLD and vol_ok and not features.above_vwap:
            action = Action.PUT
        else:
            action = Action.NO_TRADE

        if action == Action.NO_TRADE:
            return TradeSignal.no_trade(score.score, confidence, trend, reasons, ticker, date)

        entry, stop, target, rr = self._lev.levels(
            action, features.close, features.atr, prof.daily_range_pct if prof else 1.0)
        return TradeSignal(
            action=action, confidence=confidence, market_strength=score.score, trend=trend,
            score=score.score, entry_time=entry_time,
            entry_price=round(features.close, 2),
            stop=round(stop, 2) if stop is not None else None,
            target=round(target, 2) if target is not None else None,
            risk_reward=rr, reasons=reasons, ticker=ticker, date=date)

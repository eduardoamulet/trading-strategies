"""`RuleBasedModel` — score direccional 0–100 por reglas ponderadas (la implementación de hoy).

Cada regla aporta puntos CON SIGNO (alcista + / bajista −) al `feature_sum`; la confirmación
cross-asset se aplica en la dirección tentativa (via `Confirmation.factor`); el total se normaliza
al máximo posible y se mapea a 0–100 (50 neutral). Salida AUDITABLE: `DirectionScore` con las
razones y el desglose de contribuciones por regla.

Pesos (máx puntos por regla) — la suma define el 100/0:
  Above VWAP 15 · VWAP slope 8 · ORB 18 · EMA9>20 10 · EMA20>50 10 · Momentum 10 · MACD 7 ·
  RSI 6 · Estructura 10 · Niveles ayer 6 · Vol alto 8   → 108 · Confirmación 20   → 128 total.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from ..domain import Confirmation, DirectionScore, FeatureSet

_MAX_FEATURE = 108.0
_CONF_WEIGHT = 20.0
_MAX_TOTAL = _MAX_FEATURE + _CONF_WEIGHT


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and np.isfinite(x)


class RuleBasedModel:
    """Cumple el Protocol `DirectionModel`. Sin estado → reutilizable y testeable."""

    def predict(self, features: FeatureSet,
                confirmation: Optional[Confirmation] = None) -> DirectionScore:
        f = features
        c: dict = {}

        if _finite(f.vwap):
            c["Above VWAP"] = 15 if f.above_vwap else -15
        if _finite(f.vwap_slope):
            c["VWAP slope"] = 8 if f.vwap_slope > 0 else -8
        if f.or_breakout:
            c["ORB breakout"] = 18
        elif f.or_breakdown:
            c["ORB breakdown"] = -18
        if _finite(f.ema9) and _finite(f.ema20):
            c["EMA9>EMA20"] = 10 if f.ema9_gt_ema20 else -10
        if _finite(f.ema20) and _finite(f.ema50):
            c["EMA20>EMA50"] = 10 if f.ema20_gt_ema50 else -10
        if _finite(f.momentum):
            c["Momentum"] = 10 if f.momentum > 0 else -10
        if _finite(f.macd_hist):
            c["MACD hist"] = 7 if f.macd_hist > 0 else -7
        if _finite(f.rsi):
            if f.rsi > 55:
                c["RSI"] = 6
            elif f.rsi < 45:
                c["RSI"] = -6
        _struct = 0
        if f.higher_high:
            _struct += 6
        if f.higher_low:
            _struct += 4
        if f.lower_high:
            _struct -= 4
        if f.lower_low:
            _struct -= 6
        if _struct:
            c["Estructura"] = max(-10, min(10, _struct))
        if _finite(f.pos_vs_prev_high) and f.pos_vs_prev_high > 0:
            c["Sobre máx ayer"] = 6
        elif _finite(f.pos_vs_prev_low) and f.pos_vs_prev_low < 0:
            c["Bajo mín ayer"] = -6

        # Volumen alto = amplifica la dirección ya presente (no tiene signo propio).
        _partial = sum(c.values())
        if _finite(f.relative_volume) and f.relative_volume > 1.5 and _partial != 0:
            c["Vol alto (confirma)"] = 8 if _partial > 0 else -8

        feat_sum = sum(c.values())

        # Confirmación cross-asset: empuja en la dirección tentativa si acompaña, la frena si contradice.
        if confirmation is not None and feat_sum != 0:
            _sgn = 1.0 if feat_sum > 0 else -1.0
            _conf = (confirmation.factor - 0.5) * 2.0 * _CONF_WEIGHT * _sgn
            if abs(_conf) >= 0.5:
                c["Confirmación cross-asset"] = round(_conf, 1)

        total = sum(c.values())
        norm = max(-1.0, min(1.0, total / _MAX_TOTAL))
        score = round(50.0 + 50.0 * norm, 1)

        reasons = [f"{k} {v:+.0f}" for k, v in sorted(c.items(), key=lambda kv: -abs(kv[1]))]
        return DirectionScore(score=score, reasons=reasons, contributions=dict(c))

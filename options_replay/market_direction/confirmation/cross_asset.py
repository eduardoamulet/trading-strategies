"""Confirmación cross-asset — ¿el mercado acompaña la dirección evaluada?

SPY es el ANCLA macro. Al evaluar un activo, se lee la dirección PROPIA del confirmador (above/below
VWAP + alineación EMA + signo de momentum) y se compara con la dirección evaluada:
  · activo ≠ SPY  → confirmador = SPY (ponderado por la correlación del activo con SPY).
  · activo = SPY  → confirmadores = BREADTH de {QQQ, IWM} (cuántos índices acompañan).

La fuerza se escala por `TickerProfile.confidence_weight` (= corr con SPY): para ETFs SPY manda
fuerte; para acciones de baja correlación (AAPL 0.44) apenas pesa → manda la señal propia del activo.

CAUSAL: la sesión del confirmador se corta con `up_to(t)` → nunca mira más allá de t.
"""
from __future__ import annotations

import numpy as np

from ..domain import Confirmation, Trend
from ..domain import ticker_profile as tp
from ..indicators import FeatureBuilder

_FB = FeatureBuilder()

_ANCHOR = "SPY"
# SPY (el ancla) se confirma con el BREADTH de los otros índices core:
_BREADTH = {"SPY": ("QQQ", "IWM")}


def _confirmers(ticker: str) -> list:
    return list(_BREADTH.get(ticker, (_ANCHOR,)))


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and np.isfinite(x)


def read_direction(session, prev_day=None):
    """Dirección PROPIA de una sesión en t: (Trend, fuerza 0–1). Voto simple sobre VWAP/EMA/momentum.
    NEUTRAL si no hay señales suficientes o empatan."""
    if session is None or getattr(session, "empty", True):
        return Trend.NEUTRAL, 0.0
    fs = _FB.build(session, prev_day)
    votes = []
    if _finite(fs.vwap):
        votes.append(1.0 if fs.above_vwap else -1.0)
    if _finite(fs.ema9) and _finite(fs.ema20):
        votes.append(1.0 if fs.ema9_gt_ema20 else -1.0)
    if _finite(fs.momentum):
        votes.append(1.0 if fs.momentum > 0 else -1.0)
    if len(votes) < 2:
        return Trend.NEUTRAL, 0.0
    net = sum(votes) / len(votes)      # -1..1
    if net > 0:
        return Trend.BULLISH, abs(net)
    if net < 0:
        return Trend.BEARISH, abs(net)
    return Trend.NEUTRAL, 0.0


class CrossAssetConfirmation:
    """Produce un `Confirmation` para (ticker, dirección, fecha, t). Depende solo del provider."""

    def __init__(self, provider):
        self._provider = provider

    def confirm(self, ticker: str, trend: Trend, date: str, t: str) -> Confirmation:
        ticker = ticker.upper()
        if trend == Trend.NEUTRAL:
            return Confirmation.neutral(confirmer="—")

        confirmers = _confirmers(ticker)
        prof = tp.get(ticker)
        w = prof.confidence_weight if prof else 0.6   # peso por correlación con SPY

        reads = []
        for c in confirmers:
            sess = self._provider.session(c, date).up_to(t)
            prev = self._provider.previous_session(c, date)
            tr, stg = read_direction(sess, prev)
            reads.append((c, tr, stg))

        # voto neto RELATIVO a la dirección evaluada: + si el confirmador acompaña, − si contradice.
        votes = []
        for _c, tr, stg in reads:
            if tr == Trend.NEUTRAL:
                votes.append(0.0)
            elif tr == trend:
                votes.append(stg)
            else:
                votes.append(-stg)
        net = sum(votes) / len(votes) if votes else 0.0     # -1..1
        strength = min(1.0, abs(net) * w)

        if net > 0.05:
            agrees = True
        elif net < -0.05:
            agrees = False
        else:
            agrees = None

        label = "+".join(c for c, _, _ in reads)
        reasons = [f"{c}: {tr.value}" + (f" ({stg:.2f})" if tr != Trend.NEUTRAL else "")
                   for c, tr, stg in reads]
        reasons.append(f"peso corr {w:.2f}")
        return Confirmation(agrees=agrees, strength=round(strength, 3),
                            confirmer=label, reasons=reasons)

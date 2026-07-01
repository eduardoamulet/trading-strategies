"""`FeatureBuilder` — ensambla los ~50 indicadores en un `FeatureSet` a partir de una sesión CAUSAL.

Entrada: `session` (ya cortada con `up_to(t)` → solo velas ≤ t) y opcionalmente `prev_day` (para gap
y niveles de ayer). Salida: `FeatureSet` inmutable. Como solo lee velas ≤ t, es imposible mirar el
futuro: las features en `t` son idénticas exista o no data posterior en el buffer (ver test).
"""
from __future__ import annotations

import numpy as np

from ..domain import FeatureSet
from . import momentum as mom
from . import structure as struct
from . import trend
from . import volatility as vol
from . import volume as volm

_NAN = float("nan")


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and np.isfinite(x)


class FeatureBuilder:
    """Orquesta las familias de indicadores. Sin estado → reutilizable y testeable."""

    def build(self, session, prev_day=None) -> FeatureSet:
        if session is None or getattr(session, "empty", True):
            return FeatureSet()

        closes = session.closes
        highs = session.highs
        lows = session.lows
        volumes = session.volumes
        last = session.last
        ts = last.timestamp
        n = len(session)

        # ── volatilidad ──
        atr = vol.atr(highs, lows, closes)

        # ── EMAs ──
        ema9, ema20, ema50 = (trend.ema(closes, s) for s in (9, 20, 50))

        # ── VWAP ──
        vwap_v = trend.vwap(highs, lows, closes, volumes)
        vwap_dist = (last.close - vwap_v) / vwap_v if (_finite(vwap_v) and vwap_v != 0) else _NAN

        # ── volumen ──
        rel_v = volm.relative_volume(volumes)

        # ── opening range + estructura ──
        or5h, or5l = session.opening_range(5)
        or10h, or10l = session.opening_range(10)
        or15h, or15l = session.opening_range(15)
        orb, orbd, dist_or = struct.or_signals(last.close, or15h, or15l)
        hh, hl, lh, ll = struct.market_structure(highs, lows)

        # ── momentum ──
        mmt = mom.momentum(closes)
        macd_l, macd_s, macd_h, macd_sl = mom.macd(closes)

        # ── gap / niveles del día anterior ──
        gap_pct = gap_abs = prev_high = prev_low = pos_ph = pos_pl = _NAN
        if prev_day is not None and not getattr(prev_day, "empty", True):
            prev_close = float(prev_day.last.close)
            prev_high = float(np.max(prev_day.highs))
            prev_low = float(np.min(prev_day.lows))
            today_open = float(session.opens[0])
            if prev_close:
                gap_pct = (today_open - prev_close) / prev_close
                gap_abs = today_open - prev_close
            if prev_high:
                pos_ph = (last.close - prev_high) / prev_high
            if prev_low:
                pos_pl = (last.close - prev_low) / prev_low

        return FeatureSet(
            ticker=session.ticker, timestamp=str(ts), hour=int(ts.hour), minute=int(ts.minute),
            day_of_week=int(ts.weekday()), n_bars=int(n),
            open=last.open, high=last.high, low=last.low, close=last.close,
            body_size=last.body_size, upper_wick=last.upper_wick, lower_wick=last.lower_wick,
            true_range=vol.true_range(highs, lows, closes), atr=atr, volatility=vol.realized_vol(closes),
            ema9=ema9, ema20=ema20, ema50=ema50,
            ema9_slope=trend.ema_slope(closes, 9), ema20_slope=trend.ema_slope(closes, 20),
            ema50_slope=trend.ema_slope(closes, 50),
            ema9_gt_ema20=bool(_finite(ema9) and _finite(ema20) and ema9 > ema20),
            ema20_gt_ema50=bool(_finite(ema20) and _finite(ema50) and ema20 > ema50),
            vwap=vwap_v, vwap_slope=trend.vwap_slope(highs, lows, closes, volumes), vwap_distance=vwap_dist,
            above_vwap=bool(_finite(vwap_v) and last.close > vwap_v),
            volume=last.volume, avg_volume=volm.avg_volume(volumes), relative_volume=rel_v,
            volume_spike=volm.volume_spike(volumes),
            or5_high=or5h, or5_low=or5l, or10_high=or10h, or10_low=or10l, or15_high=or15h, or15_low=or15l,
            or_breakout=orb, or_breakdown=orbd, distance_to_or=dist_or,
            higher_high=hh, higher_low=hl, lower_high=lh, lower_low=ll,
            gap_pct=gap_pct, gap_vs_prev_close=gap_abs, prev_high=prev_high, prev_low=prev_low,
            pos_vs_prev_high=pos_ph, pos_vs_prev_low=pos_pl,
            momentum=mmt, roc=mom.roc(closes), rsi=mom.rsi(closes),
            macd=macd_l, macd_signal=macd_s, macd_hist=macd_h, macd_slope=macd_sl,
            momentum_positive=bool(_finite(mmt) and mmt > 0),
            volume_expanding=bool(_finite(rel_v) and rel_v > 1.0),
        )

"""`FeatureSet` — la foto de TODOS los indicadores calculados en un minuto `t` (solo con datos ≤ t).

Es EL CONTRATO entre la capa de indicadores y la capa de modelo: tanto el `RuleBasedModel` de hoy
como un `XGBoostModel` futuro consumen exactamente este objeto. Por eso los ~50 features se calculan
una sola vez (capa `indicators`) y sirven igual para reglas o para ML.

Es un dataclass `frozen` (inmutable) con `slots` para que sea liviano y no se mute por accidente.
Todos los campos numéricos arrancan en `nan` (sin datos suficientes) y los booleanos en `False`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_NAN = float("nan")


@dataclass(frozen=True, slots=True)
class FeatureSet:
    # ── Meta / tiempo ─────────────────────────────────────────────────────────
    ticker: str = ""
    timestamp: str = ""          # ISO del minuto evaluado
    hour: int = 0
    minute: int = 0
    day_of_week: int = 0         # 0 = lunes … 4 = viernes
    n_bars: int = 0              # cuántas velas hay hasta t (calidad del cómputo)

    # ── Vela actual ──────────────────────────────────────────────────────────
    open: float = _NAN
    high: float = _NAN
    low: float = _NAN
    close: float = _NAN
    body_size: float = _NAN
    upper_wick: float = _NAN
    lower_wick: float = _NAN

    # ── Volatilidad ──────────────────────────────────────────────────────────
    true_range: float = _NAN
    atr: float = _NAN            # ATR(14) Wilder
    volatility: float = _NAN     # desvío de retornos intradía (anualizado o crudo, ver indicador)

    # ── Tendencia: EMAs ──────────────────────────────────────────────────────
    ema9: float = _NAN
    ema20: float = _NAN
    ema50: float = _NAN
    ema9_slope: float = _NAN     # pendiente reciente (Δ por barra)
    ema20_slope: float = _NAN
    ema50_slope: float = _NAN
    ema9_gt_ema20: bool = False
    ema20_gt_ema50: bool = False

    # ── VWAP ─────────────────────────────────────────────────────────────────
    vwap: float = _NAN
    vwap_slope: float = _NAN
    vwap_distance: float = _NAN  # (close − vwap) / vwap  (signo = lado)
    above_vwap: bool = False

    # ── Volumen ──────────────────────────────────────────────────────────────
    volume: float = _NAN
    avg_volume: float = _NAN
    relative_volume: float = _NAN  # volume / avg_volume
    volume_spike: bool = False     # relative_volume > umbral

    # ── Opening Range (5/10/15 min) ──────────────────────────────────────────
    or5_high: float = _NAN
    or5_low: float = _NAN
    or10_high: float = _NAN
    or10_low: float = _NAN
    or15_high: float = _NAN
    or15_low: float = _NAN
    or_breakout: bool = False    # close > OR-high (usa el OR de referencia, ver indicador)
    or_breakdown: bool = False   # close < OR-low
    distance_to_or: float = _NAN  # distancia relativa al borde del OR de referencia

    # ── Estructura de mercado ────────────────────────────────────────────────
    higher_high: bool = False
    higher_low: bool = False
    lower_high: bool = False
    lower_low: bool = False

    # ── Gaps / niveles del día anterior ──────────────────────────────────────
    gap_pct: float = _NAN              # (open_hoy − close_ayer) / close_ayer
    gap_vs_prev_close: float = _NAN    # open_hoy − close_ayer (absoluto)
    prev_high: float = _NAN
    prev_low: float = _NAN
    pos_vs_prev_high: float = _NAN     # (close − prev_high) / prev_high
    pos_vs_prev_low: float = _NAN      # (close − prev_low) / prev_low

    # ── Momentum ─────────────────────────────────────────────────────────────
    momentum: float = _NAN            # close − close[n atrás]
    roc: float = _NAN                 # rate of change %
    rsi: float = _NAN                 # RSI(14)
    macd: float = _NAN
    macd_signal: float = _NAN
    macd_hist: float = _NAN
    macd_slope: float = _NAN          # pendiente del histograma/línea MACD

    # ── Direccionalidad agregada (helpers booleanos derivados) ────────────────
    momentum_positive: bool = False
    volume_expanding: bool = False

    def is_complete(self) -> bool:
        """True si hay velas suficientes para confiar en el cómputo (heurística mínima)."""
        return self.n_bars >= 2 and self.close == self.close  # close no-NaN

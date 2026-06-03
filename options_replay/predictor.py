"""Bullish/bearish probability predictor para los primeros 5 minutos después
de la apertura del mercado.

Approach: k-NN sobre sesiones históricas cacheadas. Para una fecha objetivo:
1. Extrae features del premarket (04:00-09:29 ET): gap %, slope, volumen, vol, posición en rango.
2. Para cada sesión histórica cacheada, extrae las mismas features y la
   OUTCOME real (precio a 09:35 > precio a 09:30 → bullish=1, sino =0).
3. Normaliza con z-score y encuentra las k sesiones más cercanas por distancia
   euclidiana ponderada.
4. Probabilidad = % de bullish entre las k vecinas.

Solo usa data DISPONIBLE ANTES DE LA APERTURA — no mira nada ≥ 09:30 de la
fecha objetivo. Eso lo hace válido como predictor "sin look-ahead bias".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

TZ = "America/New_York"
HERE = Path(__file__).parent
CONFIG_PATH = HERE / "predictor_config.json"

# Keys de features que entran al k-NN (orden estable).
FEATURE_KEYS = ["gap_pct", "trend_slope", "pm_volume", "volatility", "position_in_range"]


# ============================================================================
# Config
# ============================================================================

def _default_config() -> dict:
    """Cada range incluye su propio 'rule' con mode/call/put/roi.
    Las fórmulas son strings que soportan 'p', '100-p', '0', '100'."""
    return {
        "classification_ranges": [
            {"min": 0,  "max": 20,  "label": "MUY BAJISTA", "color": "#b71c1c",
             "box_bg": "#ffcdd2", "box_border": "#b71c1c",
             "rule": {"mode": "put_only",  "call_formula": "0",     "put_formula": "100",   "roi": 30}},
            {"min": 21, "max": 40,  "label": "BAJISTA",     "color": "#ef5350",
             "box_bg": "#ffebee", "box_border": "#ef5350",
             "rule": {"mode": "both",      "call_formula": "p",     "put_formula": "100-p", "roi": 10}},
            {"min": 41, "max": 60,  "label": "NEUTRAL",     "color": "#42a5f5",
             "box_bg": "#e3f2fd", "box_border": "#42a5f5",
             "rule": {"mode": "both",      "call_formula": "p",     "put_formula": "100-p", "roi": 30}},
            {"min": 61, "max": 80,  "label": "ALCISTA",     "color": "#66bb6a",
             "box_bg": "#e8f5e9", "box_border": "#66bb6a",
             "rule": {"mode": "both",      "call_formula": "p",     "put_formula": "100-p", "roi": 10}},
            {"min": 81, "max": 100, "label": "MUY ALCISTA", "color": "#2e7d32",
             "box_bg": "#c8e6c9", "box_border": "#2e7d32",
             "rule": {"mode": "call_only", "call_formula": "100",   "put_formula": "0",     "roi": 30}},
        ],
        "k_neighbors": 20,
        "min_history_size": 10,
        "feature_weights": {k: 1.0 for k in FEATURE_KEYS},
    }


def load_config() -> dict:
    """Carga el config desde predictor_config.json. Si no existe, defaults."""
    if not CONFIG_PATH.exists():
        return _default_config()
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = json.load(f)
        # Merge con defaults para tolerar configs incompletos.
        defaults = _default_config()
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return _default_config()


# ============================================================================
# Result type
# ============================================================================

@dataclass
class Prediction:
    probability: int      # 0-100
    label: str            # "BAJISTA", "ALCISTA", etc.
    color: str            # hex color
    sample_size: int      # cuántas sesiones históricas se usaron
    features: dict = field(default_factory=dict)  # features de la fecha objetivo (debug)
    reason: str = ""      # explicación si no se pudo predecir


# ============================================================================
# Feature extraction
# ============================================================================

def extract_premarket_features(under_df: pd.DataFrame, target_date: str) -> Optional[dict]:
    """Extrae features del premarket (timestamps < 09:30 ET del target_date).
    Devuelve None si no hay suficientes bars de premarket (mínimo 5)."""
    if under_df is None or under_df.empty:
        return None

    open_ts = pd.Timestamp(f"{target_date} 09:30:00", tz=TZ)
    premarket = under_df[under_df["timestamp"] < open_ts].copy()

    if len(premarket) < 5:
        return None

    pm_first_open = float(premarket.iloc[0]["open"])
    pm_last_close = float(premarket.iloc[-1]["close"])
    pm_high = float(premarket["high"].max())
    pm_low = float(premarket["low"].min())
    n = len(premarket)

    # Gap = movimiento del premarket entre el primer bar y el último.
    gap_pct = ((pm_last_close - pm_first_open) / pm_first_open * 100) if pm_first_open else 0.0

    # Trend slope = pendiente de regresión lineal de closes (% por minuto).
    closes = premarket["close"].values.astype(float)
    x = np.arange(n, dtype=float)
    try:
        slope, _ = np.polyfit(x, closes, 1)
    except (np.linalg.LinAlgError, ValueError):
        slope = 0.0
    trend_slope = (slope / pm_last_close * 100) if pm_last_close else 0.0

    # Volumen total del premarket (raw — se normaliza vs historia después).
    pm_volume = float(premarket["volume"].sum())

    # Volatilidad = std de retornos % de bar a bar.
    if n > 1:
        returns = np.diff(closes) / closes[:-1]
        volatility = float(np.std(returns) * 100)
    else:
        volatility = 0.0

    # Posición del último close dentro del rango [low, high] del premarket.
    if pm_high > pm_low:
        position_in_range = (pm_last_close - pm_low) / (pm_high - pm_low)
    else:
        position_in_range = 0.5

    return {
        "gap_pct": gap_pct,
        "trend_slope": trend_slope,
        "pm_volume": pm_volume,
        "volatility": volatility,
        "position_in_range": position_in_range,
        "pm_first_open": pm_first_open,
        "pm_last_close": pm_last_close,
        "pm_bars_count": n,
    }


def compute_outcome(under_df: pd.DataFrame, target_date: str) -> Optional[int]:
    """Devuelve 1 si el spot a 09:35 > spot a 09:30 (bullish), 0 sino.
    None si no hay suficiente data."""
    if under_df is None or under_df.empty:
        return None

    open_ts = pd.Timestamp(f"{target_date} 09:30:00", tz=TZ)
    five_min_ts = pd.Timestamp(f"{target_date} 09:35:00", tz=TZ)

    after_open = under_df[
        (under_df["timestamp"] >= open_ts)
        & (under_df["timestamp"] <= five_min_ts)
    ]
    if len(after_open) < 2:
        return None

    open_price = float(after_open.iloc[0]["open"])
    close_5min = float(after_open.iloc[-1]["close"])
    return 1 if close_5min > open_price else 0


# ============================================================================
# k-NN prediction
# ============================================================================

def _classify(probability: int, config: dict) -> tuple[str, str]:
    """Devuelve (label, color) según los rangos del config."""
    for r in config["classification_ranges"]:
        if r["min"] <= probability <= r["max"]:
            return r["label"], r["color"]
    return "?", "#888888"


def _neutral(config: dict, sample_size: int, reason: str = "",
             features: Optional[dict] = None) -> Prediction:
    label, color = _classify(50, config)
    return Prediction(
        probability=50, label=label, color=color,
        sample_size=sample_size, features=features or {}, reason=reason,
    )


def predict_bullish_probability(
    ticker: str,
    target_date: str,
    downloader,
    config: Optional[dict] = None,
) -> Prediction:
    """Predice probabilidad alcista 0-100 para los primeros 5min después de
    la apertura del mercado en (ticker, target_date)."""
    if config is None:
        config = load_config()

    k = int(config.get("k_neighbors", 20))
    min_history = int(config.get("min_history_size", 10))
    weights = config.get("feature_weights", {})

    # Features de la fecha objetivo. Usa cache local (no descarga).
    try:
        target_under = downloader.underlying(ticker, target_date)
    except Exception as e:
        return _neutral(config, sample_size=0, reason=f"Error cargando data: {e}")

    target_features = extract_premarket_features(target_under, target_date)
    if target_features is None:
        return _neutral(
            config, sample_size=0,
            reason=f"Sin data de premarket para {ticker} {target_date} (≥5 bars requeridos antes de 09:30)",
        )

    # Scan archivos históricos del ticker en cache (solo fechas < target).
    data_dir = downloader.data_dir / "underlying"
    historical_files = sorted([
        p for p in data_dir.glob(f"{ticker}_*.parquet")
        if p.stem.split("_", 1)[1] < target_date
    ])

    history: list[tuple[dict, int]] = []
    for p in historical_files:
        d = p.stem.split("_", 1)[1]
        try:
            under = pd.read_parquet(p)
            feats = extract_premarket_features(under, d)
            out = compute_outcome(under, d)
            if feats is not None and out is not None:
                history.append((feats, out))
        except Exception:
            continue

    if len(history) < min_history:
        return _neutral(
            config, sample_size=len(history),
            reason=f"Histórico insuficiente ({len(history)}/{min_history} sesiones válidas anteriores)",
            features=target_features,
        )

    # Matrix de features y normalización z-score con stats de la historia.
    feat_matrix = np.array([[h[0][fk] for fk in FEATURE_KEYS] for h in history])
    target_vec = np.array([target_features[fk] for fk in FEATURE_KEYS])

    means = feat_matrix.mean(axis=0)
    stds = feat_matrix.std(axis=0)
    stds = np.where(stds == 0, 1.0, stds)  # evita /0 si feature constante

    feat_norm = (feat_matrix - means) / stds
    target_norm = (target_vec - means) / stds

    # Aplicar pesos por feature (del config).
    w_array = np.array([weights.get(fk, 1.0) for fk in FEATURE_KEYS])
    feat_weighted = feat_norm * w_array
    target_weighted = target_norm * w_array

    # Distancia euclidiana ponderada.
    diffs = feat_weighted - target_weighted
    distances = np.sqrt((diffs ** 2).sum(axis=1))

    # Top-k vecinos más cercanos.
    k_actual = min(k, len(history))
    nearest_idx = np.argsort(distances)[:k_actual]
    outcomes = [history[i][1] for i in nearest_idx]

    bullish_count = sum(outcomes)
    probability = int(round(100 * bullish_count / len(outcomes)))
    probability = max(0, min(100, probability))

    label, color = _classify(probability, config)
    # Agregar features de los vecinos para debug.
    target_features["_neighbors_outcomes"] = outcomes
    target_features["_neighbors_distances"] = [float(d) for d in distances[nearest_idx]]
    return Prediction(
        probability=probability, label=label, color=color,
        sample_size=len(outcomes), features=target_features,
    )


# ============================================================================
# Trading parameter adjustment
# ============================================================================

def _evaluate_formula(formula: str, p: int) -> int:
    """Evalúa una formula de allocation. Soporta exactamente 4 expresiones:
    '0', '100', 'p', '100-p'. Pure (sin eval) para seguridad."""
    f = (formula or "").strip().replace(" ", "").lower()
    if f == "p":
        return int(p)
    if f == "100-p":
        return int(100 - p)
    # Intentar parsear como número literal
    try:
        return int(f)
    except ValueError:
        return 0


def adjust_trading_parameters(probability: int, config: Optional[dict] = None) -> dict:
    """Mapea probabilidad → parámetros de trading según la `rule` del rango
    en el que cae la probabilidad. Cada classification_range tiene su propio
    {mode, call_formula, put_formula, roi}.

    Devuelve dict con:
      - mode: "put_only" | "both" | "call_only"
      - call_allocation: int 0-100
      - put_allocation: int 0-100
      - roi_threshold: int (%)
    """
    if config is None:
        config = load_config()

    for r in config.get("classification_ranges", []):
        if r["min"] <= probability <= r["max"]:
            rule = r.get("rule", {})
            call_alloc = _evaluate_formula(rule.get("call_formula", "p"), probability)
            put_alloc = _evaluate_formula(rule.get("put_formula", "100-p"), probability)
            return {
                "mode": rule.get("mode", "both"),
                "call_allocation": call_alloc,
                "put_allocation": put_alloc,
                "roi_threshold": int(rule.get("roi", 30)),
            }

    # Fallback: si la probabilidad cae fuera de todos los rangos (no debería pasar
    # si los rangos cubren 0-100), volver a 50/50 con ROI 30.
    return {"mode": "both", "call_allocation": 50, "put_allocation": 50, "roi_threshold": 30}

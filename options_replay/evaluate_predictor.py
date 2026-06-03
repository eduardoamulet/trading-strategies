"""evaluate_predictor.py — Validación walk-forward del predictor bullish.

Mide si el k-NN actual (el que usa el app) realmente supera baselines triviales,
usando SOLO el cache local (no toca Polygon). Es el paso #1 de cualquier mejora:
sin esto, "no funciona bien" es anécdota, no dato.

Metodología:
- Walk-forward estricto: para predecir el día i se usa SOLO la historia < i
  (idéntico a como el app evita look-ahead). No hay leakage temporal.
- Target: close[09:35] > open[09:30]  → bullish=1, sino 0  (igual que el app).
- Modelo evaluado: k-NN con z-score + pesos del config (replica fiel de
  predict_bullish_probability), computado in-memory para correr en segundos.
- Baselines:
    * const_0.5      → "sin información" (Brier siempre 0.25)
    * base_rate      → tasa histórica de días alcistas (walk-forward) — LA a batir
    * gap_sign       → tasa base condicional al signo del gap premarket
- Métricas: Brier, LogLoss, AUC, Accuracy, Brier Skill Score (vs base_rate),
  + reliability diagram (calibración por deciles).

Uso (desde options_replay/):
    py evaluate_predictor.py SPY
    py evaluate_predictor.py SPY QQQ IWM NDX
    py evaluate_predictor.py            # default: SPY QQQ IWM
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from predictor import (  # noqa: E402
    FEATURE_KEYS,
    compute_outcome,
    extract_premarket_features,
    load_config,
)

DATA_DIR = HERE / "data"


def _print(*a, **k):
    k.setdefault("flush", True)
    print(*a, **k)


# ============================================================================
# Métricas (numpy puro — sin dependencia de sklearn)
# ============================================================================

def brier_score(p: np.ndarray, y: np.ndarray) -> float:
    """Mean squared error de probabilidades. 0 = perfecto, 0.25 = const 0.5."""
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray, eps: float = 1e-15) -> float:
    """Cross-entropy. Penaliza fuerte la confianza mal puesta."""
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def accuracy(p: np.ndarray, y: np.ndarray, thr: float = 0.5) -> float:
    return float(np.mean((p >= thr).astype(int) == y))


def _rankdata(a: np.ndarray) -> np.ndarray:
    """Ranks con promedio en empates (equivalente a scipy.stats.rankdata)."""
    a = np.asarray(a, dtype=float)
    sorter = np.argsort(a, kind="mergesort")
    inv = np.empty(len(a), dtype=int)
    inv[sorter] = np.arange(len(a))
    a_sorted = a[sorter]
    obs = np.r_[True, a_sorted[1:] != a_sorted[:-1]]
    dense = obs.cumsum()[inv]
    count = np.r_[np.nonzero(obs)[0], len(obs)]
    return 0.5 * (count[dense] + count[dense - 1] + 1)


def auc_score(p: np.ndarray, y: np.ndarray) -> float:
    """AUC vía estadístico de Mann-Whitney U (rank-based). NaN si una sola clase."""
    y = np.asarray(y)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = _rankdata(p)
    sum_ranks_pos = ranks[y == 1].sum()
    return float((sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def brier_skill_score(bs_model: float, bs_ref: float) -> float:
    """1 - BS_model/BS_ref. >0 → mejor que la referencia; <=0 → no aporta."""
    if bs_ref <= 0:
        return float("nan")
    return 1.0 - bs_model / bs_ref


# ============================================================================
# Carga de features in-memory (una sola pasada por el cache)
# ============================================================================

def load_ticker_dataset(ticker: str) -> list[dict]:
    """Lee todos los parquet de underlying del ticker, extrae (fecha, features,
    outcome) por día. Devuelve lista ordenada por fecha, solo días válidos
    (con premarket >=5 bars y outcome computable)."""
    under_dir = DATA_DIR / "underlying"
    files = sorted(under_dir.glob(f"{ticker}_*.parquet"))
    rows: list[dict] = []
    for p in files:
        date_str = p.stem.split("_", 1)[1]
        try:
            under = pd.read_parquet(p)
        except Exception:
            continue
        feats = extract_premarket_features(under, date_str)
        outcome = compute_outcome(under, date_str)
        if feats is None or outcome is None:
            continue
        rows.append({
            "date": date_str,
            "features": feats,
            "outcome": int(outcome),
            "gap_pct": feats["gap_pct"],
        })
    return rows


# ============================================================================
# k-NN in-memory — réplica fiel de predict_bullish_probability
# ============================================================================

def knn_probability(
    target_feats: dict,
    history: list[dict],
    config: dict,
) -> float | None:
    """Probabilidad bullish del k-NN usando SOLO `history` (días < target).
    Replica la matemática de predictor.predict_bullish_probability:
    z-score con stats de la historia, pesos del config, distancia euclidiana,
    voto no ponderado entre los k vecinos. Devuelve fracción [0,1] o None si
    no hay suficiente historia."""
    k = int(config.get("k_neighbors", 20))
    min_history = int(config.get("min_history_size", 10))
    weights = config.get("feature_weights", {})

    if len(history) < min_history:
        return None

    feat_matrix = np.array([[h["features"][fk] for fk in FEATURE_KEYS] for h in history])
    target_vec = np.array([target_feats[fk] for fk in FEATURE_KEYS])

    means = feat_matrix.mean(axis=0)
    stds = feat_matrix.std(axis=0)
    stds = np.where(stds == 0, 1.0, stds)

    feat_norm = (feat_matrix - means) / stds
    target_norm = (target_vec - means) / stds

    w = np.array([weights.get(fk, 1.0) for fk in FEATURE_KEYS])
    diffs = (feat_norm - target_norm) * w
    distances = np.sqrt((diffs ** 2).sum(axis=1))

    k_actual = min(k, len(history))
    nearest_idx = np.argsort(distances)[:k_actual]
    outcomes = np.array([history[i]["outcome"] for i in nearest_idx])
    return float(outcomes.mean())


# ============================================================================
# Walk-forward evaluation
# ============================================================================

def evaluate_ticker(ticker: str, config: dict) -> dict | None:
    dataset = load_ticker_dataset(ticker)
    min_history = int(config.get("min_history_size", 10))

    if len(dataset) < min_history + 20:
        _print(f"  [{ticker}] Datos insuficientes: {len(dataset)} días válidos "
               f"(se necesitan >= {min_history + 20}). Skip.")
        return None

    records = []  # por día testeado
    for i in range(min_history, len(dataset)):
        target = dataset[i]
        history = dataset[:i]  # estrictamente anterior → walk-forward sin leakage

        p_model = knn_probability(target["features"], history, config)
        if p_model is None:
            continue

        y = target["outcome"]
        gap = target["gap_pct"]

        # --- baselines walk-forward (solo historia) ---
        hist_outcomes = np.array([h["outcome"] for h in history])
        hist_gaps = np.array([h["gap_pct"] for h in history])

        p_const = 0.5
        p_baserate = float(hist_outcomes.mean())

        # gap-sign: tasa base condicional al signo del gap del día objetivo
        if gap > 0:
            mask = hist_gaps > 0
        else:
            mask = hist_gaps <= 0
        if mask.sum() >= 3:
            p_gapsign = float(hist_outcomes[mask].mean())
        else:
            p_gapsign = p_baserate  # fallback si no hay suficientes con ese signo

        records.append({
            "date": target["date"],
            "y": y,
            "gap_pct": gap,
            "p_model": p_model,
            "p_const": p_const,
            "p_baserate": p_baserate,
            "p_gapsign": p_gapsign,
        })

    if not records:
        _print(f"  [{ticker}] Sin registros evaluables. Skip.")
        return None

    df = pd.DataFrame(records)
    y = df["y"].values.astype(float)

    metrics = {}
    for name, col in [
        ("MODELO (k-NN)", "p_model"),
        ("base const 0.5", "p_const"),
        ("base rate",      "p_baserate"),
        ("gap sign",       "p_gapsign"),
    ]:
        p = df[col].values.astype(float)
        metrics[name] = {
            "brier": brier_score(p, y),
            "logloss": log_loss(p, y),
            "auc": auc_score(p, y),
            "acc": accuracy(p, y),
        }

    bss_vs_baserate = brier_skill_score(
        metrics["MODELO (k-NN)"]["brier"], metrics["base rate"]["brier"]
    )

    return {
        "ticker": ticker,
        "n_test": len(df),
        "n_total_days": len(dataset),
        "base_rate_overall": float(y.mean()),
        "metrics": metrics,
        "bss_vs_baserate": bss_vs_baserate,
        "df": df,
    }


# ============================================================================
# Calibración (reliability diagram en texto)
# ============================================================================

def calibration_table(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> str:
    """Tabla de calibración: bin de prob predicha vs frecuencia real observada.
    Un modelo bien calibrado tiene pred ~ obs en cada bin."""
    bins = np.linspace(0, 1, n_bins + 1)
    lines = [f"  {'rango pred':<14} {'n':>4} {'pred prom':>10} {'real obs':>10} {'gap':>8}"]
    lines.append("  " + "-" * 50)
    for b in range(n_bins):
        lo, hi = bins[b], bins[b + 1]
        if b == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        n = int(mask.sum())
        if n == 0:
            continue
        pred_mean = float(p[mask].mean())
        obs_mean = float(y[mask].mean())
        gap = obs_mean - pred_mean
        lines.append(
            f"  [{lo:.1f}, {hi:.1f})    {n:>4} {pred_mean:>10.3f} {obs_mean:>10.3f} {gap:>+8.3f}"
        )
    return "\n".join(lines)


# ============================================================================
# Report
# ============================================================================

def print_report(result: dict) -> None:
    t = result["ticker"]
    _print("=" * 70)
    _print(f"TICKER: {t}")
    _print(f"  Días válidos: {result['n_total_days']}  |  Días testeados (walk-fwd): {result['n_test']}")
    _print(f"  Tasa base alcista (todo el set): {result['base_rate_overall']:.1%}")
    _print("")
    _print(f"  {'Predictor':<16} {'Brier':>8} {'LogLoss':>9} {'AUC':>7} {'Acc':>7}")
    _print("  " + "-" * 52)
    for name, m in result["metrics"].items():
        auc_str = f"{m['auc']:.3f}" if not np.isnan(m["auc"]) else "  n/a"
        _print(f"  {name:<16} {m['brier']:>8.4f} {m['logloss']:>9.4f} {auc_str:>7} {m['acc']:>7.1%}")
    _print("")

    bss = result["bss_vs_baserate"]
    verdict = (
        "SUPERA al base rate (hay edge)" if bss > 0.01 else
        "EMPATA con base rate (sin edge real)" if bss > -0.01 else
        "PEOR que base rate (las features confunden)"
    )
    _print(f"  Brier Skill Score (modelo vs base rate): {bss:+.4f}")
    _print(f"  -> {verdict}")
    _print("")
    _print("  Calibración del MODELO (pred vs real por bin):")
    df = result["df"]
    _print(calibration_table(df["p_model"].values, df["y"].values.astype(float)))
    _print("")


def print_aggregate(results: list[dict]) -> None:
    if len(results) < 2:
        return
    _print("=" * 70)
    _print("RESUMEN AGREGADO (todos los tickers)")
    _print(f"  {'Ticker':<8} {'n_test':>7} {'BrierMod':>9} {'BrierBase':>10} {'BSS':>8} {'AUC':>7}")
    _print("  " + "-" * 54)
    for r in results:
        m = r["metrics"]
        auc = m["MODELO (k-NN)"]["auc"]
        auc_str = f"{auc:.3f}" if not np.isnan(auc) else "n/a"
        _print(f"  {r['ticker']:<8} {r['n_test']:>7} "
               f"{m['MODELO (k-NN)']['brier']:>9.4f} {m['base rate']['brier']:>10.4f} "
               f"{r['bss_vs_baserate']:>+8.4f} {auc_str:>7}")
    _print("")
    avg_bss = np.mean([r["bss_vs_baserate"] for r in results])
    avg_auc = np.nanmean([r["metrics"]["MODELO (k-NN)"]["auc"] for r in results])
    _print(f"  BSS promedio: {avg_bss:+.4f}   |   AUC promedio: {avg_auc:.3f}")
    _print("")
    _print("  INTERPRETACION:")
    _print("    AUC ~0.50 y BSS ~0  -> el modelo no tiene edge; el problema esta")
    _print("                           en las FEATURES (o el horizonte de 5min es")
    _print("                           intrinsecamente ruidoso). Siguiente paso:")
    _print("                           log-vol, gap/ATR, features cross-asset.")
    _print("    AUC >0.54 y BSS >0  -> hay edge real; vale la pena calibrar y")
    _print("                           agregar el ensemble logistic + k-NN.")
    _print("")


def main(argv: list[str]) -> int:
    tickers = [a.upper().strip() for a in argv[1:]] or ["SPY", "QQQ", "IWM"]
    config = load_config()

    _print(f"Config: k={config.get('k_neighbors')} "
           f"min_history={config.get('min_history_size')} "
           f"weights={config.get('feature_weights')}")
    _print(f"Target: close[09:35] > open[09:30]  (bullish=1)")
    _print(f"Validación: walk-forward estricto (historia < día testeado)")
    _print("")

    results = []
    for t in tickers:
        res = evaluate_ticker(t, config)
        if res is not None:
            print_report(res)
            results.append(res)

    print_aggregate(results)

    # Dump CSV por ticker para inspección/plot posterior.
    out_dir = HERE / "eval_output"
    out_dir.mkdir(exist_ok=True)
    for r in results:
        path = out_dir / f"eval_{r['ticker']}.csv"
        r["df"].to_csv(path, index=False)
    if results:
        _print(f"CSVs por dia guardados en: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

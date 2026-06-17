"""Session statistics for a single ReplayResult."""
from __future__ import annotations

from engine import ReplayResult


def backtest_risk_metrics(rows: list) -> dict:
    """Métricas de RIESGO/COLA de un backtest multi-día — lo que el win rate y el total
    esconden (crítico para martingalas, donde muchas ganancias chicas tapan días de −100%).

    `rows`: lista de dicts {'fecha', 'gain' ($), 'invest' ($), 'roi' (frac opcional)}. Se
    ordena por 'fecha' (cronológico) para la curva de equity y el drawdown. Pura (solo numpy).

    Devuelve: profit_factor, expectancy ($/día), peor/mejor día, # días catastróficos (ROI
    ≤ −90%), racha perdedora máx, max drawdown ($ y ×apuesta), Sortino/Sharpe (diarios),
    percentiles de ROI, y las series equity_curve / rois para graficar."""
    import numpy as np
    rows = [r for r in (rows or []) if r.get("invest")]
    rows = sorted(rows, key=lambda r: str(r.get("fecha", "")))
    n = len(rows)
    if n == 0:
        return {"n": 0}
    gains = np.array([float(r["gain"]) for r in rows], dtype=float)
    invs = np.array([float(r["invest"]) for r in rows], dtype=float)
    rois = np.array([float(r["roi"]) if r.get("roi") is not None
                     else (float(r["gain"]) / float(r["invest"]) if r["invest"] else 0.0)
                     for r in rows], dtype=float)

    wins = float(gains[gains > 0].sum())
    losses = float(-gains[gains < 0].sum())          # positivo
    pf = (wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0)
    total_gain = float(gains.sum())
    avg_inv = float(invs.mean())
    expectancy = total_gain / n

    # Drawdown sobre la P&L ACUMULADA en orden cronológico (peak-to-trough).
    equity = np.cumsum(gains)
    peak = np.maximum.accumulate(equity)
    max_dd = float((peak - equity).max())
    dd_x = (max_dd / avg_inv) if avg_inv else 0.0

    # Racha perdedora máxima (días consecutivos con gain < 0).
    streak = mx_streak = 0
    for g in gains:
        if g < 0:
            streak += 1
            mx_streak = max(mx_streak, streak)
        else:
            streak = 0

    # Sortino/Sharpe DIARIOS (no anualizados — la serie tiene huecos de días sin trade).
    mean_roi = float(rois.mean())
    downside = rois[rois < 0]
    dd_dev = float(np.sqrt((downside ** 2).mean())) if downside.size else 0.0
    sortino = (mean_roi / dd_dev) if dd_dev > 0 else (float("inf") if mean_roi > 0 else 0.0)
    std = float(rois.std())
    sharpe = (mean_roi / std) if std > 0 else 0.0

    worst_i = int(rois.argmin())
    best_i = int(rois.argmax())
    pcts = {p: float(np.percentile(rois, p)) for p in (5, 25, 50, 75, 95)}

    return {
        "n": n,
        "total_gain": total_gain,
        "avg_invest": avg_inv,
        "profit_factor": pf,
        "expectancy": expectancy,
        "wins_sum": wins,
        "losses_sum": losses,
        "max_drawdown": max_dd,
        "max_drawdown_x": dd_x,
        "max_losing_streak": int(mx_streak),
        "sortino": sortino,
        "sharpe": sharpe,
        "worst_roi": float(rois[worst_i]),
        "worst_gain": float(gains[worst_i]),
        "worst_fecha": str(rows[worst_i].get("fecha", "")),
        "best_roi": float(rois[best_i]),
        "best_fecha": str(rows[best_i].get("fecha", "")),
        "n_catastrophic": int((rois <= -0.90).sum()),
        "pcts": pcts,
        "fechas": [str(r.get("fecha", "")) for r in rows],
        "equity_curve": [float(x) for x in equity],
        "rois": [float(x) for x in rois],
    }


def session_stats(res: ReplayResult) -> dict:
    df = res.df
    minutes_to_peak = int((res.max_total_dt - df.iloc[0]["timestamp"]).total_seconds() // 60)
    minutes_to_trough = int((res.min_total_dt - df.iloc[0]["timestamp"]).total_seconds() // 60)
    spot_min = float(df["spot"].min())
    spot_max = float(df["spot"].max())
    spot_range_pct = (spot_max - spot_min) / df.iloc[0]["spot"] if df.iloc[0]["spot"] else 0.0
    return {
        "n_minutes": int(len(df)),
        "initial_total": round(res.initial_total, 2),
        "final_total": round(res.final_total, 2),
        "max_total": round(res.max_total, 2),
        "min_total": round(res.min_total, 2),
        "pnl_final_abs": round(res.pnl_final_abs, 2),
        "pnl_final_pct": round(res.pnl_final_pct, 4),
        "pnl_max_abs": round(res.pnl_max_abs, 2),
        "pnl_max_pct": round(res.pnl_max_pct, 4),
        "pnl_min_abs": round(res.pnl_min_abs, 2),
        "pnl_min_pct": round(res.pnl_min_pct, 4),
        "minutes_to_peak": minutes_to_peak,
        "minutes_to_trough": minutes_to_trough,
        "spot_min": round(spot_min, 2),
        "spot_max": round(spot_max, 2),
        "spot_range_pct": round(spot_range_pct, 4),
    }

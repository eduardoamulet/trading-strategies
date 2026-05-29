"""Session statistics for a single ReplayResult."""
from __future__ import annotations

from engine import ReplayResult


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

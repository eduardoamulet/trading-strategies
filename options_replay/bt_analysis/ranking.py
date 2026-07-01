"""Rankings de escenarios. Prioriza ROBUSTEZ/consistencia, no el mayor ROI aislado."""
from __future__ import annotations

import pandas as pd

# (columna, etiqueta, ascending) — ascending=False → «mayor es mejor». dd es negativo: mayor (menos
# negativo) es mejor. Sharpe/Sortino/Calmar solo aparecen si existen (granularidad detailed).
_RANK_SPECS = [
    ("robustness", "Robustness Score", False),
    ("roi", "ROI promedio (%)", False),
    ("win_rate", "Win Rate (%)", False),
    ("pf_proxy", "Profit Factor (proxy)", False),
    ("dd", "Drawdown — peor corrida (%)", False),
    ("net", "Ganancia neta ($)", False),
    ("expectancy", "Esperanza (ROI prom %)", False),
    ("sharpe", "Sharpe", False),
    ("sortino", "Sortino", False),
    ("calmar", "Calmar", False),
    ("error_rate", "Tasa de error (%)", True),
]

_SHOW = ["id", "robustness", "tier", "roi", "win_rate", "pf_proxy", "dd", "net", "n_trades"]


def rankings(df: pd.DataFrame, top: int = 15) -> dict:
    """{etiqueta_métrica: DataFrame top-N}. Solo métricas presentes en el df."""
    out = {}
    show = [c for c in _SHOW if c in df.columns]
    for key, label, asc in _RANK_SPECS:
        if key not in df.columns or df[key].notna().sum() == 0:
            continue
        cols = show if key in show else show + [key]
        out[label] = (df[df[key].notna()].sort_values(key, ascending=asc)
                      .head(top)[cols].reset_index(drop=True))
    return out


def best_overall(df: pd.DataFrame, min_trades: int = 30):
    """El escenario más robusto con un PISO de nº de corridas (evita overfits con muestra chica)."""
    d = df[df["n_trades"].fillna(0) >= min_trades] if "n_trades" in df.columns else df
    if d.empty:
        d = df
    return d.sort_values("robustness", ascending=False).iloc[0] if len(d) else None

"""Orquestador del motor de interpretación — ensambla las secciones según la GRANULARIDAD del file.

`analyze()` devuelve un dict `report` con las secciones aplicables. Nunca inventa análisis que los
datos no soportan: a nivel «scenario» marca explícitamente qué NO es derivable (día/ticker/hora/OOS).
"""
from __future__ import annotations

import pandas as pd

from . import (clustering, correlation, detailed as det, dow as dow_mod, loader, metrics,
               playbook, ranking, significance)


def analyze(results_df, granularity, scenarios_df=None, dow_df=None, seed=None) -> dict:
    """Corre el análisis aplicable. Devuelve `report` (dict de secciones)."""
    report = {"granularity": granularity, "seed": dict(seed or {}), "warnings": [], "notes": []}
    if granularity == loader.EMPTY:
        report["warnings"].append(
            "El results file NO tiene datos (solo los IDs C001–C480). Corré el batch para llenarlo "
            "antes de interpretar.")
        return report
    if granularity == loader.DETAILED:
        return _analyze_detailed(results_df, scenarios_df, seed, report)

    df = metrics.add_robustness(metrics.add_scenario_metrics(results_df))
    joined = loader.join_scenarios(df, scenarios_df) if scenarios_df is not None else df
    report["n_scenarios"] = int(df["id"].nunique())
    report["eda"] = _eda(df)
    report["scored"] = df
    report["joined"] = joined
    report["rankings"] = ranking.rankings(df)
    report["best"] = ranking.best_overall(df)
    report["correlations"] = correlation.correlations(joined)
    report["clusters"] = clustering.cluster_scenarios(joined)
    report["significance"] = significance.tests(df)

    if granularity == loader.SCENARIO:
        report["notes"].append(
            "Granularidad AGREGADA (1 fila por escenario: ticker+día+hora colapsados). El análisis por "
            "**día de la semana, ticker, hora, series temporales, OOS temporal y sensibilidad ±min NO "
            "es derivable de este file**. Se analiza a nivel ESCENARIO (qué config de condiciones es "
            "más robusta). Para el playbook por día, pasá un file DOW o uno detallado.")

    # Día de la semana: del file DOW si está; si el results es detailed, se computa del propio df.
    report["dow"] = dow_mod.analyze_dow(dow_df, detailed_df=(df if granularity == loader.DETAILED else None),
                                        scored=df)
    report["playbook"] = playbook.build_playbook(report)
    report["playbook_json"] = playbook.to_json(report["playbook"])
    return report


def _eda(df: pd.DataFrame) -> dict:
    """Estadísticos descriptivos + calidad de datos."""
    num = [c for c in ["roi", "win_rate", "pf_proxy", "dd", "best", "net", "n_trades",
                       "error_rate", "robustness"] if c in df.columns]
    desc = df[num].describe().round(2)
    # outliers por IQR sobre ROI
    q1, q3 = df["roi"].quantile(0.25), df["roi"].quantile(0.75)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    outliers = int(((df["roi"] < lo) | (df["roi"] > hi)).sum())
    return {
        "describe": desc,
        "n_scenarios": int(len(df)),
        "n_missing": int(df[num].isna().to_numpy().sum()),
        "roi_outliers": outliers,
        "n_profitable": int((df["roi"] > 0).sum()),
        "n_error_scenarios": int((df["error_rate"].fillna(0) > 0).sum()),
        "tier_counts": df["tier"].value_counts().to_dict(),
    }


def _analyze_detailed(results_df, scenarios_df, seed, report) -> dict:
    """Granularidad DETALLADA (1 fila por ticker×día×escenario): métricas REALES por-corrida +
    por-ticker + día-de-la-semana + OOS train/test."""
    dd = det.prepare(results_df)
    report["n_positions"] = int(len(dd))
    report["n_scenarios"] = int(dd["id"].nunique())
    report["n_days"] = int(dd["date"].nunique())
    report["n_tickers"] = int(dd["ticker"].nunique())

    scen = det.by_scenario(dd)     # métricas reales por escenario (sharpe/sortino/calmar/pf/...)
    scored = scen.rename(columns={"avg_roi": "roi", "max_dd": "dd", "profit_factor": "pf_proxy",
                                  "n": "n_trades", "net_roi": "net"})
    scored["best"] = scored["roi"]
    scored["expectancy"] = scored["roi"]
    scored["error_rate"] = 0.0
    scored = metrics.add_robustness(scored)   # robustez con dd/pf REALES
    joined = loader.join_scenarios(scored, scenarios_df) if scenarios_df is not None else scored

    report["scored"] = scored
    report["joined"] = joined
    report["eda"] = _eda(scored)
    report["rankings"] = ranking.rankings(scored)
    report["best"] = ranking.best_overall(scored)
    report["correlations"] = correlation.correlations(joined)
    report["clusters"] = clustering.cluster_scenarios(joined)
    report["significance"] = significance.tests(scored)
    report["by_ticker"] = det.by_ticker(dd)
    report["dow"] = det.by_weekday(dd)
    report["oos"] = det.oos_split(dd)
    report["context_corr"] = det.context_correlation(dd)   # contexto de mercado (md_*/spread) → ROI
    report["playbook"] = playbook.build_playbook(report)
    report["playbook_json"] = playbook.to_json(report["playbook"])
    report["notes"].append(
        f"Granularidad DETALLADA: {report['n_positions']:,} posiciones · {report['n_scenarios']} "
        f"escenarios × {report['n_tickers']} tickers × {report['n_days']} días. Habilitados: **por "
        "ticker, día de la semana (real), OOS train/test y Sharpe/Sortino/Calmar/PF reales**. La HORA "
        "sigue fija (09:30/13:55) → hora/±min no varían en este file (para eso hace falta un sweep de "
        "horarios).")
    return report


def analyze_paths(results_path, template_path=None, dow_path=None) -> dict:
    """Carga los files y corre `analyze()`. `template_path`/`dow_path` son opcionales."""
    rdf, gran = loader.load_results(results_path)
    seed, sc = loader.load_template(template_path) if template_path else ({}, None)
    dowdf = loader.load_dow(dow_path) if dow_path else None
    return analyze(rdf, gran, scenarios_df=sc, dow_df=dowdf, seed=seed)

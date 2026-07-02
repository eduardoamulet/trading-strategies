"""Exportar Interpretación para Análisis Cuantitativo — documento LLM-ready + JSON + CSV/Excel.

Construye TODO a partir del `report` de `engine.analyze()` (no recalcula nada): el mismo análisis que
ve la UI se serializa a un Markdown estructurado (optimizado para que Claude/ChatGPT lo analicen), un
JSON por escenario, y tablas CSV/Excel. Incluye el **ranking por probabilidad de ROI>0** (priorizando
robustez, no el ROI máximo) y una sección honesta de **datos faltantes** (qué agregar al backtest para
el análisis profundo). Puro; sin Streamlit.
"""
from __future__ import annotations

import io
import json

import pandas as pd

_DETAILED = "detailed"


# ── Ranking por probabilidad de ROI>0 (la tabla central del pedido) ─────────────────────────────
def probability_ranking(report: dict, min_trades: int = 20) -> pd.DataFrame:
    """Escenarios ordenados por ROBUSTEZ (proxy de «seguirá funcionando»), mostrando la probabilidad
    de ROI>0 (= win-rate) y el ROI esperado. Prioriza robustez y consistencia, no el ROI máximo.
    Filtra escenarios con pocas corridas (evita overfits de muestra chica)."""
    df = report.get("scored")
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    if "n_trades" in d.columns:
        d = d[d["n_trades"].fillna(0) >= min_trades] if (d["n_trades"].fillna(0) >= min_trades).any() else d
    d = d.sort_values(["robustness", "win_rate"], ascending=False).reset_index(drop=True)
    tier_ok = {"Excelente", "Muy Bueno", "Aceptable"}
    out = pd.DataFrame({
        "Ranking": range(1, len(d) + 1),
        "Escenario": d["id"],
        "Prob ROI>0 (%)": d["win_rate"].round(1),
        "ROI Esperado (%)": d["roi"].round(2),
        "Robustness Score": d["robustness"].round(0).astype("Int64"),
        "Tier": d["tier"],
    })
    for c, lbl in (("sharpe", "Sharpe"), ("pf_proxy", "Profit Factor"), ("dd", "Drawdown (%)")):
        if c in d.columns:
            out[lbl] = d[c].round(2)
    out["Recomendación"] = [
        ("OPERAR" if (t in tier_ok and w > 50 and r > 0) else "NO OPERAR")
        for t, w, r in zip(d["tier"], d["win_rate"].fillna(0), d["roi"].fillna(0))]
    return out


def scenarios_json(report: dict) -> list:
    """Un dict por escenario (consumible desde Python para automatizar la selección)."""
    df = report.get("scored")
    if df is None or df.empty:
        return []
    seed = report.get("seed", {})
    op = seed.get("Tipo de operacion", "")
    tickers = seed.get("Tickers", "")
    entry = seed.get("Horario de entrada", "")
    exit_ = seed.get("Horario de salida", "")
    tier_ok = {"Excelente", "Muy Bueno", "Aceptable"}
    rows = []
    for _, r in df.sort_values("robustness", ascending=False).iterrows():
        rec = ("OPERAR" if (r.get("tier") in tier_ok and (r.get("win_rate") or 0) > 50
                            and (r.get("roi") or 0) > 0) else "NO OPERAR")
        item = {
            "scenario": r["id"], "ticker": tickers, "operation": op,
            "entry": entry, "exit": exit_,
            "prob_roi_positive": round(float(r.get("win_rate") or 0), 1),
            "expected_roi": round(float(r.get("roi") or 0), 2),
            "profit_factor": _r(r.get("pf_proxy")),
            "drawdown": _r(r.get("dd")),
            "win_rate": round(float(r.get("win_rate") or 0), 1),
            "n_trades": int(r.get("n_trades") or 0),
            "robustness_score": round(float(r.get("robustness") or 0)),
            "tier": r.get("tier"),
            "recommendation": rec,
        }
        for c, k in (("sharpe", "sharpe"), ("sortino", "sortino"), ("calmar", "calmar")):
            if c in df.columns:
                item[k] = _r(r.get(c))
        rows.append(item)
    return rows


def _r(x):
    try:
        return None if x is None or pd.isna(x) else round(float(x), 3)
    except Exception:
        return None


# ── Datos faltantes / sugerencias (lo que el usuario pidió explícitamente) ──────────────────────
def data_gaps(report: dict) -> list:
    """Qué información NO está en el results file y habría que capturar para el análisis profundo."""
    gran = report.get("granularity")
    gaps = [
        "**Contrato / greeks** (Strike, Delta, Gamma, Theta, Bid, Ask, Spread, Open Interest, Volumen): "
        "no están en los results del batch. Para incluirlos, el motor de backtest debería registrar un "
        "SNAPSHOT del contrato elegido al entrar (NBBO + greeks) por posición.",
        "**Señales del Market Direction Engine** (VWAP, EMA9/20/50, ATR, Relative Volume, Momentum, "
        "Opening Range, Gap, Market Score, Confidence, TradeSignal, razones): son de OTRO sistema "
        "(market_direction), el backtest de opciones no las corre. Para cruzarlas, el batch debería "
        "llamar al engine por posición y loguear el TradeSignal.",
        "**Motivo de entrada/salida y duración exacta por posición**: el file agregado no los trae; el "
        "detallado tiene 1 fila por ticker×día pero sin el motivo de cierre ni el contrato.",
    ]
    if gran != _DETAILED:
        gaps.append("**Granularidad**: este file AGREGA ticker+día+hora en 1 fila por escenario → por-"
                    "ticker, día-de-la-semana, hora, OOS y sensibilidad NO son derivables. Generá un "
                    "results DETALLADO (1 fila por ticker×día×escenario, modo `report.write` sin --fill).")
    gaps.append("**Variación de HORA**: los 480 escenarios usan entrada 09:30 / salida 13:55 fijas → "
                "«hora vs ROI» y sensibilidad ±min no tienen variación. Para eso hace falta un SWEEP de "
                "horarios (correr los escenarios con distintas horas de entrada/salida).")
    gaps.append("**Período más largo y diverso**: para día-de-la-semana robusto + walk-forward + OOS "
                "confiables se necesitan varios meses (idealmente multi-régimen), no 2 semanas.")
    return gaps


# ── Markdown estructurado (LLM-ready) ────────────────────────────────────────────────────────────
def _df_md(df: pd.DataFrame, max_rows: int = 30) -> str:
    """DataFrame → tabla Markdown (sin dependencias). Trunca a max_rows."""
    if df is None or getattr(df, "empty", True):
        return "_(sin datos)_\n"
    d = df.head(max_rows)
    cols = list(d.columns)
    head = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join("| " + " | ".join(_cell(v) for v in row) + " |"
                     for row in d.itertuples(index=False))
    extra = f"\n\n_… {len(df) - max_rows} filas más (ver CSV/Excel adjunto)._" if len(df) > max_rows else ""
    return f"{head}\n{sep}\n{body}{extra}\n"


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v).replace("|", "\\|").replace("\n", " ")


def to_markdown(report: dict) -> str:
    """Documento Markdown completo y estructurado, optimizado para análisis por IA."""
    gran = report.get("granularity")
    seed = report.get("seed", {})
    eda = report.get("eda", {})
    L = []
    L.append("# Interpretación de Backtesting — Análisis Cuantitativo\n")
    L.append("> Documento generado por SignalForge para análisis estadístico posterior (Claude/ChatGPT). "
             "El objetivo es identificar los escenarios (C001–C480) con mayor **probabilidad de ROI>0** y "
             "mejor **ROI ajustado por riesgo**, priorizando **robustez y consistencia** sobre el ROI "
             "máximo histórico. Si ningún escenario tiene ventaja estadística suficiente → **NO OPERAR**.\n")

    # 1 Resumen ejecutivo
    L.append("## 1. Resumen Ejecutivo\n")
    b = report.get("best")
    if b is not None:
        L.append(f"- **Mejor escenario (por robustez):** `{b['id']}` — Robustness {b['robustness']:.0f} "
                 f"({b['tier']}), ROI prom {float(b.get('roi') or 0):.2f}%, Win-rate "
                 f"{float(b.get('win_rate') or 0):.1f}%.")
    L.append(f"- **Escenarios:** {report.get('n_scenarios', 0)} · rentables: "
             f"{eda.get('n_profitable', 0)} · granularidad: **{gran}**.")
    dow = report.get("dow", {})
    if dow.get("available"):
        no = [d for d, i in dow.get("per_day", {}).items() if i.get("recommendation") == "NO OPERAR"]
        L.append(f"- **Por día:** OPERAR {dow.get('n_operar', 0)}/{dow.get('n_dias', 0)} días"
                 + (f" · NO OPERAR: {', '.join(no)}." if no else "."))
    corr = report.get("correlations")
    if corr is not None and not corr.empty:
        t = corr.iloc[0]
        L.append(f"- **Driver principal:** `{t['param']}` ↔ {t['target']} (Spearman {t['spearman']:+.2f}).")

    # 2 Config
    L.append("\n## 2. Configuración utilizada\n")
    if seed:
        L.append(_df_md(pd.DataFrame([{"Parámetro": k, "Valor": v} for k, v in seed.items()]), 40))
    else:
        L.append("_(sin template — subí el template para la config global)_\n")

    # 3 Resultados generales / EDA
    L.append("\n## 3. Resultados Generales (EDA)\n")
    L.append(f"- Faltantes: {eda.get('n_missing', 0)} · Outliers ROI (IQR): {eda.get('roi_outliers', 0)} "
             f"· Escenarios con error: {eda.get('n_error_scenarios', 0)}.")
    L.append("- Calidad (tier): " + " · ".join(f"{k} {v}" for k, v in (eda.get("tier_counts") or {}).items()))
    if eda.get("describe") is not None:
        L.append("\n**Estadísticos descriptivos:**\n")
        L.append(_df_md(eda["describe"].reset_index().rename(columns={"index": "métrica"}), 20))

    # 4 Ranking por probabilidad de ROI>0 (central)
    L.append("\n## 4. Ranking — Probabilidad de ROI>0 (priorizando robustez)\n")
    L.append("Ordenado por Robustness Score (proxy de «probabilidad de seguir funcionando»). "
             "`Prob ROI>0` = win-rate sobre las corridas. `Recomendación` = OPERAR solo con tier ≥ "
             "Aceptable, Prob>50% y ROI>0.\n")
    L.append(_df_md(probability_ranking(report), 25))

    # 5 Por escenario (todos, resumido)
    L.append("\n## 5. Resultados por Escenario\n")
    L.append(_scored_md(report))

    # 6 Por ticker
    L.append("\n## 6. Resultados por Ticker\n")
    bt = report.get("by_ticker")
    L.append(_df_md(bt, 20) if bt is not None else
             "_No derivable a esta granularidad (ticker agregado). Requiere results detallado._\n")

    # 7 Por día de la semana
    L.append("\n## 7. Resultados por Día de la Semana\n")
    if dow.get("available"):
        rows = [{"Día": d, **{k: v for k, v in i.items()
                              if k in ("scenario", "win_rate", "avg_roi", "avg_usd", "sharpe",
                                       "recommendation")}}
                for d, i in dow.get("per_day", {}).items()]
        L.append(_df_md(pd.DataFrame(rows), 10))
    else:
        L.append("_No disponible: " + dow.get("reason", "sin datos por día") + "._\n")

    # 8 Por hora
    L.append("\n## 8. Resultados por Hora\n")
    L.append("_No derivable: los 480 escenarios usan entrada/salida FIJAS (09:30 / 13:55). Para «hora "
             "vs ROI» hace falta un sweep de horarios._\n")

    # 9 Correlaciones
    L.append("\n## 9. Correlaciones (condición → outcome)\n")
    L.append("Pearson (lineal) · Spearman (monótona) · Kendall (concordancia).\n")
    L.append(_df_md(corr, 30) if corr is not None else "_(sin correlaciones)_\n")
    cc = report.get("context_corr")
    if cc is not None and not cc.empty:
        L.append("\n**Contexto de mercado a la entrada → ROI de la posición** (del results ENRIQUECIDO — "
                 "score/confianza del Market Direction Engine, spread, duración):\n")
        L.append(_df_md(cc, 15))

    # 10 Clustering
    L.append("\n## 10. Clustering (familias de escenarios)\n")
    cl = report.get("clusters", {})
    L.append(_df_md(cl["summary"], 10) if cl and not cl.get("error") else "_(sin clustering)_\n")

    # 11 OOS
    L.append("\n## 11. Validación Out-of-Sample (train/test 70/30)\n")
    oos = report.get("oos")
    if oos and oos.get("available"):
        L.append(f"Corte {oos['cut_date']} · train {oos['n_train_days']}d → test {oos['n_test_days']}d. "
                 "Clasificación: **Robusto** (test≥60% train) · **Moderadamente Robusto** (≥30%) · "
                 "**Poco Robusto** · **Sobreajustado**.\n")
        L.append("Distribución: " + " · ".join(f"{k}: {v}" for k, v in (oos.get("counts") or {}).items()) + "\n")
        L.append(_df_md(oos.get("table"), 25))
    else:
        L.append("_No derivable a esta granularidad (necesita results detallado con varios días)._\n")

    # 12 Significancia
    L.append("\n## 12. Estadísticas / Significancia\n")
    sig = report.get("significance", {})
    rz = sig.get("roi_vs_zero")
    if rz:
        L.append(f"- ROI medio vs 0: media {rz['mean_roi']}% · t={rz['t']} · p={rz['p_value']} → "
                 f"{'significativo' if rz['sig'] else 'no significativo'}.")
    for c in sig.get("caveats", []):
        L.append(f"- ⚠️ {c}")

    # 13 Resumen automático
    L.append("\n## 13. Resumen Automático\n")
    L.append(_auto_summary(report))

    # 14 Datos faltantes
    L.append("\n## 14. Datos Faltantes y Sugerencias (para análisis más profundo)\n")
    for g in data_gaps(report):
        L.append(f"- {g}")

    # 15 Conclusiones
    L.append("\n## 15. Conclusiones\n")
    L.append("- Priorizá **robustez y consistencia** por encima del ROI máximo histórico.")
    L.append("- Usá el **Ranking (§4)** para elegir el escenario con mayor probabilidad de ROI>0; si el "
             "primero deja de ser válido (liquidez/filtro), pasá al siguiente sin recalcular todo.")
    L.append("- Si para una fecha/ticker ningún escenario supera el umbral de ventaja → **NO OPERAR**.")
    L.append("- Para responder «dado ticker+fecha+contexto, ¿qué escenario ejecutar?» de forma completa, "
             "capturá los **datos faltantes (§14)** — sobre todo greeks del contrato y las señales del "
             "Market Direction Engine por posición.")
    return "\n".join(L) + "\n"


def _scored_md(report: dict) -> str:
    df = report.get("scored")
    if df is None or df.empty:
        return "_(sin datos)_\n"
    cols = [c for c in ["id", "robustness", "tier", "roi", "win_rate", "pf_proxy", "sharpe", "dd",
                        "net", "n_trades"] if c in df.columns]
    d = df.sort_values("robustness", ascending=False)[cols].rename(columns={
        "id": "Escenario", "robustness": "Robustez", "tier": "Tier", "roi": "ROI%",
        "win_rate": "WinRate%", "pf_proxy": "PF", "sharpe": "Sharpe", "dd": "Drawdown%",
        "net": "Neto$", "n_trades": "n"})
    return _df_md(d, 40)


def _auto_summary(report: dict) -> str:
    df = report.get("scored")
    if df is None or df.empty:
        return "_(sin datos)_\n"
    parts = []
    top = df.sort_values("robustness", ascending=False).head(20)
    bot = df.sort_values("robustness", ascending=True).head(20)
    parts.append("**20 mejores escenarios (robustez):** " + ", ".join(top["id"].tolist()))
    parts.append("\n\n**20 peores escenarios (robustez):** " + ", ".join(bot["id"].tolist()))
    bt = report.get("by_ticker")
    if bt is not None and not bt.empty:
        parts.append("\n\n**Tickers más rentables:** "
                     + ", ".join(f"{r['ticker']} ({r['avg_roi']:.1f}%)"
                                 for _, r in bt.sort_values("avg_roi", ascending=False).head(3).iterrows()))
    dow = report.get("dow", {})
    if dow.get("available"):
        pd_ = dow.get("per_day", {})
        _m = lambda i: (i.get("avg_roi") if i.get("avg_roi") is not None else i.get("avg_usd")) or -1e9
        best = sorted(pd_.items(), key=lambda kv: _m(kv[1]), reverse=True)
        parts.append("\n\n**Días más rentables:** "
                     + ", ".join(f"{d}" for d, _ in best[:2]))
        parts.append(" · **menos rentables:** " + ", ".join(f"{d}" for d, _ in best[-2:]))
    op = report.get("seed", {}).get("Tipo de operacion", "")
    parts.append(f"\n\n**CALL vs PUT:** los escenarios corren «{op}» (no separable por pierna en este file).")
    # distribuciones (quantiles)
    for col, lbl in (("roi", "ROI%"), ("win_rate", "Win Rate%"), ("dd", "Drawdown%")):
        if col in df.columns and df[col].notna().any():
            q = df[col].quantile([0.1, 0.5, 0.9]).round(2)
            parts.append(f"\n\n**Distribución {lbl}:** p10 {q.iloc[0]} · mediana {q.iloc[1]} · p90 {q.iloc[2]}")
    return "".join(parts) + "\n"


# ── CSV / Excel ──────────────────────────────────────────────────────────────────────────────────
def scored_csv(report: dict) -> bytes:
    df = report.get("scored")
    if df is None or df.empty:
        return b""
    return df.to_csv(index=False).encode("utf-8")


def to_excel(report: dict) -> bytes:
    """Excel multi-hoja: Escenarios · Ranking · PorTicker · PorDía · Correlaciones."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        sc = report.get("scored")
        if sc is not None and not sc.empty:
            sc.to_excel(w, index=False, sheet_name="Escenarios")
        rk = probability_ranking(report)
        if not rk.empty:
            rk.to_excel(w, index=False, sheet_name="Ranking")
        bt = report.get("by_ticker")
        if bt is not None and not bt.empty:
            bt.to_excel(w, index=False, sheet_name="PorTicker")
        dow = report.get("dow", {})
        if dow.get("available"):
            rows = [{"Día": d, **{k: v for k, v in i.items() if not isinstance(v, (list, dict))}}
                    for d, i in dow.get("per_day", {}).items()]
            pd.DataFrame(rows).to_excel(w, index=False, sheet_name="PorDia")
        corr = report.get("correlations")
        if corr is not None and not corr.empty:
            corr.to_excel(w, index=False, sheet_name="Correlaciones")
    return buf.getvalue()


def scenarios_json_bytes(report: dict) -> bytes:
    return json.dumps(scenarios_json(report), indent=2, ensure_ascii=False).encode("utf-8")

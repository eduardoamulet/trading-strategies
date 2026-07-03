"""UI Streamlit del motor de interpretación — render del `report` de engine.analyze().

Separada de la lógica: recibe el dict `report` y dibuja secciones + visualizaciones + descargas.
Degrada con elegancia cuando una sección no aplica a la granularidad del file.
"""
from __future__ import annotations

import io
import json

import pandas as pd
import streamlit as st

_REC_COLOR = {"OPERAR": "#16a34a", "NO OPERAR": "#dc2626"}
_TIER_COLOR = {"Excelente": "#15803d", "Muy Bueno": "#16a34a", "Aceptable": "#ca8a04",
               "Débil": "#ea580c", "Descartar": "#dc2626"}


def render(report: dict) -> None:
    gran = report.get("granularity")
    if gran == "empty":
        st.error("⚠️ " + report["warnings"][0])
        return

    st.caption(f"Granularidad detectada: **{gran}** · **{report.get('n_scenarios', 0)}** escenarios")
    for n in report.get("notes", []):
        st.info(n)
    for w in report.get("warnings", []):
        st.warning(w)

    _exec_summary(report)
    _export(report)
    _eda(report)
    _by_ticker(report)
    _context(report)
    _rankings(report)
    _correlations(report)
    _clustering(report)
    _dow(report)
    _oos(report)
    _significance(report)
    _playbook(report)


def _by_ticker(report: dict) -> None:
    bt = report.get("by_ticker")
    if bt is None or getattr(bt, "empty", True):
        return
    with st.expander("4 · Por ticker — rendimiento de cada activo", expanded=False):
        st.dataframe(bt, use_container_width=True, hide_index=True)


def _context(report: dict) -> None:
    cc = report.get("context_corr")
    if cc is None or getattr(cc, "empty", True):
        return
    with st.expander("5 · Contexto de mercado → ROI — ¿la señal a la entrada predice el resultado?",
                     expanded=True):
        st.caption("Correlación (Pearson/Spearman) del **contexto a la entrada** (score/confianza del "
                   "Market Direction Engine, spread del contrato, duración) con el **ROI de la posición**. "
                   "Del results ENRIQUECIDO. |corr| alto = el contexto ayuda a predecir el ROI.")
        st.dataframe(cc, use_container_width=True, hide_index=True)


def _oos(report: dict) -> None:
    oos = report.get("oos")
    if not oos:
        return
    with st.expander("VALIDACIÓN OUT-OF-SAMPLE — Train/Test 70/30 cronológico", expanded=True):
        if not oos.get("available"):
            st.warning("No derivable: " + oos.get("reason", ""))
            return
        st.caption(f"Corte: **{oos['cut_date']}** · train {oos['n_train_days']} días → "
                   f"test {oos['n_test_days']} días. Clasificación por consistencia train↔test: "
                   "**Robusto** (test≥60% del train) · **Moderadamente Robusto** (≥30%) · "
                   "**Poco Robusto** · **Sobreajustado** (bueno en train, negativo en test).")
        st.markdown("**Distribución:** " + " · ".join(f"{k}: {v}" for k, v in oos.get("counts", {}).items()))
        tbl = oos.get("table")
        if tbl is not None and not tbl.empty:
            st.dataframe(tbl, use_container_width=True, hide_index=True)


def _exec_summary(report: dict) -> None:
    st.markdown("### 📌 Resumen ejecutivo")
    b = report.get("best")
    eda = report.get("eda", {})
    c1, c2, c3, c4 = st.columns(4)
    if b is not None:
        c1.metric("Mejor escenario", str(b["id"]), f"robustez {b['robustness']:.0f} · {b['tier']}")
        c2.metric("ROI prom (mejor)", f"{float(b.get('roi') or 0):.2f}%")
        c3.metric("Win Rate (mejor)", f"{float(b.get('win_rate') or 0):.1f}%")
    c4.metric("Escenarios rentables", f"{eda.get('n_profitable', 0)}/{report.get('n_scenarios', 0)}")
    dow = report.get("dow", {})
    if dow.get("available"):
        _n_op = dow.get("n_operar", 0)
        _n = dow.get("n_dias", 0)
        _insuf = dow.get("n_insuf", 0)
        _no = [d for d, i in dow.get("per_day", {}).items() if i.get("recommendation") == "NO OPERAR"]
        if _n_op == 0 and _n and _insuf >= _n:
            st.info(f"**Por día de la semana:** sin datos suficientes (n < {dow.get('min_n', 8)} por día — "
                    "ventana corta). ⚠️ Esto **no** dice que los escenarios sean malos: los números de "
                    "arriba son a nivel ESCENARIO (usan TODAS las posiciones y sí son fiables). Para un "
                    "veredicto POR DÍA hace falta un período más largo (varios meses).")
        else:
            st.success(f"**Veredicto por día**: OPERAR **{_n_op}/{_n}** días"
                       + (f" · **NO OPERAR**: {', '.join(_no)}" if _no else ""))


def _export(report: dict) -> None:
    from . import export as _exp
    st.markdown("### 📤 Exportar Interpretación para Análisis Cuantitativo")
    st.caption("Genera un documento **Markdown LLM-ready** (+ JSON / CSV / Excel) con todo el análisis, "
               "listo para que Claude/ChatGPT descubran patrones. Prioriza **probabilidad de ROI>0 y "
               "robustez**, no el ROI máximo histórico.")
    rk = _exp.probability_ranking(report)
    if not rk.empty:
        st.markdown("**Ranking — Probabilidad de ROI>0 (priorizando robustez):**")
        st.dataframe(rk.head(25).style.map(
            lambda v: f"color:{_REC_COLOR.get(v, '')};font-weight:700" if v in _REC_COLOR else "",
            subset=["Recomendación"]), use_container_width=True, hide_index=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.download_button("⬇ Markdown (IA)", _exp.to_markdown(report).encode("utf-8"),
                       file_name="interpretacion_backtesting.md", mime="text/markdown",
                       use_container_width=True)
    c2.download_button("⬇ JSON", _exp.scenarios_json_bytes(report), file_name="escenarios.json",
                       mime="application/json", use_container_width=True)
    c3.download_button("⬇ CSV", _exp.scored_csv(report), file_name="escenarios.csv", mime="text/csv",
                       use_container_width=True)
    c4.download_button("⬇ Excel", _exp.to_excel(report), file_name="interpretacion.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)
    st.markdown("**¿Qué falta capturar para el análisis profundo?** (sugerencias)")
    for g in _exp.data_gaps(report):
        st.markdown(f"- {g}")


def _eda(report: dict) -> None:
    with st.expander("1 · EDA — calidad de datos y estadísticos descriptivos", expanded=False):
        eda = report.get("eda", {})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Valores faltantes", eda.get("n_missing", 0))
        c2.metric("Outliers ROI (IQR)", eda.get("roi_outliers", 0))
        c3.metric("Escenarios con error", eda.get("n_error_scenarios", 0))
        c4.metric("Rentables", eda.get("n_profitable", 0))
        st.markdown("**Distribución por calidad (tier):** "
                    + " · ".join(f"{k} {v}" for k, v in (eda.get("tier_counts") or {}).items()))
        st.dataframe(eda.get("describe"), use_container_width=True)
        _plot_hist(report.get("scored"), "robustness", "Distribución del Robustness Score")


def _rankings(report: dict) -> None:
    ranks = report.get("rankings", {})
    if not ranks:
        return
    with st.expander("7 · Rankings — por métrica (prioriza robustez, no ROI aislado)", expanded=False):
        metric = st.selectbox("Ordenar por", list(ranks.keys()), key="bta_rank_metric")
        st.dataframe(ranks[metric], use_container_width=True, hide_index=True)


def _correlations(report: dict) -> None:
    corr = report.get("correlations")
    if corr is None or corr.empty:
        return
    with st.expander("2 · Correlación — qué condiciones mueven el ROI/WinRate/Drawdown", expanded=True):
        st.caption("Pearson (lineal) · Spearman (monótona, robusta) · Kendall (concordancia). "
                   "Ordenado por |Spearman|.")
        st.dataframe(corr, use_container_width=True, hide_index=True)
        try:
            import plotly.express as px
            piv = corr.pivot_table(index="param", columns="target", values="spearman")
            fig = px.imshow(piv, color_continuous_scale="RdBu", zmin=-1, zmax=1, aspect="auto",
                            text_auto=".2f", title="Heatmap — Spearman (condición × outcome)")
            fig.update_layout(height=max(300, 34 * len(piv)), margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, use_container_width=True)
        except Exception:
            pass
        top = corr.iloc[0]
        st.info(f"**Driver más fuerte:** `{top['param']}` ↔ {top['target']} "
                f"(Spearman {top['spearman']:+.2f}). "
                "Signo + = subir/activar esa condición mejora el outcome; − = lo empeora.")


def _clustering(report: dict) -> None:
    cl = report.get("clusters", {})
    if not cl or cl.get("error"):
        return
    with st.expander(f"6 · Clustering — {cl.get('k')} familias de escenarios", expanded=False):
        st.dataframe(cl["summary"], use_container_width=True, hide_index=True)
        try:
            import plotly.express as px
            d = report["scored"].copy()
            d["cluster"] = cl["labels"]
            fig = px.scatter(d, x="win_rate", y="roi", color="cluster", hover_name="id",
                             color_continuous_scale="Turbo", title="Escenarios: ROI vs Win Rate (por cluster)")
            fig.update_layout(height=380, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, use_container_width=True)
        except Exception:
            pass


def _cfg_str(c: dict | None) -> str:
    """Condiciones de salida de un escenario → string compacto RESPETANDO los flags Sí/No
    (una condición apagada se ve «off» — es lo que diferencia C001/C041/C061/C063/C123, no los
    números). Fuente única del formato: trade_plan.scenario_config_summary."""
    from trade_plan import scenario_config_summary
    return scenario_config_summary(c)


def _dow(report: dict) -> None:
    dow = report.get("dow", {})
    with st.expander("3+8 · Día de la semana — mejor config + OPERAR / NO OPERAR", expanded=True):
        if not dow.get("available"):
            st.warning("No derivable de este file: " + dow.get("reason", ""))
            return
        st.caption(f"Nivel **{dow.get('level', 'cartera')}** · fuente: {dow.get('source', '')}. El ROI "
                   "por día es de **cartera** (Σganancia/Σinversión de los 3 tickers ese día → refleja el "
                   f"colectivo). Regla: OPERAR solo con ventaja (WR>55% · ROI>0 · Sharpe>0 · "
                   f"n≥{dow.get('min_n', 5)} días); si no, **NO OPERAR**.")
        # Condiciones de salida POR ESCENARIO (results ⋈ template). Sin template → {} (solo IDs).
        try:
            from . import playbook as _pbk
            _cfgs = _pbk._scenario_configs(report)
        except Exception:
            _cfgs = {}
        _is_roi = any("avg_roi" in i for i in dow.get("per_day", {}).values())
        _mlabel = "ROI % prom" if _is_roi else "$ prom"
        rows = []
        for dia, i in dow.get("per_day", {}).items():
            rows.append({"Día": dia, "Escenario": i.get("scenario"), "Win Rate %": i.get("win_rate"),
                         _mlabel: i.get("avg_roi", i.get("avg_usd")), "Sharpe": i.get("sharpe"),
                         "n": i.get("n"), "Recomendación": i.get("recommendation"),
                         **({"Condiciones de salida": _cfg_str(_cfgs.get(str(i.get("scenario"))))}
                            if _cfgs else {}),
                         "Motivo": i.get("reason", "")})
        df = pd.DataFrame(rows)
        st.dataframe(df.style.map(
            lambda v: f"color:{_REC_COLOR.get(v, '')};font-weight:700" if v in _REC_COLOR else "",
            subset=["Recomendación"]), use_container_width=True, hide_index=True)
        if not _cfgs:
            st.caption("💡 Para ver **qué condición de salida aplica cada escenario** (umbral, stop, "
                       "colectivo…), subí también el **template** al interpretar — las condiciones "
                       "viven en la hoja «Backtesting scenarios».")
        try:
            import plotly.express as px
            fig = px.bar(df, x="Día", y=_mlabel, color="Recomendación",
                         color_discrete_map=_REC_COLOR, title=f"{_mlabel} por día — mejor config")
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, use_container_width=True)
        except Exception:
            pass

        # Desglose por TICKER: ¿el patrón del día difiere entre QQQ / SPY / IWM?
        dt = report.get("dow_ticker")
        if dt is not None and not dt.empty:
            st.markdown("**Por día × ticker** — ¿el patrón difiere entre activos?")
            st.caption("Aquí el n es por ticker (≈ nº de semanas, ~⅓ del combinado), por eso su umbral "
                       f"es más bajo (n≥{dow.get('min_n', 5)}). Si un día es OPERAR en cartera pero "
                       "NO OPERAR en un ticker, esa ventaja no es uniforme entre activos.")
            _dt = dt.copy()
            if _cfgs and "Escenario" in _dt.columns:
                # La condición de salida QUE SE LE APLICÓ a ese (día, ticker) = la del escenario
                # ganador de esa celda (todas las filas de un escenario corren con SU config).
                _dt["Condiciones de salida"] = _dt["Escenario"].map(
                    lambda s: _cfg_str(_cfgs.get(str(s).strip())))
            st.dataframe(_dt.style.map(
                lambda v: f"color:{_REC_COLOR.get(v, '')};font-weight:700" if v in _REC_COLOR else "",
                subset=["Recomendación"]), use_container_width=True, hide_index=True)
            try:
                piv = dt.pivot(index="Ticker", columns="Día", values="Recomendación")
                piv = piv.reindex(columns=[c for c in ["Lun", "Mar", "Mié", "Jue", "Vie"]
                                           if c in piv.columns])
                st.caption("Matriz Ticker × Día (verde = OPERAR):")
                st.dataframe(piv.style.map(
                    lambda v: f"background-color:{_REC_COLOR.get(v, '')}22;color:{_REC_COLOR.get(v, '')};"
                              "font-weight:700" if v in _REC_COLOR else ""), use_container_width=True)
            except Exception:
                pass


def _significance(report: dict) -> None:
    sig = report.get("significance", {})
    if not sig:
        return
    with st.expander("10 · Significancia estadística", expanded=False):
        rz = sig.get("roi_vs_zero")
        if rz:
            st.markdown(f"- **ROI medio vs 0**: media {rz['mean_roi']}% · t={rz['t']} · "
                        f"p={rz['p_value']} → {'**significativo**' if rz['sig'] else 'no significativo'}. "
                        f"{rz['interp']}.")
        bw = sig.get("best_winrate_vs_50")
        if bw:
            st.markdown(f"- **Win-rate del mejor ({bw['id']}) vs 50%**: {bw['win_rate']}% (n={bw['n']}) · "
                        f"p={bw['p_value']} → {'**significativo**' if bw['sig'] else 'no significativo'}.")
        for c in sig.get("caveats", []):
            st.caption("⚠️ " + c)


def _playbook(report: dict) -> None:
    pb = report.get("playbook", {})
    pj = report.get("playbook_json", {})
    with st.expander("11 · Playbook + JSON — recomendaciones accionables", expanded=True):
        st.caption(f"Config fija: entrada **{pb.get('entrada_fija')}** · salida **{pb.get('salida_fija')}** "
                   f"· **{pb.get('operacion')}** · {pb.get('tickers')} · nivel: {pb.get('nivel')}")
        if pb.get("mejor_escenario"):
            m = pb["mejor_escenario"]
            _rc = _REC_COLOR.get(m["recommendation"], "#888")
            st.markdown(f"**Mejor escenario global:** {m['scenario']} · robustez {m['robustness']:.0f} "
                        f"({m['tier']}) · ROI {m['roi']}% · WR {m['win_rate']}% → "
                        f"<span style='color:{_rc};font-weight:700'>{m['recommendation']}</span>",
                        unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        c1.download_button("⬇ Playbook JSON", json.dumps(pj, indent=2, ensure_ascii=False).encode("utf-8"),
                           file_name="playbook.json", mime="application/json", use_container_width=True)
        # Excel con todos los escenarios puntuados
        scored = report.get("scored")
        if scored is not None:
            buf = io.BytesIO()
            cols = [c for c in ["id", "robustness", "tier", "roi", "win_rate", "pf_proxy", "dd", "best",
                                "net", "n_trades", "error_rate"] if c in scored.columns]
            with pd.ExcelWriter(buf, engine="openpyxl") as w:
                scored[cols].to_excel(w, index=False, sheet_name="Escenarios")
            c2.download_button("⬇ Escenarios (Excel)", buf.getvalue(), file_name="escenarios_puntuados.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               use_container_width=True)
        st.json(pj, expanded=False)


def _plot_hist(scored, col: str, title: str) -> None:
    if scored is None or col not in scored.columns:
        return
    try:
        import plotly.express as px
        fig = px.histogram(scored, x=col, nbins=30, title=title)
        fig.update_layout(height=280, margin=dict(l=10, r=10, t=40, b=10), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        st.bar_chart(scored[col].value_counts().sort_index())

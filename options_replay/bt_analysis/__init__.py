"""bt_analysis — motor de interpretación de resultados de backtesting (opciones 0DTE).

ADAPTATIVO a la granularidad del results file:
  · "scenario"  → 1 fila por escenario (ticker+día+hora agregados): ranking, robustez, clustering,
                  correlación condición→ROI, JSON de mejores configs. (El file estándar del batch.)
  · "detailed"  → 1 fila por (ticker×día×escenario): habilita día-de-la-semana, por-ticker, OOS,
                  sensibilidad, métricas por-corrida (PF/Sharpe/Sortino/Calmar reales).

NO busca el mayor ROI aislado: prioriza consistencia y robustez, y concluye NO OPERAR cuando no hay
ventaja estadística suficiente. Todo es lógica pura (sin Streamlit); la UI vive en `ui.py`.
"""
from __future__ import annotations

__all__ = ["analyze", "analyze_paths"]


def __getattr__(name):   # import lazy → permite usar submódulos sin cargar todo el motor
    if name in __all__:
        from . import engine
        return getattr(engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

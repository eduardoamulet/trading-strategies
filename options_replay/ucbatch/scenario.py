"""Scenario Parser — Seed (global) + Scenario (condiciones) → kwargs de `run_one` + config colectiva.

Traduce el dominio del Excel al contrato del engine. El runner hace:
    spec = {ticker, fecha, hora: seed.entrada, tipo: seed.tipo}
    it   = run_one(dl, spec, **mapped.run_kwargs)
y, si `mapped.collective` no es None, aplica `apply_collective_exit(..., **mapped.collective)` por día.

Gating por «Alcance de salida»:
  • solo colectivo → condiciones de ticker (umbral/stop/filtro) NEUTRALIZADAS.
  • solo tickers   → colectivo desactivado (collective = None).
"""
from __future__ import annotations

from dataclasses import dataclass

from .reader import Scenario, Seed

# ── límites «no aplica» (mismos sentinels que usa la app: el umbral nunca se toca / sin stop) ──
_NO_PROFIT = 100000.0
_NO_STOP = -100000.0


def _yes(v) -> bool:
    return str(v).strip().lower() in ("sí", "si", "yes", "y", "true", "1", "✓", "x")


def _num(v, default: float) -> float:
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default


def resolution_from_seg(seg: int) -> str:
    """Granularidad en segundos → clave de resolución del Downloader."""
    return {15: "15s", 30: "30s", 60: "1min"}.get(int(seg), "1min")


def _fill_flags(fills: str) -> tuple:
    """Texto de «Modelo de fills» → (entry_at_ask, exit_at_bid, nbbo_timeline)."""
    f = (fills or "").lower()
    if "fase 2" in f or "por barra" in f:
        return True, True, True          # Fase 2: compra ask / vende bid / mark = bid por minuto
    if "fase 1" in f or "entrada/salida" in f:
        return True, True, False         # Fase 1: cruza spread en entrada/salida, sin timeline
    return False, False, False           # Precio de barra (rápido)


def _selection(criterio: str) -> str:
    """«Criterio de selección de contrato» → código del engine. El COMPUESTO («… sino …»)
    se detecta PRIMERO: su etiqueta contiene «spread» e «itm» a la vez y el contains simple
    lo mapearía mal."""
    c = (criterio or "").lower()
    if "sino" in c or ("spread" in c and "itm" in c):
        return "spread_itm_first"      # Opción 1; si no compra → fallback 1-ITM
    return "itm_first" if "itm" in c else "spread"


def _dte_flags(dte: str) -> tuple:
    """«Vencimiento DTE» → (dte:int, auto_dte:bool)."""
    d = (dte or "").lower()
    if "auto" in d:
        return 0, True
    if "1" in d:
        return 1, False
    return 0, False


@dataclass
class MappedScenario:
    id: str
    run_kwargs: dict           # **kwargs para run_one (ticker/fecha/hora/tipo van en el spec)
    collective: dict | None    # {profit_frac, stop_frac} o None
    alcance: str
    tipo: str | None = None    # «Tipo de operación (escenario)»: override del tipo del seed
                               # (permite comparar políticas de salida como variable); None = seed


def map_scenario(seed: Seed, sc: Scenario) -> MappedScenario:
    g = sc.cond.get
    alcance = str(g("Alcance de salida") or "tickers y colectivo").strip().lower()
    tk_on = alcance != "solo colectivo"
    col_on = alcance != "solo tickers"

    e_ask, e_bid, nbbo = _fill_flags(seed.fills)
    dte, auto_dte = _dte_flags(seed.dte)

    # ── Condiciones POR TICKER (neutralizadas si alcance = «solo colectivo») ──
    umbral = _num(g("Umbral ROI (%) del ticker"), 10.0) if (tk_on and _yes(g("Cerrar si Umbral ROI ticker"))) else _NO_PROFIT
    stop = -abs(_num(g("Stop loss (%) del ticker"), 100.0)) if (tk_on and _yes(g("Cerrar si Stop loss ticker"))) else _NO_STOP

    filtro = str(g("Filtro confirmación 1ª vela") or "No filtrar").strip().lower()
    confirm = tk_on and filtro != "no filtrar"
    flip = confirm and ("flip" in filtro or "vuelta" in filtro)
    cut_weak = _yes(g("Cerrar si confirmación débil")) if confirm else True
    min_body = _num(g("Cuerpo mínimo anti-doji (%)"), 0.0) if confirm else 0.0

    # «Stop por pierna (%) (escenario)» — columna OPCIONAL (estudio refuerzo-vs-contra):
    # vende la pierna que toca −X% de SU capital y congela su valor; la posición sigue.
    # Vacía/ausente/0 → None (comportamiento de siempre). Condición POR TICKER → mismo
    # gate de «Alcance de salida» que umbral/stop del ticker.
    leg_stop = _num(g("Stop por pierna (%) (escenario)"), 0.0) if tk_on else 0.0

    # «Time-stop hora (escenario)» — columna OPCIONAL (protocolo GEX 2026-07-09): a esa hora,
    # si el ROI combinado va ≤ −20% (umbral por defecto de run_one), la posición corta ahí.
    # Vacía/ausente → off. Mismo gate de «Alcance de salida» que las condiciones por ticker.
    _ts_hora = (str(g("Time-stop hora (escenario)") or "").strip() or None) if tk_on else None

    run_kwargs = dict(
        inversion=float(seed.inversion),
        umbral_pct=float(umbral),
        stop_pct=float(stop),
        leg_stop_pct=(-abs(leg_stop) if leg_stop else None),
        time_stop_hora=_ts_hora,
        call_pct=float(seed.call_pct),
        selection_criterion=_selection(seed.criterio),
        entry_at_ask=e_ask, exit_at_bid=e_bid, nbbo_timeline=nbbo,
        search_window_min=float(seed.ventana_min),
        dte=int(dte), auto_dte=auto_dte,
        exit_hora=str(seed.salida),
        apply_refuerzo=_yes(g("Aplicar refuerzo")),
        refuerzo_loss_pct=_num(g("Umbral pérdida refuerzo (%)"), 50.0) / 100.0,
        refuerzo_max=int(_num(g("No. de veces a reforzar"), 2)),
        confirm_candle=confirm, confirm_min_body_pct=float(min_body),
        flip_on_wrong_direction=flip, cut_weak_confirmation=cut_weak,
    )

    # ── Salida COLECTIVA (None si alcance = «solo tickers») ──
    collective = None
    if col_on:
        profit = (_num(g("Umbral ROI colectivo (%)"), 0.0) / 100.0) if _yes(g("Cerrar si Umbral ROI colectivo")) else None
        cstop = (-abs(_num(g("Stop loss (%) colectivo"), 0.0)) / 100.0) if _yes(g("Cerrar si Stop loss colectivo")) else None
        if profit is not None or cstop is not None:
            collective = {"profit_frac": profit, "stop_frac": cstop}

    _tipo_esc = str(g("Tipo de operación (escenario)") or "").strip() or None
    return MappedScenario(id=sc.id, run_kwargs=run_kwargs, collective=collective,
                          alcance=alcance, tipo=_tipo_esc)

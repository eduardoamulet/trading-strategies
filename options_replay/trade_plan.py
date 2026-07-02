"""Plan de trading por rango de fechas — núcleo PURO del backtest dirigido por criterios (Fase 1).

Convierte los VEREDICTOS del análisis (bt_analysis → playbook JSON, o una matriz manual) en un
`TradePlan` normalizado y lo expande sobre un rango de fechas a filas del CONTRATO de iteración
del panel «Backtest de señales / iteraciones»: {Ticker, Fecha, Hora, Tipo, Estrategia}.

Costura del sistema: este módulo es un PRODUCTOR más de ese contrato (igual que los handoffs de
Alertas / Trading view y el editor manual) — todo lo de aguas abajo (runner, salidas colectivas,
render, export) se reutiliza sin cambios.

DIP: NO conoce Streamlit, Polygon ni el calendario bursátil. Recibe predicados inyectados:
`non_trading_reason(fecha) -> str | None` y `has_0dte(ticker, fecha) -> bool` (los helpers que ya
existen en app.py). Sin predicados, solo excluye fines de semana; el runner igualmente re-filtra.

Gate por ticker (día×ticker) = GATE DURO: si el playbook trae `por_ticker` y hay veredicto para
(día, ticker), ese veredicto MANDA sobre el de cartera (p. ej. SPY opera el Martes aunque la
cartera diga NO OPERAR — el patrón no es uniforme entre activos, validado con el detallado de
6 semanas). Sin entrada en el gate, decide la regla del día.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import pandas as pd

_WD_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]        # índice = date.weekday()
_PLAN_DAYS = ("Lun", "Mar", "Mié", "Jue", "Vie")
_ES_BY_EN = {"monday": "Lun", "tuesday": "Mar", "wednesday": "Mié",
             "thursday": "Jue", "friday": "Vie"}
_CANON = {"lun": "Lun", "mar": "Mar", "mie": "Mié", "mié": "Mié", "jue": "Jue", "vie": "Vie"}

DEFAULT_TIPO = "CALL y PUT"
DEFAULT_HORA = "09:30"


def _canon_day(d) -> Optional[str]:
    """«lunes» / «Mie» / «Mié» / «monday» → etiqueta canónica «Lun».. «Vie» (None si no es día de plan)."""
    s = str(d or "").strip().lower()
    return _ES_BY_EN.get(s) or _CANON.get(s[:3])


@dataclass(frozen=True)
class Rule:
    """Veredicto normalizado. En reglas de DÍA, tipo/hora vienen del playbook (o defaults); en el
    gate por ticker quedan en None (el gate solo decide `operar`; tipo/hora los pone el día)."""
    operar: bool
    scenario: str = ""
    motivo: str = ""
    tipo: Optional[str] = None
    hora: Optional[str] = None


@dataclass
class TradePlan:
    """Criterio normalizado: regla por día (Lun..Vie) + gate opcional por (día, ticker)."""
    days: dict[str, Rule]
    ticker_gate: Optional[dict[str, dict[str, Rule]]] = None      # {día: {TICKER: Rule}}
    source: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class BuildResult:
    """Salida del generador: filas del contrato + descartes con motivo + resumen + avisos."""
    rows: list[dict]
    discarded: list[dict]
    summary: dict
    warnings: list[str]


# ── Adaptadores de criterio (Strategy: cada fuente construye el MISMO TradePlan) ─────────────────
def plan_from_playbook(pj: dict, *, use_ticker_gate: bool = True) -> TradePlan:
    """Plan desde el playbook JSON del análisis (bt_analysis → botón «Playbook JSON»).

    `pj` es el dict tal cual se descarga: claves de día «Monday».. «Friday» (acepta también
    «Lun».. «Vie») + opcional `por_ticker` = {día: {ticker: veredicto}}. Con `use_ticker_gate`
    el veredicto día×ticker MANDA sobre el del día (gate duro).
    Levanta ValueError si el playbook no tiene desglose por día (playbook de nivel ESCENARIO,
    generado desde un results AGREGADO)."""
    days: dict[str, Rule] = {}
    for k, v in (pj or {}).items():
        es = _canon_day(k)
        if not es or not isinstance(v, dict):
            continue
        days[es] = Rule(
            operar=str(v.get("recommendation") or "").strip().upper() == "OPERAR",
            scenario=str(v.get("scenario") or ""),
            motivo=str(v.get("reason") or ""),
            tipo=str(v.get("operation") or DEFAULT_TIPO),
            hora=str(v.get("entry") or DEFAULT_HORA),
        )
    if not days:
        raise ValueError(
            "El playbook no tiene desglose por día (es de nivel ESCENARIO: el results era "
            "AGREGADO). Interpretá un results DETALLADO (1 fila por ticker×día) y usá ese playbook.")
    for es in _PLAN_DAYS:
        days.setdefault(es, Rule(False, motivo="sin datos en el playbook"))

    gate: Optional[dict[str, dict[str, Rule]]] = None
    if use_ticker_gate and isinstance(pj.get("por_ticker"), dict):
        gate = {}
        for dk, tks in pj["por_ticker"].items():
            es = _canon_day(dk)
            if not es or not isinstance(tks, dict):
                continue
            for tk, v in tks.items():
                if not isinstance(v, dict):
                    continue
                gate.setdefault(es, {})[str(tk).upper().strip()] = Rule(
                    operar=str(v.get("recommendation") or "").strip().upper() == "OPERAR",
                    scenario=str(v.get("scenario") or ""),
                    motivo="veredicto día×ticker")
        gate = gate or None

    plan = TradePlan(days=days, ticker_gate=gate, source="playbook")
    _warn_mixed_scenarios(plan)
    return plan


def plan_from_manual(dias, *, tipo: str = DEFAULT_TIPO, hora: str = DEFAULT_HORA,
                     por_ticker: Optional[dict] = None) -> TradePlan:
    """Plan manual (sin análisis previo): `dias` = iterable de días habilitados («Lun».. «Vie»,
    acepta «mie»/«lunes»/«monday»…) o dict {día: bool}. `por_ticker` opcional = {día: {ticker:
    bool}} como gate duro por (día, ticker)."""
    if isinstance(dias, dict):
        enabled = {_canon_day(k) for k, v in dias.items() if v}
    else:
        enabled = {_canon_day(d) for d in (dias or [])}
    enabled.discard(None)
    days = {es: Rule(es in enabled, tipo=tipo, hora=hora,
                     motivo="habilitado manualmente" if es in enabled else "día no habilitado")
            for es in _PLAN_DAYS}
    gate: Optional[dict[str, dict[str, Rule]]] = None
    if por_ticker:
        gate = {}
        for dk, tks in por_ticker.items():
            es = _canon_day(dk)
            if not es:
                continue
            for tk, ok in (tks or {}).items():
                gate.setdefault(es, {})[str(tk).upper().strip()] = Rule(bool(ok), motivo="manual")
        gate = gate or None
    return TradePlan(days=days, ticker_gate=gate, source="manual")


def _warn_mixed_scenarios(plan: TradePlan) -> None:
    """Los veredictos OPERAR pueden venir de escenarios DISTINTOS; las condiciones de salida del
    panel son POR CORRIDA (no por fila) → avisar para que el usuario elija cuál validar."""
    scen = {r.scenario for r in plan.days.values() if r.operar and r.scenario}
    if plan.ticker_gate:
        scen |= {r.scenario for tks in plan.ticker_gate.values() for r in tks.values()
                 if r.operar and r.scenario}
    if len(scen) > 1:
        plan.warnings.append(
            "Los veredictos OPERAR usan escenarios distintos (" + ", ".join(sorted(scen)) +
            "): las CONDICIONES DE SALIDA del panel aplican a TODA la corrida — configurá las del "
            "escenario que quieras validar.")


# ── Generador: plan × rango de fechas → filas del contrato de iteración ──────────────────────────
def build_iterations(plan: TradePlan, date_start, date_end, tickers: Iterable[str], *,
                     non_trading_reason: Optional[Callable[[str], Optional[str]]] = None,
                     has_0dte: Optional[Callable[[str, str], bool]] = None,
                     soft_cap: int = 300) -> BuildResult:
    """Expande el plan sobre [date_start, date_end] (ambos inclusive) → filas {Ticker, Fecha,
    Hora, Tipo, Estrategia} listas para sembrar `bt_iters`.

    Orden de filtros por (día, ticker): fin de semana (silencioso, no es candidato) → sesión
    (predicado, p. ej. feriado) → criterio del plan (gate día×ticker manda; si no, el día) →
    0DTE (predicado; dejalo en None con Auto-DTE/DTE=1). `soft_cap` NO trunca: solo agrega un
    aviso — decide la UI. Levanta ValueError si las fechas no parsean."""
    tks = list(dict.fromkeys(str(t).upper().strip() for t in (tickers or []) if str(t).strip()))
    warnings = list(plan.warnings)
    rows: list[dict] = []
    disc: list[dict] = []
    counts = {"generadas": 0, "criterio": 0, "sin_0dte": 0, "sin_sesion": 0, "dias_habiles": 0}
    try:
        d0, d1 = pd.Timestamp(date_start).normalize(), pd.Timestamp(date_end).normalize()
    except Exception as e:
        raise ValueError(f"Rango de fechas inválido: {date_start!r} → {date_end!r}") from e
    if pd.isna(d0) or pd.isna(d1):
        raise ValueError(f"Rango de fechas inválido: {date_start!r} → {date_end!r}")
    if not tks:
        warnings.append("Sin tickers: no se generaron iteraciones.")
        return BuildResult(rows, disc, counts, warnings)
    if d1 < d0:
        warnings.append(f"Rango invertido ({d0.date()} → {d1.date()}): no se generaron iteraciones.")
        return BuildResult(rows, disc, counts, warnings)

    for ts in pd.date_range(d0, d1, freq="D"):
        wd = ts.weekday()
        if wd >= 5:
            continue                                   # fin de semana: no es candidato
        fecha, dia = ts.strftime("%Y-%m-%d"), _WD_ES[wd]
        counts["dias_habiles"] += 1
        ntr = non_trading_reason(fecha) if non_trading_reason else None
        if ntr:
            counts["sin_sesion"] += len(tks)
            disc.extend({"Ticker": t, "Fecha": fecha, "Día": dia,
                         "Motivo": f"sin sesión: {ntr}"} for t in tks)
            continue
        day_rule = plan.days.get(dia) or Rule(False, motivo="sin regla para el día")
        for tk in tks:
            rule, origen = _resolve(plan, dia, tk, day_rule)
            if not rule.operar:
                counts["criterio"] += 1
                quien = "cartera" if origen == "dia" else tk
                disc.append({"Ticker": tk, "Fecha": fecha, "Día": dia,
                             "Motivo": f"criterio {dia} ({quien}): NO OPERAR"
                                       + (f" — {rule.motivo}" if rule.motivo else "")})
                continue
            if has_0dte is not None and not has_0dte(tk, fecha):
                counts["sin_0dte"] += 1
                disc.append({"Ticker": tk, "Fecha": fecha, "Día": dia, "Motivo": "sin 0DTE ese día"})
                continue
            scen = rule.scenario or day_rule.scenario
            rows.append({"Ticker": tk, "Fecha": fecha,
                         "Hora": day_rule.hora or DEFAULT_HORA,
                         "Tipo": day_rule.tipo or DEFAULT_TIPO,
                         "Estrategia": f"Plan {scen}" if scen else "Plan rango"})
            counts["generadas"] += 1

    if soft_cap and len(rows) > soft_cap:
        warnings.append(f"{len(rows)} iteraciones supera el tope blando de {soft_cap} — la corrida "
                        "puede tardar; revisá el rango/tickers antes de correr.")
    return BuildResult(rows, disc, counts, warnings)


def _resolve(plan: TradePlan, dia: str, ticker: str, day_rule: Rule) -> tuple[Rule, str]:
    """Regla efectiva para (día, ticker): el gate día×ticker MANDA si tiene entrada; si no, la del
    día. Devuelve (regla, origen) con origen ∈ {"ticker", "dia"} para el motivo del descarte."""
    if plan.ticker_gate:
        r = (plan.ticker_gate.get(dia) or {}).get(ticker)
        if r is not None:
            return r, "ticker"
    return day_rule, "dia"

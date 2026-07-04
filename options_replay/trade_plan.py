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
    gate por ticker quedan en None (el gate solo decide `operar`; tipo/hora los pone el día).
    `config` = condiciones del ESCENARIO (dict canónico del playbook, presente solo si el análisis
    corrió con template) — la UI las muestra y puede re-aplicarlas al panel."""
    operar: bool
    scenario: str = ""
    motivo: str = ""
    tipo: Optional[str] = None
    hora: Optional[str] = None
    config: Optional[dict] = None


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
            config=v.get("config") if isinstance(v.get("config"), dict) else None,
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
                    motivo="veredicto día×ticker",
                    config=v.get("config") if isinstance(v.get("config"), dict) else None)
        gate = gate or None

    plan = TradePlan(days=days, ticker_gate=gate, source="playbook")
    _warn_mixed_scenarios(plan)
    return plan


def pj_from_saved_playbook(pb: dict) -> dict:
    """Playbook PERSISTIDO (data/playbook.json — claves «Lun»..«Vie», con estados de la máquina
    de supervivencia) → dict con la FORMA del playbook JSON del análisis (claves «Monday»..,
    `por_ticker` anidado), para reutilizar `plan_from_playbook` y la UI del generador sin otra
    rama.

    GATE DE ESTADO: un día OPERAR pero NO-operable (candidato/suspendido) queda **NO OPERAR**
    (motivo = estado_motivo), y sus veredictos día×ticker se DESCARTAN — el estado es una
    protección de CARTERA (kill-switch, calibración, churn); sin esto el gate fino re-abriría
    un día suspendido. Playbooks viejos sin `estado` = comportamiento anterior (solo
    recommendation). Levanta ValueError sin playbook o sin per_day."""
    if not pb or not isinstance(pb.get("per_day"), dict) or not pb["per_day"]:
        raise ValueError("No hay playbook guardado con veredictos por día — generá uno en "
                         "Herramientas → Playbook (o corré update_playbook.py).")
    en_by_es = {"Lun": "Monday", "Mar": "Tuesday", "Mié": "Wednesday",
                "Jue": "Thursday", "Vie": "Friday"}
    out: dict = {}
    bloqueados: set = set()                    # días OPERAR frenados por el estado
    for es, i in pb["per_day"].items():
        en = en_by_es.get(str(es))
        if not en or not isinstance(i, dict):
            continue
        rec = str(i.get("recommendation") or "").strip().upper()
        estado = i.get("estado")
        ok = rec == "OPERAR" and estado in (None, "operable")
        # Bloquean su gate día×ticker: los OPERAR frenados por estado Y todo día SUSPENDIDO
        # (kill-switch/calibración/churn son protecciones de CARTERA — el gate fino no las
        # puentea). Un NO OPERAR simple o candidato conserva el gate fino (comportamiento
        # validado: SPY puede operar el martes aunque la cartera no).
        if (rec == "OPERAR" and not ok) or estado == "suspendido":
            bloqueados.add(str(es))
        out[en] = {
            "recommendation": "OPERAR" if ok else "NO OPERAR",
            "scenario": i.get("scenario"),
            "reason": ((f"estado {estado}: {i.get('estado_motivo') or ''}".strip()
                        if (rec == "OPERAR" and not ok) else i.get("reason")) or ""),
            "config": i.get("config") if isinstance(i.get("config"), dict) else None,
            "win_rate": i.get("win_rate"), "expected_roi": i.get("avg_roi"),
            "sharpe": i.get("sharpe"), "n": i.get("n"), "estado": estado,
        }
    if not out:
        raise ValueError("El playbook guardado no tiene veredictos por día reconocibles.")
    por_tk: dict = {}
    for r in pb.get("por_ticker") or []:       # registros {Ticker, Día, Recomendación, …}
        es = _canon_day(r.get("Día"))
        tk = str(r.get("Ticker") or "").upper().strip()
        if not es or not tk or es in bloqueados:
            continue
        por_tk.setdefault(es, {})[tk] = {"recommendation": r.get("Recomendación"),
                                         "scenario": r.get("Escenario")}
    if por_tk:
        out["por_ticker"] = por_tk
    return out


def scenario_run_overrides(cfg: dict) -> dict:
    """PURO: condiciones canónicas de un escenario → parámetros del RUNNER para una fila — el
    ESPEJO de `ucbatch.scenario.map_scenario` (misma semántica: flags Sí/No, alcance con
    neutralización, sentinels «no aplica»), pero desde el dict canónico del playbook. Lo consume
    el modo «🗓 config por día»: cada fila corre con las condiciones del escenario de SU día de
    la semana, en UNA sola corrida. `sin_lookahead` NO se mapea (tampoco lo ejecuta el batch:
    no existe en run_one — la hora ya viene fijada en la fila)."""
    _NO_PROFIT, _NO_STOP = 100000.0, -100000.0
    al = str(cfg.get("alcance") or "tickers y colectivo").strip().lower()
    tk_on = not ("solo" in al and "colectivo" in al)   # «solo colectivo» → neutraliza ticker
    col_on = not ("solo" in al and "ticker" in al)     # «solo tickers» → sin colectivo

    def _onv(fk: str, vk: str):
        v = _fnum(cfg.get(vk))
        return v, bool(flag_on(cfg.get(fk), default=v is not None))

    v, on = _onv("ticker_roi_on", "ticker_roi")
    umbral = float(v if v is not None else 10.0) if (tk_on and on) else _NO_PROFIT
    v, on = _onv("ticker_stop_on", "ticker_stop")
    stop = -abs(float(v if v is not None else 100.0)) if (tk_on and on) else _NO_STOP

    filtro = str(cfg.get("filtro_confirmacion") or "No filtrar").strip().lower()
    confirm = bool(tk_on and filtro and filtro != "no filtrar")
    flip = confirm and ("flip" in filtro or "vuelta" in filtro)
    cut_weak = bool(flag_on(cfg.get("cerrar_confirmacion_debil"), default=True)) if confirm else True
    min_body = float(_fnum(cfg.get("cuerpo_min")) or 0.0) if confirm else 0.0

    collective = None
    if col_on:
        v, on = _onv("col_roi_on", "col_roi")
        profit = (float(v) / 100.0) if (on and v is not None) else None
        v, on = _onv("col_stop_on", "col_stop")
        cstop = (-abs(float(v)) / 100.0) if (on and v is not None) else None
        if profit is not None or cstop is not None:
            collective = {"profit_frac": profit, "stop_frac": cstop}

    return {
        "umbral_pct": umbral, "stop_pct": stop,
        "apply_refuerzo": bool(flag_on(cfg.get("refuerzo"), default=False)),
        "refuerzo_loss_pct": float(_fnum(cfg.get("refuerzo_umbral")) or 50.0) / 100.0,
        "refuerzo_max": int(_fnum(cfg.get("refuerzo_n")) or 2),
        "confirm_candle": confirm, "confirm_min_body_pct": min_body,
        "flip_on_wrong_direction": flip, "cut_weak_confirmation": cut_weak,
        "collective": collective,
    }


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


def flag_on(v, default=None):
    """«Sí»/«No»/bool/número → bool. `default` si v es None o no interpretable."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("sí", "si", "true", "x", "✓", "yes", "on"):
        return True
    if s in ("no", "false", "", "—", "off"):
        return False
    try:
        return float(s) > 0
    except ValueError:
        return default


def _fnum(v):
    try:
        return float(str(v).replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def scenario_widget_values(cfg: dict, n_tickers: int) -> dict:
    """PURO: condiciones canónicas de un escenario → {clave de widget: valor} del panel — TODAS:
    salidas, refuerzo (martingala global: checkbox + umbral + veces) y sin-lookahead. RESPETA los
    flags Sí/No («Cerrar si …» / «Aplicar refuerzo»): el checkbox se setea también cuando el flag
    es No — APAGAR una condición es parte de aplicar el escenario (C061 apaga umbral y stop del
    ticker; C063 apaga además el ROI colectivo; C123 apaga el refuerzo). Los valores numéricos se
    setean siempre (quedan visibles aunque el checkbox esté off, como en el template). La UI
    vuelca este dict a session_state ANTES de instanciar los widgets."""
    out: dict = {}
    al = str(cfg.get("alcance") or "").strip().lower()
    if "solo" in al and "ticker" in al:
        out["sig_alcance"] = "Aplicar solo a tickers"
    elif "solo" in al and "colectivo" in al:
        out["sig_alcance"] = "Aplicar solo a colectivo"
    elif "ticker" in al and "colectivo" in al:
        out["sig_alcance"] = "Aplicar a tickers y colectivo"
    if "sig_alcance" in out:
        out["_sig_alcance_ntk"] = n_tickers        # que el default dinámico del panel no lo pise
    for fk, wk in (("ticker_roi_on", "sig_apply_umb"), ("ticker_stop_on", "sig_apply_stop"),
                   ("col_roi_on", "sig_coll_exit"), ("col_stop_on", "sig_coll_stop"),
                   ("cerrar_confirmacion_debil", "sig_cut_weak")):
        b = flag_on(cfg.get(fk))
        if b is not None:
            out[wk] = b
    for vk, wk, neg in (("ticker_roi", "sig_umb", False), ("ticker_stop", "sig_stop", True),
                        ("col_roi", "sig_coll_thr", False), ("col_stop", "sig_coll_stop_thr", True)):
        v = _fnum(cfg.get(vk))
        if v is not None:
            out[wk] = -abs(v) if neg else v
    v = _fnum(cfg.get("cuerpo_min"))
    if v is not None:
        out["sig_min_body"] = min(max(v, 0.0), 1.0)   # bounds del number_input del panel
    fc = str(cfg.get("filtro_confirmacion") or "").lower()
    if fc:
        out["sig_conf_mode"] = ("Dar vuelta (flip) si va en contra" if ("flip" in fc or "vuelta" in fc)
                                else "Cerrar si va en contra" if "cerrar" in fc else "No filtrar")
    # Refuerzo (martingala GLOBAL del panel: aplica al Tipo de cada fila) + sin-lookahead.
    b = flag_on(cfg.get("refuerzo"))
    if b is not None:
        out["sig_apply_ref"] = b
    v = _fnum(cfg.get("refuerzo_umbral"))
    if v is not None:
        out["sig_refuerzo"] = min(max(v, 1.0), 99.0)          # bounds del number_input
    v = _fnum(cfg.get("refuerzo_n"))
    if v is not None:
        out["sig_refuerzo_max"] = int(min(max(v, 1), 20))     # el widget es INT (float rompería)
    b = flag_on(cfg.get("sin_lookahead"))
    if b is not None:
        out["sig_no_lookahead"] = b
    return out


def scenario_config_summary(cfg: Optional[dict]) -> str:
    """String compacto de las condiciones RESPETANDO los flags: el valor si la condición está ON,
    «off» si está apagada (sin flag en la config → se muestra el valor, compat con playbooks
    viejos). Fuente ÚNICA de este formato: la usan el dropdown del panel y las tablas del análisis.
    Ej C061: «tickers y colectivo · ROI tk off · Stop tk off · ROI col 5% · Stop col −80% ·
    No filtrar · Refuerzo Sí (50% ×3)»."""
    if not cfg:
        return "—"
    def _g(v):
        f = _fnum(v)
        return f"{f:g}" if f is not None else str(v)
    parts = []
    if cfg.get("alcance"):
        parts.append(str(cfg["alcance"]))
    for fk, vk, lbl in (("ticker_roi_on", "ticker_roi", "ROI tk"),
                        ("ticker_stop_on", "ticker_stop", "Stop tk"),
                        ("col_roi_on", "col_roi", "ROI col"),
                        ("col_stop_on", "col_stop", "Stop col")):
        if cfg.get(vk) is None and cfg.get(fk) is None:
            continue
        on = flag_on(cfg.get(fk), default=True)
        parts.append(f"{lbl} {_g(cfg[vk])}%" if (on and cfg.get(vk) is not None) else f"{lbl} off")
    if cfg.get("filtro_confirmacion"):
        parts.append(str(cfg["filtro_confirmacion"]))
    ref = cfg.get("refuerzo")
    if ref is not None:
        if flag_on(ref, default=False):
            s = "Refuerzo Sí"
            if cfg.get("refuerzo_umbral") is not None:
                s += f" ({_g(cfg['refuerzo_umbral'])}%"
                s += f" ×{_g(cfg['refuerzo_n'])})" if cfg.get("refuerzo_n") is not None else ")"
            parts.append(s)
        else:
            parts.append("sin refuerzo")
    return " · ".join(parts) or "—"


def _resolve(plan: TradePlan, dia: str, ticker: str, day_rule: Rule) -> tuple[Rule, str]:
    """Regla efectiva para (día, ticker): el gate día×ticker MANDA si tiene entrada; si no, la del
    día. Devuelve (regla, origen) con origen ∈ {"ticker", "dia"} para el motivo del descarte."""
    if plan.ticker_gate:
        r = (plan.ticker_gate.get(dia) or {}).get(ticker)
        if r is not None:
            return r, "ticker"
    return day_rule, "dia"


# ── Fase 3: señales históricas REALES filtradas por el criterio del plan ─────────────────────────
def _norm_premarket_hora(hhmm) -> str:
    """Señales de antes de las 09:00 (pre-market) → entrada 09:30 (apertura), así el backtest 0DTE
    tiene datos. Misma regla que el handoff de Alertas en app.py (_norm_hora). Lo demás, igual."""
    s = str(hhmm or "").strip()
    try:
        h, m = s.split(":")[:2]
        if (int(h), int(m)) < (9, 0):
            return "09:30"
    except Exception:  # noqa: BLE001
        pass
    return s


def filter_signals(plan: TradePlan, signals, date_start, date_end, *,
                   tickers: Optional[Iterable[str]] = None,
                   non_trading_reason: Optional[Callable[[str], Optional[str]]] = None) -> BuildResult:
    """Filtra SEÑALES HISTÓRICAS reales (p. ej. la base de Alertas) por el criterio del plan dentro
    de [date_start, date_end] → filas del contrato `bt_iters`.

    A diferencia de `build_iterations` (plan sintético), acá cada señal CONSERVA su hora (con la
    normalización pre-market → 09:30), su tipo (CALL/PUT) y su metadata («% Cumpl.», Estrategia);
    el plan solo decide OPERAR sí/no por (día, ticker) — mismo gate duro.

    `signals`: iterable de dicts con symbol|ticker, fecha (YYYY-MM-DD), hora, tipo y opcionales
    probabilidad|prob, estrategia (el esquema de signals_db). `tickers` vacío/None = todos.
    Las señales FUERA del rango no son candidatas (no cuentan como descarte); el resumen trae
    señales_total / en_rango / otros_tickers / invalidas para no perder nada en silencio."""
    tks = {str(t).upper().strip() for t in (tickers or []) if str(t).strip()}
    warnings = list(plan.warnings)
    rows: list[dict] = []
    disc: list[dict] = []
    counts = {"generadas": 0, "criterio": 0, "sin_0dte": 0, "sin_sesion": 0,
              "señales_total": 0, "en_rango": 0, "otros_tickers": 0, "invalidas": 0}
    try:
        d0, d1 = pd.Timestamp(date_start).normalize(), pd.Timestamp(date_end).normalize()
    except Exception as e:
        raise ValueError(f"Rango de fechas inválido: {date_start!r} → {date_end!r}") from e
    if pd.isna(d0) or pd.isna(d1):
        raise ValueError(f"Rango de fechas inválido: {date_start!r} → {date_end!r}")
    if d1 < d0:
        warnings.append(f"Rango invertido ({d0.date()} → {d1.date()}): no se generaron iteraciones.")
        return BuildResult(rows, disc, counts, warnings)

    for s in signals or []:
        counts["señales_total"] += 1
        tk = str(s.get("symbol") or s.get("ticker") or "").upper().strip()
        fecha = str(s.get("fecha") or "").strip()[:10]
        try:
            f = pd.Timestamp(fecha).normalize()
        except Exception:  # noqa: BLE001
            f = pd.NaT
        if not tk or pd.isna(f):
            counts["invalidas"] += 1
            continue
        if not (d0 <= f <= d1):
            continue                                    # fuera del rango: no es candidata
        counts["en_rango"] += 1
        if tks and tk not in tks:
            counts["otros_tickers"] += 1
            continue
        dia = _WD_ES[f.weekday()]
        ntr = (non_trading_reason(fecha) if non_trading_reason
               else ("fin de semana" if f.weekday() >= 5 else None))
        if ntr:
            counts["sin_sesion"] += 1
            disc.append({"Ticker": tk, "Fecha": fecha, "Día": dia, "Motivo": f"sin sesión: {ntr}"})
            continue
        day_rule = plan.days.get(dia) or Rule(False, motivo="sin regla para el día")
        rule, origen = _resolve(plan, dia, tk, day_rule)
        if not rule.operar:
            counts["criterio"] += 1
            quien = "cartera" if origen == "dia" else tk
            disc.append({"Ticker": tk, "Fecha": fecha, "Día": dia,
                         "Motivo": f"criterio {dia} ({quien}): NO OPERAR"
                                   + (f" — {rule.motivo}" if rule.motivo else "")})
            continue
        rows.append({"Ticker": tk, "Fecha": fecha, "Hora": _norm_premarket_hora(s.get("hora")),
                     "Tipo": str(s.get("tipo") or "").upper().strip(),
                     "% Cumpl.": s.get("probabilidad", s.get("prob")),
                     "Estrategia": str(s.get("estrategia") or "")})
        counts["generadas"] += 1

    rows.sort(key=lambda r: (r["Fecha"], r["Hora"], r["Ticker"]))
    disc.sort(key=lambda r: (r["Fecha"], r["Ticker"]))
    return BuildResult(rows, disc, counts, warnings)

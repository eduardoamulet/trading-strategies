"""Playbook final + JSON consumible desde Python (formato del PRD, por día de la semana).

Si hay análisis por día → playbook por día con OPERAR/NO OPERAR. Si no (granularidad agregada sin
DOW) → recomendación a nivel escenario. Nunca fuerza un ganador por día si no hay ventaja.
"""
from __future__ import annotations

import json

_EN_DAY = {"Lun": "Monday", "Mar": "Tuesday", "Mié": "Wednesday", "Jue": "Thursday", "Vie": "Friday"}


def _num(v):
    """Escalar numpy/NaN → float nativo o None (JSON limpio, sin `NaN` inválido)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Condiciones de un escenario (nombres CANÓNICOS de loader._SCENARIO_RENAME) que van al playbook:
# son las que explican POR QUÉ esa config es la del día y las que el panel puede re-aplicar.
_COND_COLS = ["alcance", "ticker_roi_on", "ticker_roi", "ticker_stop_on", "ticker_stop",
              "filtro_confirmacion", "cerrar_confirmacion_debil", "cuerpo_min",
              "col_roi_on", "col_roi", "col_stop_on", "col_stop",
              "refuerzo", "refuerzo_umbral", "refuerzo_n", "sin_lookahead"]


def _scenario_configs(report: dict) -> dict:
    """{id: {condición: valor}} desde report['joined'] (results ⋈ template). Vacío si el análisis
    corrió SIN template: las condiciones viven en la hoja «Backtesting scenarios», sin ella el
    playbook solo puede nombrar el ID del escenario."""
    j = report.get("joined")
    if j is None or getattr(j, "empty", True) or "id" not in getattr(j, "columns", []):
        return {}
    cols = [c for c in _COND_COLS if c in j.columns]
    if not cols:
        return {}
    out: dict = {}
    for _, r in j.drop_duplicates(subset="id").iterrows():
        cfg = {}
        for c in cols:
            v = r[c]
            if v is None or v != v:
                continue
            if isinstance(v, str):
                cfg[c] = v.strip()
            else:
                f = _num(v)
                cfg[c] = f if f is not None else str(v).strip()
        if cfg:
            out[str(r["id"]).strip()] = cfg
    return out


def build_playbook(report: dict) -> dict:
    seed = report.get("seed", {})
    entry = seed.get("Horario de entrada", "09:30")
    exit_ = seed.get("Horario de salida", "13:55")
    op = seed.get("Tipo de operacion", "CALL y PUT")
    tickers = seed.get("Tickers", "")
    dow = report.get("dow", {})
    pb = {"entrada_fija": entry, "salida_fija": exit_, "operacion": op, "tickers": tickers, "dias": {}}
    if dow.get("available"):
        cfgs = _scenario_configs(report)       # {} si el análisis corrió sin template
        for dia, info in dow.get("per_day", {}).items():
            cfg = cfgs.get(str(info.get("scenario") or "").strip())
            pb["dias"][dia] = {**info, "entry": entry, "exit": exit_, "operation": op,
                               **({"config": cfg} if cfg else {})}
        pb["nivel"] = ("día de la semana · CARTERA (ROI colectivo Σg/Σinv)"
                       if dow.get("level") == "cartera" else "día de la semana")
        # Desglose día×ticker (si el análisis lo trae): va al JSON para que el backtest por rango
        # pueda usar el veredicto FINO como gate (p. ej. SPY opera el Martes aunque la cartera no).
        dt = report.get("dow_ticker")
        if dt is not None and getattr(dt, "empty", True) is False:
            pt: dict = {}
            for _, r in dt.iterrows():
                e = {
                    "scenario": str(r.get("Escenario") or ""),
                    "win_rate": _num(r.get("Win Rate %")),
                    "expected_roi": _num(r.get("ROI %")),
                    "sharpe": _num(r.get("Sharpe")),
                    "n": _int(r.get("n")),
                    "recommendation": str(r.get("Recomendación") or ""),
                }
                cfg = cfgs.get(e["scenario"])
                if cfg:
                    e["config"] = cfg
                pt.setdefault(str(r.get("Día")), {})[str(r.get("Ticker"))] = e
            pb["por_ticker"] = pt
    else:
        pb["nivel"] = "escenario (sin desglose por día)"
        pb["dow_reason"] = dow.get("reason", "")
        best = report.get("best")
        if best is not None:
            roi = float(best.get("roi") or 0)
            operar = best.get("tier") in ("Excelente", "Muy Bueno") and roi > 0
            pb["mejor_escenario"] = {
                "scenario": best["id"], "robustness": float(best.get("robustness") or 0),
                "tier": best.get("tier"), "roi": round(roi, 2),
                "win_rate": round(float(best.get("win_rate") or 0), 1),
                "recommendation": "OPERAR" if operar else "NO OPERAR",
            }
    return pb


def to_json(pb: dict) -> dict:
    """JSON por día (formato del PRD). Consumible desde Python para automatizar la selección diaria."""
    out: dict = {}
    for dia, info in pb.get("dias", {}).items():
        out[_EN_DAY.get(dia, dia)] = {
            "scenario": info.get("scenario"),
            "ticker": pb.get("tickers"),
            "entry": info.get("entry"),
            "exit": info.get("exit"),
            "operation": info.get("operation"),
            "expected_roi": info.get("avg_roi"),          # detailed (%)
            "expected_avg_usd": info.get("avg_usd"),      # file DOW ($)
            "win_rate": info.get("win_rate"),
            "profit_factor": info.get("profit_factor"),
            "sharpe": info.get("sharpe"),
            "n": info.get("n"),
            "recommendation": info.get("recommendation"),
        }
        if info.get("config"):
            out[_EN_DAY.get(dia, dia)]["config"] = info["config"]
    if pb.get("por_ticker"):
        out["por_ticker"] = {_EN_DAY.get(d, d): tks for d, tks in pb["por_ticker"].items()}
    if not out and pb.get("mejor_escenario"):
        out["_scenario_level"] = pb["mejor_escenario"]
    return out


def json_str(pb: dict) -> str:
    return json.dumps(to_json(pb), indent=2, ensure_ascii=False)

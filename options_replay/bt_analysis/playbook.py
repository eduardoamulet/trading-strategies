"""Playbook final + JSON consumible desde Python (formato del PRD, por día de la semana).

Si hay análisis por día → playbook por día con OPERAR/NO OPERAR. Si no (granularidad agregada sin
DOW) → recomendación a nivel escenario. Nunca fuerza un ganador por día si no hay ventaja.
"""
from __future__ import annotations

import json

_EN_DAY = {"Lun": "Monday", "Mar": "Tuesday", "Mié": "Wednesday", "Jue": "Thursday", "Vie": "Friday"}


def build_playbook(report: dict) -> dict:
    seed = report.get("seed", {})
    entry = seed.get("Horario de entrada", "09:30")
    exit_ = seed.get("Horario de salida", "13:55")
    op = seed.get("Tipo de operacion", "CALL y PUT")
    tickers = seed.get("Tickers", "")
    dow = report.get("dow", {})
    pb = {"entrada_fija": entry, "salida_fija": exit_, "operacion": op, "tickers": tickers, "dias": {}}
    if dow.get("available"):
        for dia, info in dow.get("per_day", {}).items():
            pb["dias"][dia] = {**info, "entry": entry, "exit": exit_, "operation": op}
        pb["nivel"] = "día de la semana"
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
    if not out and pb.get("mejor_escenario"):
        out["_scenario_level"] = pb["mejor_escenario"]
    return out


def json_str(pb: dict) -> str:
    return json.dumps(to_json(pb), indent=2, ensure_ascii=False)

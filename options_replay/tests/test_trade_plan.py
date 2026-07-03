"""Unit tests para trade_plan — plan por rango de fechas dirigido por criterios (Fase 1).

Headless y PURO: los predicados de calendario/0DTE son fakes inyectados (sin Streamlit/Polygon).
Semana de referencia: 2026-03-02 (Lun) … 2026-03-06 (Vie) — la misma del detallado de 6 semanas.
Corré:  pytest tests/test_trade_plan.py -q
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
from trade_plan import (build_iterations, filter_signals, plan_from_manual,  # noqa: E402
                        plan_from_playbook, scenario_config_summary, scenario_widget_values)
from bt_analysis import playbook as pbk  # noqa: E402

# Playbook JSON como lo produce bt_analysis (claves EN) — refleja el resultado real de 6 semanas:
# cartera OPERAR Lun/Mié/Jue; el gate día×ticker habilita SPY el Martes y QQQ/IWM el Viernes.
PJ = {
    "Monday": {"scenario": "C061", "entry": "09:30", "exit": "13:55",
               "operation": "CALL y PUT", "win_rate": 100.0, "recommendation": "OPERAR"},
    "Tuesday": {"scenario": "C063", "entry": "09:30", "exit": "13:55",
                "operation": "CALL y PUT", "win_rate": 50.0, "recommendation": "NO OPERAR"},
    "Wednesday": {"scenario": "C041", "entry": "09:30", "exit": "13:55",
                  "operation": "CALL y PUT", "win_rate": 100.0, "recommendation": "OPERAR"},
    "Thursday": {"scenario": "C001", "entry": "09:30", "exit": "13:55",
                 "operation": "CALL y PUT", "win_rate": 100.0, "recommendation": "OPERAR"},
    "Friday": {"scenario": "C123", "entry": "09:30", "exit": "13:55",
               "operation": "CALL y PUT", "win_rate": 50.0, "recommendation": "NO OPERAR"},
    "por_ticker": {
        "Tuesday": {"SPY": {"scenario": "C003", "recommendation": "OPERAR"},
                    "QQQ": {"scenario": "C143", "recommendation": "NO OPERAR"}},
        "Friday": {"SPY": {"scenario": "C123", "recommendation": "NO OPERAR"},
                   "QQQ": {"scenario": "C003", "recommendation": "OPERAR"},
                   "IWM": {"scenario": "C083", "recommendation": "OPERAR"}},
    },
}
TKS = ["QQQ", "SPY", "IWM"]
LUN, MAR, MIE, VIE = "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-06"


# ── Adaptador playbook ────────────────────────────────────────────────────────
def test_plan_from_playbook_dias():
    p = plan_from_playbook(PJ)
    assert [p.days[d].operar for d in ("Lun", "Mar", "Mié", "Jue", "Vie")] == \
        [True, False, True, True, False]
    assert p.days["Lun"].scenario == "C061"
    assert p.days["Lun"].tipo == "CALL y PUT" and p.days["Lun"].hora == "09:30"
    assert p.ticker_gate["Mar"]["SPY"].operar and not p.ticker_gate["Mar"]["QQQ"].operar


def test_plan_from_playbook_nivel_escenario_lanza():
    with pytest.raises(ValueError, match="desglose por día"):
        plan_from_playbook({"_scenario_level": {"scenario": "C032", "recommendation": "OPERAR"}})


def test_warning_escenarios_mixtos_propaga():
    p = plan_from_playbook(PJ)
    assert any("escenarios distintos" in w for w in p.warnings)
    res = build_iterations(p, LUN, LUN, ["QQQ"])
    assert any("escenarios distintos" in w for w in res.warnings)


# ── Gate día×ticker (duro) ────────────────────────────────────────────────────
def test_gate_manda_sobre_cartera():
    # Martes: cartera NO OPERAR, pero el gate habilita SPY (escenario C003).
    res = build_iterations(plan_from_playbook(PJ), MAR, MAR, TKS)
    assert [r["Ticker"] for r in res.rows] == ["SPY"]
    assert res.rows[0]["Estrategia"] == "Plan C003"
    motivos = {d["Ticker"]: d["Motivo"] for d in res.discarded}
    assert "QQQ" in motivos["QQQ"] and "cartera" in motivos["IWM"]   # QQQ por gate, IWM por día


def test_sin_gate_respeta_cartera():
    res = build_iterations(plan_from_playbook(PJ, use_ticker_gate=False), MAR, MAR, TKS)
    assert res.rows == [] and res.summary["criterio"] == 3


# ── Semana completa: conteos y fin de semana ──────────────────────────────────
def test_semana_completa_conteos():
    # Lun 3 + Mar 1 (SPY gate) + Mié 3 + Jue 3 + Vie 2 (QQQ/IWM gate) = 12; el rango incluye
    # el fin de semana (07/08) que se excluye en silencio (no cuenta como descarte).
    res = build_iterations(plan_from_playbook(PJ), LUN, "2026-03-08", TKS)
    assert res.summary == {"generadas": 12, "criterio": 3, "sin_0dte": 0,
                           "sin_sesion": 0, "dias_habiles": 5}
    assert len(res.rows) == 12 and len(res.discarded) == 3
    assert all(pd.Timestamp(r["Fecha"]).weekday() < 5 for r in res.rows)


def test_feriado_descarta_dia_completo():
    res = build_iterations(plan_from_playbook(PJ), LUN, VIE, TKS,
                           non_trading_reason=lambda f: "feriado" if f == MIE else None)
    assert res.summary["sin_sesion"] == 3
    assert not any(r["Fecha"] == MIE for r in res.rows)
    assert res.summary["generadas"] == 9        # 12 − los 3 del miércoles


def test_sin_0dte_descarta_ticker():
    res = build_iterations(plan_from_playbook(PJ), LUN, VIE, TKS,
                           has_0dte=lambda tk, f: tk != "IWM")
    assert not any(r["Ticker"] == "IWM" for r in res.rows)
    # IWM pasaba el criterio Lun/Mié/Jue (cartera) + Vie (gate) = 4 descartes por 0DTE.
    assert res.summary["sin_0dte"] == 4 and res.summary["generadas"] == 8


# ── Contrato de fila (golden) ─────────────────────────────────────────────────
def test_fila_contrato_golden():
    res = build_iterations(plan_from_playbook(PJ), LUN, LUN, ["qqq "])
    assert res.rows == [{"Ticker": "QQQ", "Fecha": "2026-03-02", "Hora": "09:30",
                         "Tipo": "CALL y PUT", "Estrategia": "Plan C061"}]


# ── Bordes del rango / entradas inválidas ─────────────────────────────────────
def test_rango_invertido_y_sin_tickers():
    p = plan_from_playbook(PJ)
    inv = build_iterations(p, VIE, LUN, TKS)
    assert inv.rows == [] and any("invertido" in w.lower() for w in inv.warnings)
    vac = build_iterations(p, LUN, VIE, [])
    assert vac.rows == [] and any("Sin tickers" in w for w in vac.warnings)
    with pytest.raises(ValueError, match="inválido"):
        build_iterations(p, "no-es-fecha", VIE, TKS)


def test_soft_cap_avisa_sin_truncar():
    res = build_iterations(plan_from_playbook(PJ), LUN, "2026-03-08", TKS, soft_cap=5)
    assert len(res.rows) == 12                       # NO trunca
    assert any("tope blando" in w for w in res.warnings)


# ── Plan manual ───────────────────────────────────────────────────────────────
def test_plan_manual_dias_y_normalizacion():
    p = plan_from_manual(["mie", "Lunes", "friday"], tipo="Sólo CALL", hora="10:00")
    assert [p.days[d].operar for d in ("Lun", "Mar", "Mié", "Jue", "Vie")] == \
        [True, False, True, False, True]
    res = build_iterations(p, LUN, VIE, ["SPY"])
    assert [r["Fecha"] for r in res.rows] == [LUN, MIE, VIE]
    assert res.rows[0]["Tipo"] == "Sólo CALL" and res.rows[0]["Hora"] == "10:00"
    assert res.rows[0]["Estrategia"] == "Plan rango"     # manual: sin escenario


def test_plan_manual_por_ticker():
    p = plan_from_manual(["Lun"], por_ticker={"Lun": {"IWM": False}})
    res = build_iterations(p, LUN, LUN, TKS)
    assert sorted(r["Ticker"] for r in res.rows) == ["QQQ", "SPY"]


# ── Fase 3: señales históricas filtradas por el criterio ─────────────────────
def _sigs_fixture():
    """Señales estilo signals_db (symbol/fecha/hora/tipo/probabilidad/estrategia)."""
    return [
        {"symbol": "SPY", "fecha": MAR, "hora": "10:15", "tipo": "CALL",
         "probabilidad": 82, "estrategia": "IA"},                       # Mar: gate SPY ✅
        {"symbol": "QQQ", "fecha": MAR, "hora": "11:00", "tipo": "PUT"},   # Mar: gate QQQ ❌
        {"symbol": "IWM", "fecha": MAR, "hora": "11:05", "tipo": "CALL"},  # Mar: cartera ❌ (sin gate)
        {"symbol": "qqq", "fecha": LUN, "hora": "08:45", "tipo": "call"},  # Lun ✅ · pre-market → 09:30
        {"symbol": "SPY", "fecha": "2026-02-20", "hora": "10:00", "tipo": "CALL"},   # fuera de rango
        {"symbol": "SPY", "fecha": "2026-03-07", "hora": "10:00", "tipo": "CALL"},   # sábado
        {"symbol": "", "fecha": MAR, "hora": "10:00", "tipo": "CALL"},               # inválida
    ]


def test_filter_signals_gate_conserva_hora_tipo_y_metadata():
    res = filter_signals(plan_from_playbook(PJ), _sigs_fixture(), LUN, "2026-03-08")
    assert [(r["Ticker"], r["Fecha"]) for r in res.rows] == [("QQQ", LUN), ("SPY", MAR)]
    assert res.rows[0]["Hora"] == "09:30" and res.rows[0]["Tipo"] == "CALL"   # normalizadas
    spy = res.rows[1]
    assert spy["Hora"] == "10:15" and spy["% Cumpl."] == 82 and spy["Estrategia"] == "IA"
    # en_rango = 5: la inválida se detecta ANTES del chequeo de rango y la de 02-20 queda fuera.
    assert res.summary == {"generadas": 2, "criterio": 2, "sin_0dte": 0, "sin_sesion": 1,
                           "señales_total": 7, "en_rango": 5, "otros_tickers": 0, "invalidas": 1}
    assert any("QQQ" in d["Motivo"] for d in res.discarded if d["Ticker"] == "QQQ")


def test_filter_signals_tickers_vacio_es_todos_y_filtro():
    p = plan_from_playbook(PJ)
    solo_spy = filter_signals(p, _sigs_fixture(), LUN, "2026-03-08", tickers=["SPY"])
    assert [r["Ticker"] for r in solo_spy.rows] == ["SPY"]
    assert solo_spy.summary["otros_tickers"] == 3          # QQQ×2 + IWM×1 en rango
    inv = filter_signals(p, _sigs_fixture(), "2026-03-08", LUN)
    assert inv.rows == [] and any("invertido" in w.lower() for w in inv.warnings)


# ── Configs de escenario → widgets del panel (flags Sí/No del template) ───────
def _c5(**over):
    """Config base de los 5 ganadores (template real 2026): solo cambian los FLAGS."""
    base = {"refuerzo": "Sí", "refuerzo_umbral": 50.0, "refuerzo_n": 3.0, "sin_lookahead": "No",
            "alcance": "tickers y colectivo", "ticker_roi_on": "Sí", "ticker_roi": 10.0,
            "ticker_stop_on": "Sí", "ticker_stop": -80.0, "filtro_confirmacion": "No filtrar",
            "cerrar_confirmacion_debil": "No", "cuerpo_min": 0.05,
            "col_roi_on": "Sí", "col_roi": 5.0, "col_stop_on": "Sí", "col_stop": -80.0}
    return {**base, **over}


# La tabla del template: C001 todo ON · C041 sin umbral tk · C061 sin umbral/stop tk ·
# C063 solo stop colectivo · C123 sin refuerzo (umbral tk off, ROI col off).
C5 = {
    "C001": _c5(),
    "C041": _c5(ticker_roi_on="No"),
    "C061": _c5(ticker_roi_on="No", ticker_stop_on="No"),
    "C063": _c5(ticker_roi_on="No", ticker_stop_on="No", col_roi_on="No"),
    "C123": _c5(ticker_roi_on="No", col_roi_on="No", refuerzo="No"),
}


@pytest.mark.parametrize("cid,umb,stop,col_roi,col_stop,ref", [
    ("C001", True, True, True, True, True),
    ("C041", False, True, True, True, True),
    ("C061", False, False, True, True, True),
    ("C063", False, False, False, True, True),
    ("C123", False, True, False, True, False),
])
def test_scenario_widget_values_flags(cid, umb, stop, col_roi, col_stop, ref):
    v = scenario_widget_values(C5[cid], n_tickers=3)
    assert v["sig_apply_umb"] is umb and v["sig_apply_stop"] is stop
    assert v["sig_coll_exit"] is col_roi and v["sig_coll_stop"] is col_stop
    # Refuerzo (martingala global) + sin-lookahead también son parte del escenario.
    assert v["sig_apply_ref"] is ref
    assert v["sig_refuerzo"] == 50.0
    assert v["sig_refuerzo_max"] == 3 and isinstance(v["sig_refuerzo_max"], int)  # widget INT
    assert v["sig_no_lookahead"] is False
    # Los VALORES se setean siempre (visibles aunque el checkbox quede off, como en el template).
    assert v["sig_umb"] == 10.0 and v["sig_stop"] == -80.0
    assert v["sig_coll_thr"] == 5.0 and v["sig_coll_stop_thr"] == -80.0
    assert v["sig_alcance"] == "Aplicar a tickers y colectivo" and v["_sig_alcance_ntk"] == 3
    assert v["sig_conf_mode"] == "No filtrar" and v["sig_cut_weak"] is False
    assert v["sig_min_body"] == 0.05


def test_scenario_config_summary_respeta_flags():
    s61 = scenario_config_summary(C5["C061"])
    assert "ROI tk off" in s61 and "Stop tk off" in s61 and "ROI col 5%" in s61
    assert "Refuerzo Sí (50% ×3)" in s61
    s123 = scenario_config_summary(C5["C123"])
    assert "Stop tk -80%" in s123 and "ROI col off" in s123 and "sin refuerzo" in s123
    # compat: config vieja SIN flags → muestra los valores (no inventa «off»)
    assert "ROI tk 10%" in scenario_config_summary({"ticker_roi": 10.0})


# ── Roundtrip: análisis → playbook JSON → plan → filas ────────────────────────
def _report_fixture():
    per_day = {
        "Lun": {"scenario": "C061", "avg_roi": 13.3, "win_rate": 100.0, "sharpe": 2.1, "n": 5,
                "recommendation": "OPERAR", "reason": "ventaja"},
        "Mar": {"scenario": "C063", "avg_roi": 113.0, "win_rate": 50.0, "sharpe": 0.37, "n": 6,
                "recommendation": "NO OPERAR", "reason": "sin ventaja"},
    }
    dt = pd.DataFrame([
        {"Ticker": "SPY", "Día": "Mar", "Escenario": "C003", "Win Rate %": 100.0,
         "ROI %": 16.39, "Sharpe": 4.274, "n": 6, "Recomendación": "OPERAR"},
        {"Ticker": "QQQ", "Día": "Mar", "Escenario": "C143", "Win Rate %": 16.7,
         "ROI %": 150.44, "Sharpe": 0.279, "n": 6, "Recomendación": "NO OPERAR"},
    ])
    return {"seed": {"Horario de entrada": "09:30", "Horario de salida": "13:55",
                     "Tipo de operacion": "CALL y PUT", "Tickers": "QQQ, SPY, IWM"},
            "dow": {"available": True, "level": "cartera", "per_day": per_day},
            "dow_ticker": dt}


def test_playbook_json_incluye_por_ticker_y_es_serializable():
    pj = pbk.to_json(pbk.build_playbook(_report_fixture()))
    assert pj["Monday"]["recommendation"] == "OPERAR"
    spy = pj["por_ticker"]["Tuesday"]["SPY"]
    assert spy["recommendation"] == "OPERAR" and spy["scenario"] == "C003"
    assert isinstance(spy["win_rate"], float) and isinstance(spy["n"], int)   # nativos, no numpy
    json.dumps(pj)                                                            # JSON válido


def test_playbook_config_por_dia_y_rule_config():
    # Con template (report["joined"]): el playbook embebe las CONDICIONES del escenario por día
    # y por (día×ticker), y el plan las expone en Rule.config.
    rep = _report_fixture()
    rep["joined"] = pd.DataFrame([
        {"id": "C061", "alcance": "Aplicar a tickers y colectivo", "ticker_roi": 15.0,
         "ticker_stop": -80.0, "col_roi": 5.0, "col_stop": -80.0,
         "filtro_confirmacion": "Dar vuelta (flip) si va en contra"},
        {"id": "C003", "alcance": "Aplicar solo a tickers", "ticker_roi": 10.0},
    ])
    pj = pbk.to_json(pbk.build_playbook(rep))
    assert pj["Monday"]["config"]["ticker_roi"] == 15.0
    assert "config" not in pj["Tuesday"]                       # C063 no está en el template
    assert pj["por_ticker"]["Tuesday"]["SPY"]["config"]["alcance"] == "Aplicar solo a tickers"
    json.dumps(pj)                                             # sigue siendo JSON válido
    plan = plan_from_playbook(pj)
    assert plan.days["Lun"].config["col_roi"] == 5.0
    assert plan.ticker_gate["Mar"]["SPY"].config["ticker_roi"] == 10.0
    assert plan.days["Mar"].config is None


def test_playbook_sin_template_no_trae_config():
    pj = pbk.to_json(pbk.build_playbook(_report_fixture()))
    assert "config" not in pj["Monday"]
    assert plan_from_playbook(pj).days["Lun"].config is None


def test_roundtrip_analisis_a_filas():
    pj = pbk.to_json(pbk.build_playbook(_report_fixture()))
    res = build_iterations(plan_from_playbook(pj), MAR, MAR, ["SPY", "QQQ"])
    assert [r["Ticker"] for r in res.rows] == ["SPY"]        # el gate habilita SOLO SPY el martes
    assert res.rows[0]["Estrategia"] == "Plan C003"

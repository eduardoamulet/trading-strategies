"""Unit tests para playbook_store — persistencia + resolución de config por fecha.

Headless: usa rutas temporales (tmp_path); no toca data/playbook.json real ni lanza batches.
Corré:  pytest tests/test_playbook_store.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
import playbook_store as pbs  # noqa: E402

PB = {
    "evaluado_desde": "2026-04-01", "evaluado_hasta": "2026-06-24",
    "tickers": "QQQ, SPY, IWM",
    "per_day": {
        "Lun": {"scenario": "C081", "recommendation": "NO OPERAR", "config": {"col_roi": 5.0}},
        "Jue": {"scenario": "C102", "recommendation": "OPERAR",
                "config": {"ticker_roi_on": "Sí", "ticker_roi": 10.0},
                "config_txt": "ROI tk 10%"},
    },
}


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "playbook.json"
    pbs.save_playbook(PB, path=p)
    out = pbs.load_playbook(path=p)
    assert out["per_day"]["Jue"]["scenario"] == "C102"
    assert out["evaluado_desde"] == "2026-04-01"


def test_load_sin_archivo_devuelve_none(tmp_path):
    assert pbs.load_playbook(path=tmp_path / "no_existe.json") is None


def test_scenario_for_date_resuelve_por_dia_de_semana():
    # 2026-06-25 es JUEVES → C102 (OPERAR); 2026-06-22 es LUNES → C081 (NO OPERAR, el caller decide).
    jue = pbs.scenario_for_date(PB, "2026-06-25")
    assert jue["scenario"] == "C102" and jue["recommendation"] == "OPERAR"
    lun = pbs.scenario_for_date(PB, "2026-06-22")
    assert lun["scenario"] == "C081" and lun["recommendation"] == "NO OPERAR"
    assert pbs.scenario_for_date(PB, "2026-06-23") is None      # Martes: no está en el playbook
    assert pbs.scenario_for_date(PB, "2026-06-27") is None      # Sábado: día no cubierto
    assert pbs.scenario_for_date(None, "2026-06-25") is None    # sin playbook
    assert pbs.scenario_for_date(PB, "no-es-fecha") is None     # fecha inválida

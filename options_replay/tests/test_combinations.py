"""Unit tests: Combinaciones de Backtesting (import + producto cartesiano + adaptadores).

Headless: SQLite y Excel en tmp_path. Corré:  pytest tests/test_combinations.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
import combinations as cmb  # noqa: E402

# Valores por columna del Excel de variables de prueba: seed 1-valor + condiciones con
# 2 variables multi-valor (2 × 3 = 6 escenarios) y el resto single-valor.
_SEED_VALS = {
    "Tickers": ["QQQ", "SPY"], "Tipo de operacion": ["CALL y PUT"],
    "Inversión ($)": ["1000"], "Inversión CALL (%)": ["50"], "Inversión PUT (%)": ["50"],
    "Horario de entrada": ["09:30"], "Horario de salida": ["13:55"],
    "Ventana búsqueda contrato (max)": ["4"],
    "Criterio de selección de contrato": ["Menor spread en rango óptimo"],
    "Modelo de fills": ["NBBO por barra · triggers sobre el bid (Fase 2)"],
    "Granularidad temporal (segundos)": ["60"], "Vencimiento DTE": ["0 — mismo día"],
}
_COND_VALS = {
    "Alcance de salida": ["tickers y colectivo"],
    "Aplicar refuerzo": ["Sí", "No"],                       # 2 valores
    "Umbral pérdida refuerzo (%)": ["50"],
    "No. de veces a reforzar": ["2"],
    "Cerrar si Umbral ROI ticker": ["Sí"],
    "Umbral ROI (%) del ticker": ["5", "10", "15"],         # 3 valores
    "Cerrar si Stop loss ticker": ["Sí"],
    "Stop loss (%) del ticker": ["-70"],
    "Filtro confirmación 1ª vela": ["No filtrar"],
    "Cerrar si confirmación débil": ["SI"],
    "Cuerpo mínimo anti-doji (%)": ["0.05"],
    "Cerrar si Umbral ROI colectivo": ["Sí"],
    "Umbral ROI colectivo (%)": ["5"],
    "Cerrar si Stop loss colectivo": ["No"],
    "Stop loss (%) colectivo": ["-70"],
}


def _make_xlsx(tmp_path, *, drop_col: str | None = None, empty_col: str | None = None) -> Path:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = cmb.VARS_SHEET
    cols = {**_SEED_VALS}
    # las dos «Fecha inicial» (inicial y final) van por posición, como en el file real
    headers, values = [], []
    for h, v in list(cols.items())[:2]:
        headers.append(h); values.append(v)
    headers += ["Fecha inicial", "Fecha inicial"]
    values += [["2026–01–01"], ["2026–06–24"]]
    for h, v in list(cols.items())[2:]:
        headers.append(h); values.append(v)
    for h, v in _COND_VALS.items():
        if h == drop_col:
            continue
        headers.append(h); values.append([] if h == empty_col else v)
    ws.append(headers)
    n = max(len(v) for v in values)
    for i in range(n):
        ws.append([v[i] if i < len(v) else None for v in values])
    p = tmp_path / "combo_test.xlsx"
    wb.save(p)
    return p


def test_import_y_validacion(tmp_path):
    db = tmp_path / "c.db"
    reg = cmb.import_file(_make_xlsx(tmp_path), path=db)
    assert reg["nombre"] == "combo_test.xlsx" and reg["estado"] == "importada"
    assert reg["seed"]["tickers"] == ["QQQ", "SPY"]
    assert reg["seed"]["fecha_inicial"] == "2026-01-01"      # guión largo normalizado
    assert reg["seed"]["fecha_final"] == "2026-06-24"
    assert reg["variables"]["Aplicar refuerzo"] == ["Sí", "No"]
    assert cmb.expected_scenarios(reg) == 6                  # 2 × 3
    # Falta una columna de condiciones → estructura inválida.
    with pytest.raises(ValueError, match="faltan columnas"):
        cmb.import_file(_make_xlsx(tmp_path, drop_col="Umbral ROI (%) del ticker"), path=db)
    # Columna de condición VACÍA (p. ej. «solo tickers» deja el colectivo en blanco) es
    # legítima: cuenta como UN valor vacío → multiplicador 1 (los defaults del motor la apagan).
    reg_v = cmb.import_file(_make_xlsx(tmp_path, empty_col="Aplicar refuerzo"), path=db)
    assert reg_v["variables"]["Aplicar refuerzo"] == [""]
    assert cmb.expected_scenarios(reg_v) == 3                # 1 × 3 (el refuerzo dejó de variar)
    # No sobrescribe: cada importación = combinación NUEVA.
    reg2 = cmb.import_file(_make_xlsx(tmp_path), path=db)
    assert reg2["id"] != reg["id"] and len(cmb.list_combinations(path=db)) == 3


def test_generacion_producto_cartesiano(tmp_path):
    db = tmp_path / "c.db"
    reg = cmb.import_file(_make_xlsx(tmp_path), path=db)
    n = cmb.generate_scenarios(reg["id"], path=db)
    assert n == 6
    scens = cmb.scenarios_for_batch(reg["id"], path=db)
    assert [s.id for s in scens] == [f"C{i:03d}" for i in range(1, 7)]
    # Producto cartesiano completo: (refuerzo × umbral) = todas las 6 parejas, sin repetir.
    parejas = {(s.cond["Aplicar refuerzo"], s.cond["Umbral ROI (%) del ticker"]) for s in scens}
    assert parejas == {(r, u) for r in ("Sí", "No") for u in ("5", "10", "15")}
    # Las columnas single-valor van fijas en todos.
    assert all(s.cond["Stop loss (%) del ticker"] == "-70" for s in scens)
    # Regenerar es idempotente (no duplica).
    assert cmb.generate_scenarios(reg["id"], path=db) == 6
    assert cmb.get_combination(reg["id"], path=db)["estado"] == "generada"


def test_adapters_pipeline(tmp_path):
    db = tmp_path / "c.db"
    reg = cmb.import_file(_make_xlsx(tmp_path), path=db)
    cmb.generate_scenarios(reg["id"], path=db)
    # Seed con override de fechas/tickers (lo que hace el job incremental).
    seed = cmb.seed_for(reg["id"], fecha_inicial="2026-07-01", fecha_final="2026-07-02",
                        tickers=["IWM"], path=db)
    assert seed.tickers == ["IWM"] and seed.fecha_inicial == "2026-07-01"
    assert seed.inversion == 1000.0 and seed.entrada == "09:30" and seed.granularidad_seg == 60
    # Los escenarios se consumen SIN cambios por map_scenario (el contrato del batch).
    from ucbatch.scenario import map_scenario
    m = map_scenario(seed, cmb.scenarios_for_batch(reg["id"], path=db)[0])
    assert m.id == "C001" and m.run_kwargs["umbral_pct"] == 5.0
    assert m.run_kwargs["apply_refuerzo"] is True
    # DataFrame canónico (drop-in del load_template()[1]) + configs canónicas.
    df = cmb.scenarios_df(reg["id"], path=db)
    assert {"id", "alcance", "ticker_roi", "refuerzo"}.issubset(df.columns)
    cfgs = cmb.scenario_configs(reg["id"], path=db)
    assert cfgs["C001"]["ticker_roi"] == "5" and cfgs["C001"]["refuerzo"] == "Sí"


def test_activa_y_eliminar(tmp_path):
    db = tmp_path / "c.db"
    r1 = cmb.import_file(_make_xlsx(tmp_path), path=db)
    r2 = cmb.import_file(_make_xlsx(tmp_path), path=db)
    with pytest.raises(ValueError, match="generados"):
        cmb.set_active(r1["id"], path=db)                    # sin escenarios → no puede activarse
    cmb.generate_scenarios(r1["id"], path=db)
    cmb.set_active(r1["id"], path=db)
    assert cmb.active_combination(path=db) == r1["id"]
    with pytest.raises(ValueError, match="ACTIVA"):
        cmb.delete_combination(r1["id"], path=db)            # la activa no se borra
    cmb.delete_combination(r2["id"], path=db)
    assert [c["id"] for c in cmb.list_combinations(path=db)] == [r1["id"]]

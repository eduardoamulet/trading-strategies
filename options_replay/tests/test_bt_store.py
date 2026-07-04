"""Unit tests: almacén incremental (bt_store) + veredicto ponderado (playbook_store).

Headless: SQLite en tmp_path; el veredicto ponderado se testea PURO con filas fabricadas.
Corré:  pytest tests/test_bt_store.py -q
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
import bt_store  # noqa: E402
from playbook_store import compute_churn, compute_weighted_verdict  # noqa: E402


def _df(rows):
    """Filas canónicas mínimas (formato loader: id/ticker/fecha/inv/ganancia…)."""
    return pd.DataFrame([{"id": r[0], "ticker": r[1], "fecha": r[2], "inv": r[3],
                          "ganancia": r[4]} for r in rows])


# ── Almacén append-only ───────────────────────────────────────────────────────
def test_ingest_dedupe_y_coverage(tmp_path):
    db = tmp_path / "bt.db"
    df = _df([("C001", "QQQ", "2026-06-01", 1000, 50),
              ("C001", "SPY", "2026-06-01", 1000, -20),
              ("C002", "QQQ", "2026-06-02", 1000, 10)])
    assert bt_store.ingest_df(df, source_file="a.xlsx", path=db) == 3
    assert bt_store.ingest_df(df, source_file="b.xlsx", path=db) == 0      # dedupe por PK
    df2 = _df([("C001", "QQQ", "2026-06-03", 1000, 5)])
    assert bt_store.ingest_df(df2, path=db) == 1                           # append del día nuevo
    cov = bt_store.coverage(path=db)
    assert cov["filas"] == 4 and cov["desde"] == "2026-06-01" and cov["hasta"] == "2026-06-03"
    assert cov["dias"] == 3 and cov["tickers"] == ["QQQ", "SPY"]
    assert bt_store.last_date(path=db) == "2026-06-03"


def test_load_range_filtra_y_es_canonico(tmp_path):
    db = tmp_path / "bt.db"
    bt_store.ingest_df(_df([("C001", "QQQ", "2026-06-01", 1000, 50),
                            ("C001", "QQQ", "2026-06-02", 1000, 60),
                            ("C001", "QQQ", "2026-06-03", 1000, 70)]), path=db)
    out = bt_store.load_range("2026-06-02", "2026-06-03", path=db)
    assert sorted(out["fecha"]) == ["2026-06-02", "2026-06-03"]
    assert {"id", "ticker", "fecha", "inv", "ganancia"}.issubset(out.columns)  # formato loader
    assert bt_store.distinct_dates(path=db) == ["2026-06-01", "2026-06-02", "2026-06-03"]


# ── Veredicto ponderado (ventana rodante + decaimiento) ───────────────────────
# 12 lunes consecutivos (2026-01-05 … 2026-03-23): los 6 VIEJOS ganan +10%, los 6 RECIENTES
# pierden −10%. Sin pesos sería WR 50% / media 0; con decaimiento fuerte, lo reciente manda.
_LUNES = [f"2026-{m:02d}-{d:02d}" for m, d in
          [(1, 5), (1, 12), (1, 19), (1, 26), (2, 2), (2, 9),
           (2, 16), (2, 23), (3, 2), (3, 9), (3, 16), (3, 23)]]


def _lunes_df(viejos_roi, recientes_roi):
    rows = []
    for i, f in enumerate(_LUNES):
        roi = viejos_roi if i < 6 else recientes_roi
        rows.append(("C001", "QQQ", f, 1000.0, 1000.0 * roi / 100.0))
    return _df(rows)


def test_decaimiento_prioriza_lo_reciente():
    # viejos +10 / recientes −10 → las métricas ponderadas reflejan lo RECIENTE…
    per_day, _ = compute_weighted_verdict(_lunes_df(+10, -10), _LUNES, half_life=3, min_n=10)
    assert per_day["Lun"]["recommendation"] == "NO OPERAR"
    assert per_day["Lun"]["avg_roi"] < 0                       # la media ponderada es negativa
    # …y al revés (viejos −10 / recientes +10) las métricas se dan vuelta, aunque el WR
    # PLANO sea 50%. Pero el GATE bayesiano igual frena: con half-life 3 la evidencia
    # efectiva de 12 días queda en ~4.5 → P5 < 55 (no es un edge creíble todavía).
    per_day2, _ = compute_weighted_verdict(_lunes_df(-10, +10), _LUNES, half_life=3, min_n=10)
    assert per_day2["Lun"]["avg_roi"] > 0
    assert per_day2["Lun"]["win_rate"] > 55                    # WR ponderado, no el 50% plano
    assert per_day2["Lun"]["n"] == 12                          # n queda SIN ponderar (honesto)
    assert per_day2["Lun"]["wr_p5"] < 55
    assert per_day2["Lun"]["recommendation"] == "NO OPERAR"


# ── Gate bayesiano (P5 de la posterior Beta) + máquina de estados ─────────────
def _fechas_semanales(n):
    """n lunes consecutivos (solo importa el día de la semana, no el calendario real)."""
    return [d.strftime("%Y-%m-%d")
            for d in pd.date_range("2025-09-01", periods=n, freq="W-MON")]


def test_gate_p5_bloquea_wr_alto_con_muestra_corta():
    # WR puntual 75% (9W/3L, pesos ~llenos): el gate viejo (WR>55) pasaba; el P5 (~52%) NO.
    rows = [("C001", "QQQ", f, 1000.0, (100.0 if i % 4 else -100.0))
            for i, f in enumerate(_LUNES)]
    per_day, _ = compute_weighted_verdict(_df(rows), _LUNES, half_life=10_000, min_n=10)
    assert per_day["Lun"]["win_rate"] == 75.0
    assert per_day["Lun"]["wr_p5"] < 55
    assert per_day["Lun"]["recommendation"] == "NO OPERAR"


def test_gate_p5_abre_con_evidencia_sostenida_y_estado_operable():
    # El MISMO 80% de WR pero sostenido 30 lunes → P5>55 (OPERAR) y, como ambas mitades
    # pasan el gate simple, el estado es «operable» (regla de las dos ventanas).
    fechas = _fechas_semanales(30)
    rows = [("C001", "QQQ", f, 1000.0, (-50.0 if i % 5 == 0 else 50.0))
            for i, f in enumerate(fechas)]
    per_day, _ = compute_weighted_verdict(_df(rows), fechas, half_life=10_000, min_n=10)
    assert per_day["Lun"]["wr_p5"] > 55
    assert per_day["Lun"]["recommendation"] == "OPERAR"
    assert per_day["Lun"]["estado"] == "operable"


def test_estado_candidato_edge_solo_reciente():
    # Mitad vieja perdedora / mitad reciente ganadora → «candidato» (1 sola ventana).
    fechas = _fechas_semanales(30)
    rows = [("C001", "QQQ", f, 1000.0, (-20.0 if i < 15 else 20.0))
            for i, f in enumerate(fechas)]
    per_day, _ = compute_weighted_verdict(_df(rows), fechas, half_life=10_000, min_n=10)
    assert per_day["Lun"]["estado"] == "candidato"
    assert "reciente" in per_day["Lun"]["estado_motivo"]


def test_estado_suspendido_edge_decaido():
    # Mitad vieja ganadora / reciente mediocre (WR~50, sin kill-switch) → «suspendido».
    fechas = _fechas_semanales(30)
    rois = [20.0] * 15 + [-1.0, 0.1] * 7 + [0.1]          # tail3 = (0.1, −1, 0.1): no dispara
    rows = [("C001", "QQQ", f, 1000.0, 1000.0 * r / 100.0) for f, r in zip(fechas, rois)]
    per_day, _ = compute_weighted_verdict(_df(rows), fechas, half_life=10_000, min_n=10)
    assert per_day["Lun"]["estado"] == "suspendido"
    assert "decaído" in per_day["Lun"]["estado_motivo"]


def test_estado_kill_switch():
    fechas = _fechas_semanales(30)
    # 3 sesiones seguidas perdedoras al final → kill-switch, aunque el resto sea ganador.
    rois = [10.0] * 27 + [-2.0, -2.0, -2.0]
    rows = [("C001", "QQQ", f, 1000.0, 1000.0 * r / 100.0) for f, r in zip(fechas, rois)]
    per_day, _ = compute_weighted_verdict(_df(rows), fechas, half_life=10_000, min_n=10)
    assert per_day["Lun"]["estado"] == "suspendido"
    assert "kill-switch" in per_day["Lun"]["estado_motivo"]
    # ROI acumulado ≤ −15% (sin 3 seguidas al final) → también kill-switch.
    rois2 = [-3.0, 0.5] * 15                              # Σ = −37.5 · tail3 = (0.5, −3, 0.5)
    rows2 = [("C001", "QQQ", f, 1000.0, 1000.0 * r / 100.0) for f, r in zip(fechas, rois2)]
    per_day2, _ = compute_weighted_verdict(_df(rows2), fechas, half_life=10_000, min_n=10)
    assert per_day2["Lun"]["estado"] == "suspendido"
    assert "acumulado" in per_day2["Lun"]["estado_motivo"]


# ── Histéresis campeón/retador + monitores de vigencia ────────────────────────
def _dos_escenarios(fechas, roi_c001, roi_c002):
    """C001 y C002 con sus series de ROI diarias (mismas fechas, cartera QQQ)."""
    rows = []
    for i, f in enumerate(fechas):
        rows.append(("C001", "QQQ", f, 1000.0, 1000.0 * roi_c001[i] / 100.0))
        rows.append(("C002", "QQQ", f, 1000.0, 1000.0 * roi_c002[i] / 100.0))
    return _df(rows)


def test_histeresis_retador_debe_dominar_5_dias():
    fechas = _fechas_semanales(30)
    c001 = [(-50.0 if i % 5 == 0 else 50.0) for i in range(30)]   # WR 80, operable (campeón vivo)
    c002 = [80.0] * 30                                            # WR 100 — mejor por rank y P5
    df = _dos_escenarios(fechas, c001, c002)
    # Sin incumbents: gana el mejor directo.
    pd0, _ = compute_weighted_verdict(df, fechas, half_life=10_000, min_n=10)
    assert pd0["Lun"]["scenario"] == "C002"
    # Con campeón C001 vivo: el retador C002 domina pero NO destrona (racha 1/5).
    inc = {"Lun": {"scenario": "C001", "retador": None}}
    pd1, _ = compute_weighted_verdict(df, fechas, half_life=10_000, min_n=10, incumbents=inc)
    assert pd1["Lun"]["scenario"] == "C001"
    _rt = pd1["Lun"]["retador"]
    assert _rt["scenario"] == "C002" and _rt["racha"] == 1
    # Racha acumulada 4 → esta re-agregación es la 5.ª consecutiva → promoción.
    inc4 = {"Lun": {"scenario": "C001", "retador": {"scenario": "C002", "racha": 4}}}
    pd5, _ = compute_weighted_verdict(df, fechas, half_life=10_000, min_n=10, incumbents=inc4)
    assert pd5["Lun"]["scenario"] == "C002"
    assert pd5["Lun"]["retador"] is None


def test_histeresis_promocion_inmediata_si_campeon_muerto():
    fechas = _fechas_semanales(30)
    c001 = [-20.0] * 30                                           # campeón muerto (sin ventaja)
    c002 = [80.0] * 30                                            # retador vivo y operable
    df = _dos_escenarios(fechas, c001, c002)
    inc = {"Lun": {"scenario": "C001", "retador": None}}
    per_day, _ = compute_weighted_verdict(df, fechas, half_life=10_000, min_n=10, incumbents=inc)
    assert per_day["Lun"]["scenario"] == "C002"                   # sin esperar 5 días
    assert per_day["Lun"]["retador"] is None


def test_monitor_calibracion_suspende():
    # Ventana ganadora (P5 ~70) pero las ÚLTIMAS 10 sesiones caen a WR 50% — por debajo del
    # piso creíble prometido → «calibración rota» suspende, sin disparar el kill-switch.
    fechas = _fechas_semanales(30)
    rois = [50.0] * 20 + [-10.0, 50.0] * 5                # tail3 = (50, −10, 50) · Σ ≫ −15
    rows = [("C001", "QQQ", f, 1000.0, 1000.0 * r / 100.0) for f, r in zip(fechas, rois)]
    per_day, _ = compute_weighted_verdict(_df(rows), fechas, half_life=10_000, min_n=10)
    i = per_day["Lun"]
    assert i["wr_reciente"] == 50.0
    assert i["calib_alerta"] is True
    assert i["estado"] == "suspendido" and "calibración" in i["estado_motivo"]
    assert i["recommendation"] == "NO OPERAR"


def test_monitor_cusum_alerta_sin_suspender():
    # Todo ganador (calibración OK, operable) pero lo realizado reciente queda MUY por debajo
    # de lo prometido (+100 → +1): el CUSUM alerta (revisión anticipada) sin suspender.
    fechas = _fechas_semanales(30)
    rois = [100.0] * 20 + [1.0] * 10
    rows = [("C001", "QQQ", f, 1000.0, 1000.0 * r / 100.0) for f, r in zip(fechas, rois)]
    per_day, _ = compute_weighted_verdict(_df(rows), fechas, half_life=10_000, min_n=10)
    i = per_day["Lun"]
    assert i["cusum_alerta"] is True
    assert i["cusum_dev"] < i["cusum_umbral"] < 0
    assert i["recommendation"] == "OPERAR"                # informativo: NO suspende
    assert i["estado"] == "operable"
    assert not i.get("calib_alerta")                      # WR reciente 100% ≥ P5


def test_churn_detecta_fragilidad():
    hist = [{"per_day": {"Lun": {"scenario": s}}}
            for s in ("C001", "C002", "C001", "C002", "C001")]
    ch = compute_churn(hist, {"Lun": {"scenario": "C002"}})
    assert ch["Lun"] == {"n": 6, "tasa": 1.0, "alerta": True}
    # Campeón estable → tasa 0, sin alerta.
    hist2 = [{"per_day": {"Lun": {"scenario": "C001"}}} for _ in range(8)]
    ch2 = compute_churn(hist2, {"Lun": {"scenario": "C001"}})
    assert ch2["Lun"]["tasa"] == 0.0 and not ch2["Lun"]["alerta"]
    # Pocos snapshots → sin evidencia, sin alerta (no castiga al sistema recién nacido).
    ch3 = compute_churn(hist[:2], {"Lun": {"scenario": "C009"}})
    assert ch3["Lun"]["tasa"] is None and not ch3["Lun"]["alerta"]


def test_gate_min_n():
    # Solo 4 lunes ganadores → con min_n=10 el gate lo frena aunque las métricas den ventaja.
    df = _df([("C001", "QQQ", f, 1000.0, 100.0) for f in _LUNES[:4]])
    per_day, _ = compute_weighted_verdict(df, _LUNES[:4], half_life=35, min_n=10)
    assert per_day["Lun"]["recommendation"] == "NO OPERAR"
    assert "insuficiente" in per_day["Lun"]["reason"]


def test_por_ticker_gate_fino():
    # QQQ gana todos los lunes; SPY pierde todos → por_ticker debe separar los veredictos.
    rows = []
    for f in _LUNES:
        rows.append(("C001", "QQQ", f, 1000.0, 80.0))
        rows.append(("C001", "SPY", f, 1000.0, -80.0))
    per_day, por_ticker = compute_weighted_verdict(_df(rows), _LUNES, half_life=35, min_n=10)
    recs = {r["Ticker"]: r["Recomendación"] for r in por_ticker if r["Día"] == "Lun"}
    assert recs == {"QQQ": "OPERAR", "SPY": "NO OPERAR"}

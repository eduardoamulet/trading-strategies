"""Tests de la persistencia directa (sin Excel): equivalencia canónica + ciclo de reeval_runs.

La garantía central del cambio: rows_to_canonical_df(rows) debe producir EXACTAMENTE el mismo
DataFrame que el round-trip report.write() → loader.load_results() — mismas columnas, mismos
redondeos, mismo orden. Si esto pasa, la ingesta directa es indistinguible de la ingesta vía
archivo (y el --verify al centavo lo confirma end-to-end con el motor real).
Corré:  pytest tests/test_canonical.py -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
import bt_store  # noqa: E402
from bt_analysis import loader  # noqa: E402
from ucbatch import report  # noqa: E402
from ucbatch.canonical import rows_to_canonical_df  # noqa: E402

SEED = SimpleNamespace(tickers=["QQQ", "SPY"], tipo="CALL y PUT",
                       fecha_inicial="2026-06-01", fecha_final="2026-06-02")


def _row(sid, tk, fecha, **over):
    """Fila del runner con TODOS los campos (core + enriquecidos), valores con decimales
    'incómodos' para ejercitar los redondeos del report."""
    r = {"ID": sid, "Ticker": tk, "Fecha": fecha,
         "inversion": 1000.0, "ganancia": 12.3456,
         "roi_min_pct": -1.2345, "roi_min_usd": -12.345, "roi_max_pct": 3.14159,
         "roi_max_usd": 31.4159, "roi_avg_pct": 1.23456, "roi_avg_usd": 12.3456,
         "n_roi_pos": 3, "n_roi_neg": 1, "n_err": 0,
         "md_action": "CALL", "md_score": 72.34567, "md_confidence": 0.81234,
         "md_trend": "ALCISTA", "mode": "CALL y PUT",
         "call_strike": 500.0, "put_strike": 499.0,
         "call_bid": 1.23, "call_ask": 1.25, "call_spread": 0.02,
         "put_bid": None, "put_ask": None, "put_spread": None,
         "call_entry_prem": 1.24, "put_entry_prem": 0.55, "spot_at_start": 500.12345,
         "exit_reason": "umbral", "duration_min": 42.5,
         "call_occ": "O:QQQ260601C00500000", "put_occ": "O:QQQ260601P00499000",
         "call_delta": 0.51234, "call_gamma": 0.01234, "call_theta": -0.2345,
         "call_iv": 0.19876, "put_delta": -0.4987, "put_gamma": 0.0111,
         "put_theta": -0.2001, "put_iv": 0.20123}
    r.update(over)
    return r


def test_equivalencia_directo_vs_roundtrip_excel(tmp_path):
    """El corazón del cambio: DataFrame directo == xlsx → loader, incluyendo None/errores y
    el reordenamiento por (ticker del seed, fecha, id)."""
    rows = [_row("C002", "SPY", "2026-06-02", exit_reason=None),        # desordenadas a propósito
            _row("C001", "QQQ", "2026-06-01"),
            _row("C002", "QQQ", "2026-06-01", n_err=1, ganancia=None),
            _row("C001", "SPY", "2026-06-02", duration_min=None)]
    directo = rows_to_canonical_df(SEED, rows)
    out = report.write(SEED, rows, tmp_path)
    via_excel, gran = loader.load_results(out)
    assert gran == "detailed"
    assert list(directo.columns) == list(via_excel.columns)
    pd.testing.assert_frame_equal(directo, via_excel, check_dtype=False)


def test_equivalencia_sin_columnas_enriquecidas(tmp_path):
    """Filas 'core' solamente (sin señal de dirección ni snapshot) — el otro layout posible."""
    core = {"ID", "Ticker", "Fecha", "inversion", "ganancia", "roi_min_pct", "roi_min_usd",
            "roi_max_pct", "roi_max_usd", "roi_avg_pct", "roi_avg_usd",
            "n_roi_pos", "n_roi_neg", "n_err"}
    rows = [{k: v for k, v in _row("C001", "QQQ", "2026-06-01").items() if k in core},
            {k: v for k, v in _row("C001", "SPY", "2026-06-02").items() if k in core}]
    directo = rows_to_canonical_df(SEED, rows)
    out = report.write(SEED, rows, tmp_path)
    via_excel, _ = loader.load_results(out)
    assert list(directo.columns) == list(via_excel.columns)
    pd.testing.assert_frame_equal(directo, via_excel, check_dtype=False)


def test_canonico_ingesta_directo_al_almacen(tmp_path):
    """El DataFrame directo entra a bt_store.ingest_df sin fricción y con dedupe intacto."""
    db = tmp_path / "bt.db"
    rows = [_row("C001", "QQQ", "2026-06-01"), _row("C001", "SPY", "2026-06-02")]
    df = rows_to_canonical_df(SEED, rows)
    assert bt_store.ingest_df(df, source_file="ingest:test", combination="comb_t", path=db) == 2
    assert bt_store.ingest_df(df, combination="comb_t", path=db) == 0     # dedupe por PK
    cov = bt_store.coverage("comb_t", path=db)
    assert cov["filas"] == 2 and cov["tickers"] == ["QQQ", "SPY"]


# ── Ciclo de vida de reeval_runs ──────────────────────────────────────────────
def test_reeval_run_ciclo_exitosa(tmp_path):
    db = tmp_path / "bt.db"
    bt_store.record_run_start("r1", combination="comb_x", combination_nombre="Mi Comb",
                              tipo="reevaluacion", fecha_desde="2026-06-01",
                              fecha_hasta="2026-06-30", tickers="QQQ,SPY",
                              n_escenarios=768, n_dias=21, path=db)
    r = bt_store.get_run("r1", path=db)
    assert r["estado"] == "corriendo" and r["finalizado"] == 0
    bt_store.record_run_finish("r1", estado="exitosa", n_filas=100, n_filas_nuevas=90, n_err=2,
                               resumen={"ganancia_total": 5.5}, path=db)
    r = bt_store.get_run("r1", path=db)
    assert r["estado"] == "exitosa" and r["n_filas_nuevas"] == 90
    assert r["duration_s"] is not None and r["resumen"]["ganancia_total"] == 5.5
    assert bt_store.latest_run(tipo="reevaluacion", path=db)["run_id"] == "r1"
    bt_store.mark_run_finalized("r1", path=db)
    assert bt_store.get_run("r1", path=db)["finalizado"] == 1


def test_reeval_run_fallida_e_historial(tmp_path):
    db = tmp_path / "bt.db"
    bt_store.record_run_start("r1", combination="c", path=db)
    bt_store.record_run_finish("r1", estado="exitosa", path=db)
    bt_store.record_run_start("r2", combination="c", tipo="incremental", path=db)
    bt_store.record_run_finish("r2", estado="fallida", error_msg="boom", path=db)
    rs = bt_store.runs_for(combination="c", path=db)
    assert [x["run_id"] for x in rs] == ["r2", "r1"]                     # más recientes primero
    assert rs[0]["estado"] == "fallida" and rs[0]["error_msg"] == "boom"
    # el lanzador crea la fila primero; el worker con INSERT OR IGNORE no la pisa
    bt_store.record_run_start("r2", combination="OTRA", path=db)
    assert bt_store.get_run("r2", path=db)["combination"] == "c"


def test_reeval_runs_huerfanas_a_abortada(tmp_path):
    db = tmp_path / "bt.db"
    bt_store.record_run_start("viejo", combination="c", path=db)
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE reeval_runs SET started_at='2026-01-01 00:00:00'")
    con.commit()
    con.close()
    assert bt_store.mark_stale_running(max_hours=1, path=db) == 1
    assert bt_store.get_run("viejo", path=db)["estado"] == "abortada"

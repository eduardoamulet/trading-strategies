"""Unit tests: almacén incremental (bt_store) + veredicto ponderado (playbook_store).

Headless: SQLite en tmp_path; el veredicto ponderado se testea PURO con filas fabricadas.
Corré:  pytest tests/test_bt_store.py -q
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
import bt_store  # noqa: E402
from playbook_store import compute_weighted_verdict  # noqa: E402


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
    # viejos +10 / recientes −10 → el veredicto ponderado debe ser NO OPERAR…
    per_day, _ = compute_weighted_verdict(_lunes_df(+10, -10), _LUNES, half_life=3, min_n=10)
    assert per_day["Lun"]["recommendation"] == "NO OPERAR"
    assert per_day["Lun"]["avg_roi"] < 0                       # la media ponderada es negativa
    # …y al revés (viejos −10 / recientes +10) → OPERAR, aunque el WR PLANO sea 50%.
    per_day2, _ = compute_weighted_verdict(_lunes_df(-10, +10), _LUNES, half_life=3, min_n=10)
    assert per_day2["Lun"]["recommendation"] == "OPERAR"
    assert per_day2["Lun"]["win_rate"] > 55                    # WR ponderado, no el 50% plano
    assert per_day2["Lun"]["n"] == 12                          # n queda SIN ponderar (honesto)


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

"""Tests del GEX diario (gex.py): convención de signos, flip interpolado, walls, cache.
Cadena sintética en una DB temporal — sin Polygon."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gex


def _sembrar(path, filas):
    """filas = [(strike, tipo, gamma, oi, spot), ...] para (2026-07-09, apertura, TST)."""
    con = gex._connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS chain_snapshot (
        fecha TEXT, momento TEXT, ticker TEXT, occ TEXT,
        expiration TEXT, tipo TEXT, strike REAL, oi INTEGER, volumen INTEGER,
        iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
        bid REAL, ask REAL, mid REAL, last_price REAL, spot REAL, capturado_en TEXT,
        PRIMARY KEY (fecha, momento, ticker, occ))""")
    for i, (k, tipo, g, oi, spot) in enumerate(filas):
        con.execute("INSERT INTO chain_snapshot (fecha, momento, ticker, occ, tipo, strike, "
                    "oi, gamma, spot) VALUES ('2026-07-09', 'apertura', 'TST', ?, ?, ?, ?, ?, ?)",
                    (f"O:TST{i:04d}", tipo, k, oi, g, spot))
    con.commit()
    return con


def test_signos_y_total(tmp_path):
    db = tmp_path / "s.db"
    # spot 100 → factor por unidad de gamma×OI: 100 × 100² × 0.01 = 10.000 $
    _sembrar(db, [(100.0, "call", 0.05, 1000, 100.0),    # +0.05×1000×10k = +$500k
                  (100.0, "put", 0.02, 1000, 100.0)])    # −0.02×1000×10k = −$200k
    g = gex.compute_gex("2026-07-09", "TST", path=db)
    assert g is not None
    assert abs(g["gex_total_musd"] - 0.3) < 1e-9         # +$300k = 0.3 M$
    assert g["regimen"] == "rango (GEX+)"
    assert g["n_contratos"] == 2 and g["spot"] == 100.0


def test_regimen_tendencia_cuando_dominan_puts(tmp_path):
    db = tmp_path / "s.db"
    _sembrar(db, [(100.0, "call", 0.01, 500, 100.0),
                  (95.0, "put", 0.06, 2000, 100.0)])
    g = gex.compute_gex("2026-07-09", "TST", path=db)
    assert g["regimen"] == "tendencia (GEX-)"
    assert g["gex_total_musd"] < 0


def test_flip_interpolado_y_walls(tmp_path):
    db = tmp_path / "s.db"
    # Acumulado por strike: 95 → −400k; 105 → −400k+800k = +400k → cruza cero ENTRE 95 y 105
    # exactamente a mitad (frac = 400/800) → flip = 100.0.
    _sembrar(db, [(95.0, "put", 0.04, 1000, 100.0),      # −$400k en 95
                  (105.0, "call", 0.08, 1000, 100.0)])   # +$800k en 105
    g = gex.compute_gex("2026-07-09", "TST", path=db)
    assert g["flip"] == 100.0
    assert g["call_wall"] == 105.0 and g["put_wall"] == 95.0
    assert g["spot_vs_flip_pct"] == 0.0                  # spot 100 = flip


def test_sin_cruce_no_hay_flip(tmp_path):
    db = tmp_path / "s.db"
    _sembrar(db, [(95.0, "call", 0.02, 500, 100.0), (105.0, "call", 0.03, 500, 100.0)])
    g = gex.compute_gex("2026-07-09", "TST", path=db)
    assert g["flip"] is None and g["spot_vs_flip_pct"] is None
    assert g["regimen"] == "rango (GEX+)"


def test_cache_y_lectura(tmp_path):
    db = tmp_path / "s.db"
    _sembrar(db, [(100.0, "call", 0.05, 1000, 100.0)])
    g1 = gex.leer_gex("2026-07-09", "TST", path=db)      # computa y cachea
    g2 = gex.leer_gex("2026-07-09", "TST", path=db)      # lee del cache
    assert g1["gex_total_musd"] == g2["gex_total_musd"]
    assert gex.leer_gex("2026-01-01", "TST", path=db) is None   # sin snapshot → None


def test_mas_reciente_cae_al_snapshot_previo(tmp_path):
    db = tmp_path / "s.db"
    _sembrar(db, [(100.0, "call", 0.05, 1000, 100.0)])   # 2026-07-09 apertura
    g = gex.gex_mas_reciente("TST", "2026-07-10", path=db)   # pide el 10 → cae al 09
    assert g is not None and g["fecha"] == "2026-07-09"

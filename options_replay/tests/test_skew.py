"""Tests del skew 25Δ diario (skew.py) con cadena sintética — sin Polygon."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import skew


def _sembrar(path, filas, fecha="2026-07-09", exp="2026-07-09"):
    """filas = [(tipo, delta, iv), ...]"""
    con = skew._connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS chain_snapshot (
        fecha TEXT, momento TEXT, ticker TEXT, occ TEXT,
        expiration TEXT, tipo TEXT, strike REAL, oi INTEGER, volumen INTEGER,
        iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
        bid REAL, ask REAL, mid REAL, last_price REAL, spot REAL, capturado_en TEXT,
        PRIMARY KEY (fecha, momento, ticker, occ))""")
    for i, (tipo, d, iv) in enumerate(filas):
        con.execute("INSERT INTO chain_snapshot (fecha, momento, ticker, occ, expiration, "
                    "tipo, delta, iv) VALUES (?, 'apertura', 'TST', ?, ?, ?, ?, ?)",
                    (fecha, f"O:TST{i:04d}", exp, tipo, d, iv))
    con.commit()


def test_skew_puts_caras(tmp_path):
    db = tmp_path / "s.db"
    _sembrar(db, [("put", -0.25, 0.32), ("put", -0.50, 0.28), ("put", -0.10, 0.40),
                  ("call", 0.25, 0.24), ("call", 0.50, 0.27), ("call", 0.10, 0.22)])
    s = skew.compute_skew("2026-07-09", "TST", path=db)
    assert s is not None
    assert s["iv_put25"] == 0.32 and s["iv_call25"] == 0.24     # los ~25Δ exactos
    assert s["skew_pts"] == 8.0                                  # (0.32−0.24)×100
    assert s["atm_iv"] == round((0.28 + 0.27) / 2, 4)


def test_elige_el_delta_mas_cercano(tmp_path):
    db = tmp_path / "s.db"
    # No hay 25Δ exacto: put −0.22 y call 0.31 son los más cercanos.
    _sembrar(db, [("put", -0.22, 0.30), ("put", -0.45, 0.28),
                  ("call", 0.31, 0.26), ("call", 0.55, 0.25)])
    s = skew.compute_skew("2026-07-09", "TST", path=db)
    assert s["iv_put25"] == 0.30 and s["iv_call25"] == 0.26
    assert s["skew_pts"] == 4.0


def test_sin_datos_none(tmp_path):
    db = tmp_path / "s.db"
    _sembrar(db, [("call", 0.25, 0.24)])     # sin puts → no hay skew
    assert skew.compute_skew("2026-07-09", "TST", path=db) is None
    assert skew.leer_skew("2026-01-01", "TST", path=db) is None


def test_mas_reciente_y_cache(tmp_path):
    db = tmp_path / "s.db"
    _sembrar(db, [("put", -0.25, 0.30), ("call", 0.25, 0.26)])
    s1 = skew.skew_mas_reciente("TST", "2026-07-10", path=db)
    assert s1 is not None and s1["fecha"] == "2026-07-09"
    s2 = skew.leer_skew("2026-07-09", "TST", path=db)   # ahora desde el cache
    assert s2["skew_pts"] == s1["skew_pts"]

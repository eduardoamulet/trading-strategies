"""Unit tests de la Fase A de performance: md_cache + partición intra-día por tramos.

Headless y PURO: el motor de dirección se fake-a con monkeypatch (sin Polygon/underlying).
Corré:  pytest tests/test_fase_a_performance.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
import md_cache  # noqa: E402
import market_direction.engine as _md  # noqa: E402
from ucbatch.runner import _chunk_ranges  # noqa: E402


# ---------------------------------------------------------------- _chunk_ranges (puro)


def _cubre_exacto(bounds, n):
    """Los tramos son contiguos, ordenados y cubren [0, n) exactamente una vez."""
    lo_esperado = 0
    for lo, hi in bounds:
        assert lo == lo_esperado and hi > lo
        lo_esperado = hi
    assert lo_esperado == n


def test_chunks_un_dia_muchos_cores():
    """El caso que motivó la Fase A: 1 día × 8 cores × 480 escenarios → 8 tramos de 60."""
    bounds = _chunk_ranges(480, n_days=1, procs=8)
    assert len(bounds) == 8
    assert all(hi - lo == 60 for lo, hi in bounds)
    _cubre_exacto(bounds, 480)


def test_chunks_muchos_dias_no_parte():
    """Con más días que cores el día entero sigue siendo la unidad (comportamiento previo)."""
    assert _chunk_ranges(480, n_days=131, procs=8) == [(0, 480)]


def test_chunks_respeta_tramo_minimo():
    """Nunca genera tramos menores a min_chunk: 40 escenarios / 8 cores → 2 tramos de 20."""
    bounds = _chunk_ranges(40, n_days=1, procs=8, min_chunk=30)
    assert len(bounds) == 2
    _cubre_exacto(bounds, 40)


def test_chunks_reparto_desparejo_cubre_todo():
    """n no divisible: 481 en 8 tramos → los primeros llevan el extra, cobertura exacta."""
    bounds = _chunk_ranges(481, n_days=1, procs=8)
    _cubre_exacto(bounds, 481)
    assert max(hi - lo for lo, hi in bounds) - min(hi - lo for lo, hi in bounds) <= 1


def test_chunks_bordes():
    assert _chunk_ranges(0, 1, 8) == []
    assert _chunk_ranges(5, 1, 1) == [(0, 5)]


# ---------------------------------------------------------------- md_cache


class _SigFake:
    """TradeSignal mínimo para el cache (action/score/confidence/trend)."""

    class _V:
        def __init__(self, v):
            self.value = v

    def __init__(self, action="CALL", score=72.3456, conf=0.8125, trend="ALCISTA"):
        self.action = self._V(action)
        self.score = score
        self.confidence = conf
        self.trend = self._V(trend)


def test_md_cache_computa_una_vez_y_luego_lee(tmp_path, monkeypatch):
    db = tmp_path / "md_cache_test.db"
    llamadas = {"n": 0}

    def _fake_engine(ticker, fecha, hora, provider=None):
        llamadas["n"] += 1
        return _SigFake()

    monkeypatch.setattr(_md, "market_direction_engine", _fake_engine)
    r1 = md_cache.get_or_compute("qqq", "2026-03-02", "09:33", db_path=db)
    r2 = md_cache.get_or_compute("QQQ", "2026-03-02", "09:33", db_path=db)
    assert llamadas["n"] == 1                      # la 2ª fue hit (y el ticker se normaliza)
    assert r1 == r2
    # mismos redondeos que producía el batch: score .1 / confianza .3
    assert r1["md_score"] == 72.3 and r1["md_confidence"] == 0.812
    assert r1["md_action"] == "CALL" and r1["md_trend"] == "ALCISTA"


def test_md_cache_no_cachea_huella_sin_datos(tmp_path, monkeypatch):
    db = tmp_path / "md_cache_test.db"
    llamadas = {"n": 0}

    def _fake_engine(ticker, fecha, hora, provider=None):
        llamadas["n"] += 1
        return _SigFake(action="NO TRADE", score=50.0, conf=0.0, trend="NEUTRAL")

    monkeypatch.setattr(_md, "market_direction_engine", _fake_engine)
    md_cache.get_or_compute("QQQ", "2026-03-02", "09:33", db_path=db)
    md_cache.get_or_compute("QQQ", "2026-03-02", "09:33", db_path=db)
    assert llamadas["n"] == 2                      # nunca se cachea → siempre recomputa


def test_md_cache_version_invalida(tmp_path, monkeypatch):
    db = tmp_path / "md_cache_test.db"
    monkeypatch.setattr(_md, "market_direction_engine",
                        lambda *a, **k: _SigFake(score=60.0))
    md_cache.get_or_compute("QQQ", "2026-03-02", "09:33", db_path=db)
    # el motor cambia de versión → el cache viejo deja de servir y se recomputa
    monkeypatch.setattr(_md, "MD_ENGINE_VERSION", "9999-99-99")
    monkeypatch.setattr(_md, "market_direction_engine",
                        lambda *a, **k: _SigFake(score=80.0))
    r = md_cache.get_or_compute("QQQ", "2026-03-02", "09:33", db_path=db)
    assert r["md_score"] == 80.0

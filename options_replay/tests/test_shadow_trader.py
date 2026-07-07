"""Tests del shadow trader (Fase 1): decisión desde el playbook + tamaño + almacén.
Sin red: la selección con Alpaca es import perezoso y acá no se ejercita."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shadow_trader as sh


def _pb(rec="OPERAR", estado="operable", esc="C1621"):
    return {"combination": "comb_test",
            "per_day": {"Jue": {"recommendation": rec, "estado": estado,
                                "scenario": esc, "reason": "test"}}}


def test_sin_playbook_saltea():
    d, motivo, esc, comb = sh.decidir_dia(None, "Jue")
    assert d == "saltear" and "sin playbook" in motivo


def test_dia_operable_entra():
    d, motivo, esc, comb = sh.decidir_dia(_pb(), "Jue")
    assert d == "entrar" and esc == "C1621" and comb == "comb_test"


def test_no_operar_saltea():
    d, motivo, *_ = sh.decidir_dia(_pb(rec="NO OPERAR"), "Jue")
    assert d == "saltear" and "NO OPERAR" in motivo


def test_estado_candidato_saltea():
    # La política del automático: OPERAR pero estado != operable → no se opera.
    d, motivo, *_ = sh.decidir_dia(_pb(estado="candidato"), "Jue")
    assert d == "saltear" and "candidato" in motivo


def test_dia_no_cubierto_saltea():
    d, motivo, *_ = sh.decidir_dia(_pb(), "Lun")
    assert d == "saltear" and "no cubre" in motivo


def test_playbook_viejo_sin_estado_entra():
    d, *_ = sh.decidir_dia(_pb(estado=None), "Jue")
    assert d == "entrar"


def test_qty():
    assert sh._qty(500.0, 0.50) == 10       # $500 / ($0.50 × 100)
    assert sh._qty(500.0, 1.76) == 2
    assert sh._qty(500.0, 0.0) == 0
    assert sh._qty(500.0, 6.00) == 0        # prima más cara que el presupuesto


def test_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "DB_PATH", tmp_path / "shadow.db")
    con = sh._connect()
    sh._grabar(con, {"fecha": "2026-07-09", "ticker": "QQQ", "decision": "entrar",
                     "motivo": "test", "call_occ": "QQQ260709C00700000",
                     "call_ask": 0.52, "call_qty": 9})
    # INSERT OR REPLACE: la última corrida del día manda
    sh._grabar(con, {"fecha": "2026-07-09", "ticker": "QQQ", "decision": "saltear",
                     "motivo": "re-corrida"})
    filas = con.execute("SELECT decision, motivo FROM shadow_decisions "
                        "WHERE fecha='2026-07-09' AND ticker='QQQ'").fetchall()
    assert filas == [("saltear", "re-corrida")]


def test_rango_prima_fallback(monkeypatch):
    monkeypatch.setattr(sh, "HERE", Path("Z:/no/existe"))
    lo, hi = sh._rango_prima("QQQ")
    assert (lo, hi) == (0.30, 0.50)


def test_usar_playbook_default_false(tmp_path, monkeypatch):
    # Sin config → False (entra todos los días hábiles); el checkbox de Operar lo persiste.
    monkeypatch.setattr(sh, "CONFIG_PATH", tmp_path / "shadow_config.json")
    assert sh._usar_playbook() is False
    (tmp_path / "shadow_config.json").write_text('{"usar_playbook": true}', encoding="utf-8")
    assert sh._usar_playbook() is True
    (tmp_path / "shadow_config.json").write_text('{"usar_playbook": false}', encoding="utf-8")
    assert sh._usar_playbook() is False

"""Unit tests de la finalización ASÍNCRONA del playbook (claim atómico + estados + spawn).

Corré:  pytest tests/test_finalize_async.py -q
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
import bt_store  # noqa: E402
import playbook_store as pbs  # noqa: E402


def _run_exitoso(db, run_id="r1"):
    bt_store.record_run_start(run_id, combination="comb_x", tipo="reevaluacion",
                              fecha_desde="2026-01-02", fecha_hasta="2026-03-31", path=db)
    bt_store.record_run_finish(run_id, estado="exitosa", n_filas=100, n_filas_nuevas=100,
                               path=db)


def test_claim_atomico_una_sola_vez(tmp_path):
    db = tmp_path / "bt.db"
    _run_exitoso(db)
    assert bt_store.claim_run_finalizing("r1", path=db) is True     # 1º reclamante gana
    assert bt_store.claim_run_finalizing("r1", path=db) is False    # 2º pierde (ya finalizando)
    assert bt_store.get_run("r1", path=db)["estado"] == "finalizando"
    bt_store.finish_run_finalized("r1", path=db)
    r = bt_store.get_run("r1", path=db)
    assert r["estado"] == "exitosa" and r["finalizado"] == 1        # historial limpio
    assert bt_store.claim_run_finalizing("r1", path=db) is False    # finalizado → no re-claim


def test_error_de_finalize_permite_retry(tmp_path):
    db = tmp_path / "bt.db"
    _run_exitoso(db)
    assert bt_store.claim_run_finalizing("r1", path=db)
    bt_store.record_finalize_error("r1", "KeyError: boom", path=db)
    r = bt_store.get_run("r1", path=db)
    assert r["estado"] == "exitosa" and r["error_msg"].startswith("finalize:")
    assert r["finalizado"] == 0
    # retry: se puede reclamar de nuevo; al lograrlo, el error se limpia al finalizar
    assert bt_store.claim_run_finalizing("r1", path=db) is True
    bt_store.finish_run_finalized("r1", path=db)
    assert bt_store.get_run("r1", path=db)["error_msg"] is None


def test_finalizador_huerfano_revive(tmp_path):
    db = tmp_path / "bt.db"
    _run_exitoso(db)
    bt_store.claim_run_finalizing("r1", path=db)
    con = sqlite3.connect(db)
    con.execute("UPDATE reeval_runs SET finished_at='2026-01-01 00:00:00'")
    con.commit()
    con.close()
    assert bt_store.revive_stale_finalizing(max_hours=1, path=db) == 1
    assert bt_store.get_run("r1", path=db)["estado"] == "exitosa"   # la página lo relanza sola
    # uno RECIENTE no se toca
    _run_exitoso(db, "r2")
    bt_store.claim_run_finalizing("r2", path=db)
    assert bt_store.revive_stale_finalizing(max_hours=1, path=db) == 0


def test_spawn_finalize_claim_y_comando(monkeypatch, tmp_path):
    lanzados = []

    def _fake_popen(cmd, **kw):
        lanzados.append(cmd)

        class _P:
            pid = 12345
        return _P()

    claims = iter([True, False])
    monkeypatch.setattr(bt_store, "claim_run_finalizing", lambda rid: next(claims))
    monkeypatch.setattr(pbs.subprocess, "Popen", _fake_popen)
    assert pbs.spawn_finalize("20260704_231116") is True            # 1ª llamada: lanza
    assert pbs.spawn_finalize("20260704_231116") is False           # 2ª: claim perdido → no lanza
    assert len(lanzados) == 1
    cmd = lanzados[0]
    assert "--finalize-run" in cmd and "20260704_231116" in cmd
    assert any(str(c).endswith("update_playbook.py") for c in cmd)


def test_display_name_recorta_boilerplate():
    dn = pbs.display_name
    assert dn("Backtesting_variables_template QQQ SPY IWM - tickers y colectivo.xlsx") == \
        "QQQ SPY IWM - tickers y colectivo"
    assert dn("Backtesting_variables_template QQQ SPY IWM - solo tickers.xlsx") == \
        "QQQ SPY IWM - solo tickers"
    assert dn(None) == "Template 480 (legacy)"
    assert dn({"combination_nombre": "Mi Comb.xlsx"}) == "Mi Comb"
    largo = dn("x" * 80)
    assert len(largo) == 45 and largo.startswith("…")     # trunca por la CABEZA (cola distintiva)


def test_beta_ppf_puro_contra_formas_cerradas():
    """El fallback puro-Python del P5: Beta(a,1) y Beta(1,b) tienen CDF cerrada → cuantiles
    exactos para verificar sin scipy. Además simetría I_x(a,b) = 1 - I_{1-x}(b,a)."""
    pp = pbs._beta_ppf_puro
    assert abs(pp(0.05, 1, 1) - 0.05) < 1e-9                    # uniforme
    assert abs(pp(0.05, 2, 1) - 0.05 ** 0.5) < 1e-9             # CDF = x²
    assert abs(pp(0.05, 3, 1) - 0.05 ** (1 / 3)) < 1e-9         # CDF = x³
    assert abs(pp(0.05, 1, 2) - (1 - 0.95 ** 0.5)) < 1e-9       # CDF = 1-(1-x)²
    # simetría del cuantil: Q_beta(q; a,b) = 1 − Q_beta(1−q; b,a)
    assert abs(pp(0.05, 7.3, 2.6) - (1 - pp(0.95, 2.6, 7.3))) < 1e-9
    # _beta_p5 nunca revienta aunque scipy esté bloqueado (usa el camino que toque)
    v = pbs._beta_p5(10.0, 5.0)
    assert 0.0 < v < 100.0


def test_playbook_por_combinacion_roundtrip(tmp_path, monkeypatch):
    """El veredicto por combinación persiste en su json propio y se relee — sin tocar el
    playbook.json vigente."""
    monkeypatch.setattr(pbs, "playbook_path_for",
                        lambda cid: tmp_path / f"playbook_{cid}.json")
    fake = {"combination": "comb_x", "per_day": {"Jue": {"scenario": "C1"}}}
    pbs.save_playbook(fake, path=pbs.playbook_path_for("comb_x"))
    out = pbs.load_playbook_for("comb_x")
    assert out["combination"] == "comb_x"
    assert pbs.load_playbook_for("comb_inexistente") is None

"""Prueba de EQUIVALENCIA: strategy_core espeja exactamente la lógica previa del
engine (gate de spread, key de selección 'Opción 1', regla de salida). Si esto pasa,
backtest y live usan la MISMA regla (sin drift). Correr: py test_strategy_core.py"""
import strategy_core as sc

BUCKETS = sc.DEFAULT_SPREAD_BUCKETS


# ---- Referencia 1: la lógica EXACTA que tenía engine._max_spread_for_price ----
def ref_max_spread(spot, cfg):
    ov = cfg.get("_max_spread_override")
    if ov is not None:
        return float(ov)
    for b in cfg.get("buckets", []):
        if b["price_min"] <= spot < b["price_max"]:
            return float(b["max_spread"])
    return float("inf")


def test_spread_gate():
    cfg = {"buckets": BUCKETS}
    for spot in [0, 50, 99.99, 100, 250, 300, 599.99, 600, 737.05, 1199.99, 1200, 5000]:
        a, b = ref_max_spread(spot, cfg), sc.max_spread_for_price(spot, BUCKETS)
        assert a == b, f"spread gate spot={spot}: ref={a} core={b}"
    # override PLANO manda sobre los buckets
    cfg2 = {"buckets": BUCKETS, "_max_spread_override": 0.08}
    for spot in [50, 737, 5000]:
        a = ref_max_spread(spot, cfg2)
        b = sc.max_spread_for_price(spot, BUCKETS, override=0.08)
        assert a == b == 0.08, f"override spot={spot}: ref={a} core={b}"
    print("OK  gate de spread (engine == strategy_core)")


# ---- Referencia 2: la regla de salida del engine (profit gana empates) ----
def ref_exit(roi, umbral, stop):
    profit = roi >= umbral      # engine: pct_total >= exit_threshold
    loss = roi <= stop          # engine: pct_total <= stop_loss
    if profit:                  # profit_pos <= loss_pos → profit gana el empate
        return "take_profit"
    if loss:
        return "stop_loss"
    return None


def test_exit_rule():
    for roi in [-100, -50, -1, 0, 0.5, 19.99, 20, 20.01, 100, 1000]:
        for umbral in [20, 100, 1000]:
            for stop in [-100, -50, -20]:
                a = ref_exit(roi, umbral, stop)
                b = sc.exit_decision(roi, umbral, stop)
                assert a == b, f"exit roi={roi} u={umbral} s={stop}: ref={a} core={b}"
    # empate exacto: roi == umbral → take_profit (no None)
    assert sc.exit_decision(20.0, 20.0, -100.0) == "take_profit"
    # roi == stop → stop_loss
    assert sc.exit_decision(-100.0, 1000.0, -100.0) == "stop_loss"
    print("OK  regla de salida (engine == strategy_core)")


# ---- Referencia 3: el key de _select 'spread' (Opción 1) del engine ----
def ref_select_key(spread, depth, volume):
    sp = round(spread, 2) if spread is not None else 9.99
    itm = depth if depth >= 0 else abs(depth) + 1e6     # engine._itm_rank
    return (sp, itm, -(volume or 0.0))


def test_selection_key():
    # (spread, depth, liquidez) — incluye empates de spread y OTM (depth<0)
    cands = [
        (0.05, 2.0, 100), (0.05, -1.0, 500), (0.03, 0.5, 10),
        (0.03, 0.5, 50), (None, 1.0, 0), (0.10, -3.0, 999),
        (0.03, -0.5, 80), (0.05, 0.0, 200),
    ]
    for (s, d, v) in cands:
        a = ref_select_key(s, d, v)
        b = sc.selection_key(s, d, v, mode="itm")
        assert a == b, f"key ({s},{d},{v}): ref={a} core={b}"
    # el ARGMIN (contrato elegido) coincide
    ref_best = min(range(len(cands)), key=lambda i: ref_select_key(*cands[i]))
    sc_best = min(range(len(cands)),
                  key=lambda i: sc.selection_key(cands[i][0], cands[i][1], cands[i][2], mode="itm"))
    assert ref_best == sc_best, f"argmin: ref={ref_best} core={sc_best}"
    print("OK  key de selección Opción 1 (engine == strategy_core)")


# ---- itm_depth / atm_rank: definición de ATM/ITM ----
def test_itm_atm():
    # CALL ITM si spot>strike ; PUT ITM si spot<strike
    assert sc.itm_depth(98, 100, "CALL") == 2.0      # ITM
    assert sc.itm_depth(102, 100, "CALL") == -2.0    # OTM
    assert sc.itm_depth(102, 100, "PUT") == 2.0      # ITM
    assert sc.itm_depth(98, 100, "PUT") == -2.0      # OTM
    # ATM puro = |strike-spot| = |depth| (lo que usa el live en mode='atm')
    for strike in [95, 98, 100, 103, 110]:
        d = sc.itm_depth(strike, 100, "CALL")
        atm_key = sc.selection_key(0.05, d, 0, mode="atm")
        assert atm_key[1] == abs(strike - 100), (strike, atm_key)
    print("OK  itm_depth / atm (definición ATM-ITM)")


if __name__ == "__main__":
    test_spread_gate()
    test_exit_rule()
    test_selection_key()
    test_itm_atm()
    print("\n✅ TODO OK — strategy_core es equivalente a la lógica del engine.")

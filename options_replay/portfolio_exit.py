"""Salidas A NIVEL CARTERA (colectivas) para el backtest de señales — lógica pura, testeable.

Cierra TODAS las posiciones abiertas de un día cuando el ROI de CARTERA cruza el umbral de ganancia
(ROI colectivo) o el stop (stop colectivo), en una sola pasada cronológica donde gana el PRIMER
trigger del día.

No depende de Streamlit ni del engine: opera sobre los `IterationResult` por *duck-typing* (atributos
`.df`, `.start_dt`, `.end_dt`, `.ticker`, `.call_entry_premium`, `.refuerzo`, etc.), así que se puede
testear de forma headless con objetos mock (ver tests/test_portfolio_exit.py).
"""
from __future__ import annotations

from collections import defaultdict

import pandas as pd


def collective_close(it, t_star, pos_idx: int, reason: str = "collective_roi") -> None:
    """Fija la salida de `it` en el minuto `t_star` (fila `pos_idx` de it.df): precios de salida =
    los de ese minuto, motivo `reason` (collective_roi / collective_stop), corta el timeline ahí,
    recalcula final/max/min.

    Solo estampa las piernas AÚN ABIERTAS al minuto del corte: una pierna ya vendida por su
    PROPIO trigger (Until ROI / plus / stop de pierna) conserva su hora y motivo — el colectivo
    cierra lo abierto, no reescribe historia. (Bug 2026-07-06: re-estampaba la pierna ya vendida
    con la hora del corte — «✔09:48» en una PUT bancada a las 09:31; los $ no cambiaban porque su
    serie ya estaba congelada, pero la hora/motivo mostrados quedaban mal.)"""
    _df = it.df

    def _abierta(_xi) -> bool:
        return _xi is None or int(_xi) > int(pos_idx)

    if it.call_entry_premium and _abierta(getattr(it, "call_exit_idx", None)):
        it.call_exit_premium = float(_df.iloc[pos_idx]["call_px"])
        it.call_exit_idx = int(pos_idx)
        it.call_exit_reason = reason
    if it.put_entry_premium and _abierta(getattr(it, "put_exit_idx", None)):
        it.put_exit_premium = float(_df.iloc[pos_idx]["put_px"])
        it.put_exit_idx = int(pos_idx)
        it.put_exit_reason = reason
    it.exit_reason = reason
    it.end_dt = pd.Timestamp(t_star)
    it.df = _df.iloc[:pos_idx + 1].reset_index(drop=True)
    # Refuerzo: recomputar el estado AL MINUTO DEL CIERRE (capital, ganancia y Nº de refuerzos hasta
    # `pos_idx`). Sin esto, it.refuerzo —y por ende it.gain_total/invest_total— seguiría reflejando la
    # corrida COMPLETA (hasta el cierre del día), no el truncado por el colectivo → Detalle ≠ heatmap.
    if getattr(it, "refuerzo", None) is not None and "ref_invested" in it.df.columns and len(it.df):
        _ri = float(it.df["ref_invested"].iloc[-1])
        _rv = float(it.df["ref_value"].iloc[-1])
        _ev = [_e for _e in it.refuerzo.get("events", []) if int(_e.get("idx", 0)) <= pos_idx]
        it.refuerzo = {**it.refuerzo, "gain": _rv - _ri, "invest": _ri, "n": len(_ev),
                       "events": _ev, "idxs": sorted({int(_e["idx"]) for _e in _ev}),
                       "n_call": sum(1 for _e in _ev if _e.get("leg") == "CALL"),
                       "n_put": sum(1 for _e in _ev if _e.get("leg") == "PUT")}
        try:
            it.final_total, it.max_total, it.min_total = _rv, float(it.df["ref_value"].max()), float(it.df["ref_value"].min())
        except Exception:
            pass
    else:
        _ser = pd.Series(0.0, index=it.df.index)
        if it.call_entry_premium:
            _ser = _ser + it.df["call_px"]
        if it.put_entry_premium:
            _ser = _ser + it.df["put_px"]
        try:
            it.final_total = float(_ser.iloc[-1])
            it.max_total = float(_ser.max())
            it.min_total = float(_ser.min())
        except Exception:
            pass


def apply_collective_exit(results: list, profit_frac=None, stop_frac=None,
                          stop_require_multi: bool = True) -> int:
    """Salida A NIVEL CARTERA en UNA pasada cronológica por DÍA: en cada minuto calcula el ROI de
    CARTERA de las abiertas (Σ ganancia $ / Σ invertido $ = el TOTAL en pantalla) y cierra TODAS las
    abiertas en ese minuto si:
      • profit_frac is not None y ROI >= profit_frac  → motivo 'collective_roi' (ROI colectivo), o
      • stop_frac   is not None y ROI <= stop_frac    → motivo 'collective_stop' (stop colectivo).
    Gana el PRIMER trigger del día (las ya cerradas no se reabren). stop_require_multi=True: el STOP
    solo dispara cuando hay >1 TICKER distinto abierto en ese minuto (con un solo ticker manda su stop
    individual). Las que ya salieron por su umbral/stop NO cuentan después. Muta los IterationResult.
    Devuelve cuántas cerró."""
    _by_day = defaultdict(list)
    for _r in results:
        _it = _r.get("iteration")
        if _it is None or getattr(_it, "df", None) is None or _it.df.empty:
            continue
        if "timestamp" not in _it.df.columns or "call_px" not in _it.df.columns:
            continue
        _by_day[_r.get("fecha")].append(_it)
    _n_closed = 0
    for _its in _by_day.values():
        _pos = []
        for _it in _its:
            _df = _it.df
            _ts = pd.to_datetime(_df["timestamp"])
            # Ganancia ($) por minuto + capital del día, CON refuerzo si aplica (= lo que muestra el
            # Detalle por señal). Martingala → ref_value/ref_invested (capital multi-tranche real).
            if getattr(_it, "refuerzo", None) is not None and "ref_roi" in _df.columns:
                _gain = _df["ref_value"] - _df["ref_invested"]
                _inv = float(_it.refuerzo.get("invest", _it.invest_call + _it.invest_put))
            else:
                _ce, _pe = _it.call_entry_premium, _it.put_entry_premium
                _ic, _ip = _it.invest_call, _it.invest_put
                _gc = (_ic * ((_df["call_px"] - _ce) / _ce)) if _ce else 0.0
                _gp = (_ip * ((_df["put_px"] - _pe) / _pe)) if _pe else 0.0
                _gain = _gc + _gp
                _inv = _ic + _ip
            _lut = {}
            for _i in range(len(_df)):
                _gv = float(_gain.iloc[_i]) if hasattr(_gain, "iloc") else float(_gain)
                _lut[_ts.iloc[_i]] = (_gv, _i)   # GANANCIA $ por minuto (refuerzo-ajustada si aplica)
            _pos.append({"it": _it, "entry": pd.Timestamp(_it.start_dt),
                         "exit": pd.Timestamp(_it.end_dt), "lut": _lut, "at": None,
                         "invest": _inv, "reason": None})
        _all_ts = sorted({_t for _p in _pos for _t in _p["lut"]})
        for _t in _all_ts:
            _open = [_p for _p in _pos
                     if _p["at"] is None and _p["entry"] <= _t <= _p["exit"] and _t in _p["lut"]]
            if not _open:
                continue
            # ROI de CARTERA = Σ(ganancia $ de las ABIERTAS) / Σ(capital de las ABIERTAS) = EXACTAMENTE
            # la fila TOTAL del heatmap (mismo numerador y denominador). Con refuerzo el "capital" es el
            # multi-tranche (it.invest_total) y la ganancia es ref_value−ref_invested. Así el corte usa
            # el MISMO número de cartera que ves en TOTAL, no el ROI de UNA pierna sola.
            _open_inv = sum(_p["invest"] for _p in _open)
            _coll = (sum(_p["lut"][_t][0] for _p in _open) / _open_inv) if _open_inv else 0.0
            _reason = None
            if profit_frac is not None and _coll >= profit_frac:
                _reason = "collective_roi"            # ganancia colectiva (umbral de ROI)
            elif stop_frac is not None and _coll <= stop_frac and (
                    not stop_require_multi or len({_p["it"].ticker for _p in _open}) > 1):
                _reason = "collective_stop"           # stop colectivo (solo con >1 ticker si se exige)
            if _reason:
                for _p in _open:
                    _p["at"] = _t                     # cerrada colectivamente en este minuto
                    _p["reason"] = _reason
        for _p in _pos:
            if _p["at"] is None:
                continue
            # El colectivo cierra: (a) ANTES de la salida natural, o (b) JUSTO en el minuto de salida
            # si la posición iba a correr hasta el cierre (session_end) — ahí el umbral colectivo se
            # cruzó al final, así que el motivo es «ROI colectivo», no «sin trigger». Si la posición ya
            # salía por su propio umbral/stop en ese minuto, se respeta ese motivo.
            _natural = getattr(_p["it"], "exit_reason", "")
            if _p["at"] < _p["exit"] or (_p["at"] == _p["exit"] and _natural == "session_end"):
                collective_close(_p["it"], _p["at"], _p["lut"][_p["at"]][1],
                                 reason=_p.get("reason") or "collective_roi")
                _n_closed += 1
    return _n_closed

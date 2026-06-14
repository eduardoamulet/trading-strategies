"""Lógica pura del backtest multi-iteración (cada iteración = una señal/fila).

Sin Streamlit → seguro para correr en hilos y testeable headless. La orquestación
en paralelo + la UI en vivo viven en app.py (que sí usa st.*). Acá solo:
  - run_one(dl, spec, ...): corre UNA iteración a partir de un spec
    {ticker, fecha, hora, tipo} y devuelve un dict de resultado.

Mapeo de Tipo → modo del motor (mismas etiquetas que el backtest manual):
  Sólo CALL/PUT → una pierna 100% · CALL y PUT / CALL o PUT (+ variantes 'plus') →
  dos piernas 50/50. Opción 1 (menor spread) · mismo día (sale 16:00). Inversión/
  Umbral/Stop y el override de spread son parámetros (defaults 1000 / 1000% / −100%).
"""
from __future__ import annotations

import json as _json
from datetime import time as _time
from pathlib import Path as _Path

import pandas as pd

from engine import (NoMatchError, run_next_iteration, run_overnight_1dte,
                    validate_0dte_session)

_HERE = _Path(__file__).parent
_TI_PATH = _HERE / "ticker_info.json"
TICKER_INFO = (_json.loads(_TI_PATH.read_text(encoding="utf-8")) if _TI_PATH.exists() else {})

REASON = {
    "100%_threshold": "Umbral de profit",
    "stop_loss": "Stop loss",
    "session_end": "Cierre (sin trigger)",
    "overnight_1dte": "Overnight 1DTE",
}


def to_ts(date_iso: str, t) -> pd.Timestamp:
    return pd.Timestamp(f"{date_iso} {t.hour:02d}:{t.minute:02d}", tz="America/New_York")


def premium_range(ticker: str):
    """Rango de prima (óptimo) por ticker desde ticker_info.json (÷100). Fallback 0.30–0.50."""
    info = TICKER_INFO.get((ticker or "").upper(), {}) or {}
    lo = (info.get("min") / 100.0) if info.get("min") is not None else 0.30
    hi = (info.get("max") / 100.0) if info.get("max") is not None else 0.50
    return float(lo), float(hi if hi > lo else lo + 0.05)


# Etiqueta de Tipo (dropdown) → modo del motor. Acepta también "CALL"/"PUT" sueltos
# (los manda el handoff de Alertas) → Sólo CALL / Sólo PUT.
_TIPO_MODE = {
    "CALL": "call_only", "SÓLO CALL": "call_only", "SOLO CALL": "call_only",
    "PUT": "put_only", "SÓLO PUT": "put_only", "SOLO PUT": "put_only",
    "CALL Y PUT": "both", "CALL Y PUT (PLUS)": "both_plus",
    "CALL Y PUT (REFUERZO)": "both_refuerzo",
    "CALL O PUT": "call_or_put", "CALL O PUT (PLUS)": "call_or_put_plus",
}


def run_one(dl, spec: dict, inversion: float = 1000.0, umbral_pct: float = 1000.0,
            stop_pct: float = -100.0, spread_cfg=None, iteration_idx: int = 1,
            entry_at_ask: bool = False, exit_at_bid: bool = False,
            auto_dte: bool = False, selection_criterion: str = "spread",
            refuerzo_loss_pct: float = 0.50, refuerzo_max: int = 2) -> dict:
    """Corre 1 iteración. `spec` admite 'ticker' o 'symbol', más 'fecha', 'hora', 'tipo'.
    `selection_criterion` = criterio de selección de contrato ('spread' = Opción 1 menor
    spread; 'itm_first' = Opción 2 primer contrato cerca de ITM, ignora spread y rango).
    NO usa st.* → seguro en hilos. Devuelve dict con status/iteration/error + datos base."""
    ticker = str(spec.get("ticker") or spec.get("symbol") or "").upper().strip()
    fecha = str(spec.get("fecha") or "").strip()
    hora_s = str(spec.get("hora") or "").strip()
    tipo = str(spec.get("tipo") or "").upper().strip()
    base = {"ticker": ticker, "fecha": fecha, "hora": hora_s, "tipo": tipo}
    mode = _TIPO_MODE.get(tipo)
    if mode is None or not ticker or not fecha:
        return {**base, "status": "error", "iteration": None,
                "error": f"iteración incompleta o Tipo inválido ('{tipo}')"}
    try:
        hh, mm = hora_s.split(":")[:2]
        entry = _time(int(hh), int(mm))
    except Exception:
        return {**base, "status": "error", "iteration": None, "error": f"hora inválida '{hora_s}'"}
    # Inversión: una pierna = 100%; dos piernas (both / call_or_put + plus) = 50/50.
    if mode == "call_only":
        inv_call, inv_put = float(inversion), 0.0
    elif mode == "put_only":
        inv_call, inv_put = 0.0, float(inversion)
    else:
        inv_call = inv_put = float(inversion) / 2.0
    lo, hi = premium_range(ticker)
    order_ts = to_ts(fecha, entry)
    day_end_ts = to_ts(fecha, _time(16, 0)) - pd.Timedelta(minutes=1)
    try:
        # Auto-DTE: si NO hay 0DTE para la fecha (ticker semanal en día no-viernes), usar
        # el vencimiento más cercano → compra `fecha`, vende a ese vencimiento (estilo DTE=1).
        _ovn_sell = None
        if auto_dte:
            try:
                _ne = dl.nearest_expiry(ticker, fecha)
            except Exception:
                _ne = None
            if _ne is None:
                return {**base, "status": "error", "iteration": None,
                        "error": f"sin vencimientos disponibles para {ticker} en/desde {fecha} "
                                 "(data reciente todavía no cargada en Polygon)"}
            if _ne != fecha:
                _ovn_sell = _ne   # no hay 0DTE ese día → vencimiento más cercano
        if _ovn_sell is not None:
            it = run_overnight_1dte(
                dl, ticker, fecha, lo, hi, inv_call, inv_put, order_ts,
                iteration_idx=int(iteration_idx), mode=mode, ext_min=lo, ext_max=hi,
                selection_criterion=selection_criterion, spread_cfg=spread_cfg, sell_date=_ovn_sell,
                exit_time=_time(16, 0), entry_at_ask=entry_at_ask, exit_at_bid=exit_at_bid)
        else:
            validate_0dte_session(dl, ticker, fecha)
            _umb = float(umbral_pct) / 100.0
            _stp = float(stop_pct) / 100.0
            # 'plus': ambas piernas se venden a más tardar a las 16:00 (Horario de salida).
            _plus_time = _time(16, 0) if mode in ("both_plus", "call_or_put_plus") else None
            it = run_next_iteration(
                dl, ticker, fecha, lo, hi, inv_call, inv_put, order_ts, day_end_ts,
                exit_threshold_pct=_umb, exit_metric="total", stop_loss_pct=_stp,
                iteration_idx=int(iteration_idx), mode=mode,
                refuerzo_loss_threshold_pct=float(refuerzo_loss_pct), refuerzo_max_count=int(refuerzo_max),
                ext_min=lo, ext_max=hi, selection_criterion=selection_criterion, dte=0, spread_cfg=spread_cfg,
                entry_at_ask=entry_at_ask, exit_at_bid=exit_at_bid,
                call_exit_threshold_pct=_umb, call_stop_loss_pct=_stp,
                put_exit_threshold_pct=_umb, put_stop_loss_pct=_stp,
                exit_plus_threshold_pct=_umb, exit_plus_time=_plus_time,
            )
        return {**base, "status": "ok", "iteration": it, "error": None}
    except NoMatchError as e:
        return {**base, "status": "error", "iteration": None,
                "error": f"Sin contrato (rango/spread): {e}"}
    except Exception as e:
        return {**base, "status": "error", "iteration": None, "error": str(e)}

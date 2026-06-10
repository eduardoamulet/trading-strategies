"""Lógica pura del backtest multi-iteración (cada iteración = una señal/fila).

Sin Streamlit → seguro para correr en hilos y testeable headless. La orquestación
en paralelo + la UI en vivo viven en app.py (que sí usa st.*). Acá solo:
  - run_one(dl, spec, ...): corre UNA iteración a partir de un spec
    {ticker, fecha, hora, tipo} y devuelve un dict de resultado.

Mapeo (fijo, como pidió el usuario para las alertas):
  Tipo=CALL → Sólo CALL · Tipo=PUT → Sólo PUT · 100% de la inversión a esa pierna ·
  Opción 1 (menor spread) · mismo día (sale a las 16:00). Inversión/Umbral/Stop y el
  override de spread son parámetros (defaults 1000 / 1000% / −100%).
"""
from __future__ import annotations

import json as _json
from datetime import time as _time
from pathlib import Path as _Path

import pandas as pd

from engine import NoMatchError, run_next_iteration, validate_0dte_session

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


def run_one(dl, spec: dict, inversion: float = 1000.0, umbral_pct: float = 1000.0,
            stop_pct: float = -100.0, spread_cfg=None, iteration_idx: int = 1,
            entry_at_ask: bool = False, exit_at_bid: bool = False) -> dict:
    """Corre 1 iteración. `spec` admite 'ticker' o 'symbol', más 'fecha', 'hora', 'tipo'.
    NO usa st.* → seguro en hilos. Devuelve dict con status/iteration/error + datos base."""
    ticker = str(spec.get("ticker") or spec.get("symbol") or "").upper().strip()
    fecha = str(spec.get("fecha") or "").strip()
    hora_s = str(spec.get("hora") or "").strip()
    tipo = str(spec.get("tipo") or "").upper().strip()
    base = {"ticker": ticker, "fecha": fecha, "hora": hora_s, "tipo": tipo}
    if tipo not in ("CALL", "PUT") or not ticker or not fecha:
        return {**base, "status": "error", "iteration": None,
                "error": "iteración incompleta (ticker/fecha/Tipo)"}
    try:
        hh, mm = hora_s.split(":")[:2]
        entry = _time(int(hh), int(mm))
    except Exception:
        return {**base, "status": "error", "iteration": None, "error": f"hora inválida '{hora_s}'"}
    mode = "call_only" if tipo == "CALL" else "put_only"
    inv_call = float(inversion) if mode == "call_only" else 0.0
    inv_put = float(inversion) if mode == "put_only" else 0.0
    lo, hi = premium_range(ticker)
    order_ts = to_ts(fecha, entry)
    day_end_ts = to_ts(fecha, _time(16, 0)) - pd.Timedelta(minutes=1)
    try:
        validate_0dte_session(dl, ticker, fecha)
        it = run_next_iteration(
            dl, ticker, fecha, lo, hi, inv_call, inv_put, order_ts, day_end_ts,
            exit_threshold_pct=float(umbral_pct) / 100.0, exit_metric="total",
            stop_loss_pct=float(stop_pct) / 100.0, iteration_idx=int(iteration_idx), mode=mode,
            ext_min=lo, ext_max=hi, selection_criterion="spread", dte=0, spread_cfg=spread_cfg,
            entry_at_ask=entry_at_ask, exit_at_bid=exit_at_bid,
        )
        return {**base, "status": "ok", "iteration": it, "error": None}
    except NoMatchError as e:
        return {**base, "status": "error", "iteration": None,
                "error": f"Sin contrato (rango/spread): {e}"}
    except Exception as e:
        return {**base, "status": "error", "iteration": None, "error": str(e)}

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
_TI_CACHE: dict = {"mtime": None, "data": {}}


def _ticker_info() -> dict:
    """ticker_info.json con caché por mtime: la sección Configuración lo edita y se re-lee
    en el siguiente backtest sin reiniciar el proceso."""
    try:
        mt = _TI_PATH.stat().st_mtime
    except OSError:
        mt = None
    if _TI_CACHE["mtime"] != mt:
        try:
            _TI_CACHE["data"] = _json.loads(_TI_PATH.read_text(encoding="utf-8")) if _TI_PATH.exists() else {}
        except Exception:
            _TI_CACHE["data"] = {}
        _TI_CACHE["mtime"] = mt
    return _TI_CACHE["data"]


TICKER_INFO = _ticker_info()   # compat (carga inicial); premium_range usa _ticker_info() fresco

REASON = {
    "100%_threshold": "Umbral de profit",
    "stop_loss": "Stop loss",
    "session_end": "Cierre (sin trigger)",
    "overnight_1dte": "Overnight 1DTE",
    "wrong_direction": "Señal en sentido del movimiento equivocado",
    "weak_confirmation": "Confirmación débil (vela doji, sin convicción)",
    "collective_roi": "ROI colectivo (cartera)",
    "collective_stop": "Stop colectivo (cartera)",
}


def to_ts(date_iso: str, t) -> pd.Timestamp:
    return pd.Timestamp(f"{date_iso} {t.hour:02d}:{t.minute:02d}", tz="America/New_York")


def premium_range(ticker: str):
    """Rango de prima (óptimo) por ticker desde ticker_info.json (÷100). Fallback 0.30–0.50."""
    info = _ticker_info().get((ticker or "").upper(), {}) or {}
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
    "CALL Y PUT (REFUERZO) (END OF DAY)": "both_refuerzo_eod",
    "CALL O PUT": "call_or_put", "CALL O PUT (PLUS)": "call_or_put_plus",
    "CALL O PUT (END OF DAY)": "call_or_put_eod",
    "SÓLO CALL (END OF DAY)": "call_only_eod", "SOLO CALL (END OF DAY)": "call_only_eod",
    "SÓLO PUT (END OF DAY)": "put_only_eod", "SOLO PUT (END OF DAY)": "put_only_eod",
}


def run_one(dl, spec: dict, inversion: float = 1000.0, umbral_pct: float = 1000.0,
            stop_pct: float = -100.0, spread_cfg=None, iteration_idx: int = 1,
            entry_at_ask: bool = False, exit_at_bid: bool = False,
            auto_dte: bool = False, selection_criterion: str = "spread",
            refuerzo_loss_pct: float = 0.50, refuerzo_max: int = 2,
            call_pct: float = 50.0, nbbo_timeline: bool = False,
            search_window_min: float = 0.0, dte: int = 0,
            exit_hora: str = "16:00", confirm_candle: bool = False,
            confirm_min_body_pct: float = 0.0,
            flip_on_wrong_direction: bool = False,
            cut_weak_confirmation: bool = True,
            apply_refuerzo: bool = False) -> dict:
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
    try:
        _eh, _em = str(exit_hora).split(":")[:2]
        exit_t = _time(int(_eh), int(_em))
    except Exception:
        exit_t = _time(16, 0)
    # Inversión por pierna: una pierna = 100%; dos piernas = según call_pct (% que va a la
    # CALL; el resto a la PUT). Default 50 → 50/50. NO hardcoded.
    if mode in ("call_only", "call_only_eod"):
        inv_call, inv_put = float(inversion), 0.0
    elif mode in ("put_only", "put_only_eod"):
        inv_call, inv_put = 0.0, float(inversion)
    else:
        _cp = max(0.0, min(100.0, float(call_pct))) / 100.0
        inv_call = float(inversion) * _cp
        inv_put = float(inversion) * (1.0 - _cp)
    lo, hi = premium_range(ticker)
    order_ts = to_ts(fecha, entry)
    day_end_ts = to_ts(fecha, exit_t) - pd.Timedelta(minutes=1)
    try:
        # Auto-DTE: si NO hay 0DTE para la fecha (ticker semanal en día no-viernes), usar
        # el vencimiento más cercano, pero lo reproduce INTRADÍA sobre `fecha` (entra y sale el
        # MISMO día al umbral/stop/cierre, NO lo retiene a vencimiento) → captura el intradía.
        _intraday_expiry = None
        if auto_dte:
            # ¿Hay 0DTE ese día? Chequeo RÁPIDO del cache (archivo de chain) — evita LISTAR todos
            # los vencimientos por API, que es lentísimo (SPY/QQQ/IWM tienen cientos → ~25s). Solo
            # si NO hay 0DTE se pide el vencimiento más cercano (ahí sí puede pegar a la API).
            _has0 = False
            try:
                _has0 = ((dl.data_dir / "chain" / f"{ticker}_{fecha}.parquet").exists()
                         or not dl.chain(ticker, fecha).empty)
            except Exception:
                _has0 = False
            if not _has0:
                try:
                    _ne = dl.nearest_expiry(ticker, fecha)
                except Exception:
                    _ne = None
                if _ne is None:
                    return {**base, "status": "error", "iteration": None,
                            "error": f"sin vencimientos disponibles para {ticker} en/desde {fecha} "
                                     "(data reciente todavía no cargada en Polygon)"}
                if _ne != fecha:
                    _intraday_expiry = _ne   # no hay 0DTE → contrato del venc. más cercano (intradía)
        # --- Filtro de CONFIRMACIÓN de vela: a los 15 min de la entrada, si la primera vela
        #     de 15m cerró EN CONTRA de la señal, se vende AHÍ con motivo "wrong_direction".
        #     Solo para señales de una pierna (CALL o PUT); en combinados la dirección es
        #     ambigua → no aplica. Solo intradía (dte=0, incluye Auto-DTE intradía). ---
        _cut_reason = None
        _flip = False
        if (confirm_candle and int(dte) == 0
                and mode in ("call_only", "put_only", "call_only_eod", "put_only_eod")):
            try:
                _und = dl.underlying(ticker, fecha)
                _cw_end = order_ts + pd.Timedelta(minutes=15)
                _win = _und[(_und["timestamp"] >= order_ts) & (_und["timestamp"] < _cw_end)]
                if not _win.empty:
                    _c_open = float(_win.iloc[0]["open"])
                    _c_close = float(_win.iloc[-1]["close"])
                    _is_call = mode in ("call_only", "call_only_eod")
                    # Cuerpo de la vela de confirmación, % en la DIRECCIÓN de la señal (>0 = a favor).
                    # Anti-DOJI: si el cuerpo no llega a `confirm_min_body_pct`, NO confirma → se corta.
                    # Banda muerta de ±umbral: cuerpo a FAVOR >= umbral → confirma (sigue). Cuerpo EN
                    # CONTRA < −umbral → "wrong_direction" (vela adversa DE VERDAD, único caso que
                    # invierte). |cuerpo| < umbral (incluye ruido tipo −0.01%) → "weak_confirmation"
                    # (doji/sin convicción): se CORTA pero NO se invierte → evita flips por ruido.
                    # Con umbral=0 equivale al comportamiento original (flipea con cualquier negativo).
                    _body = (_c_close - _c_open) / _c_open * 100.0 if _c_open else 0.0
                    _body_dir = _body if _is_call else -_body
                    _thr_body = float(confirm_min_body_pct)
                    # La salida por DOJI (weak_confirmation) solo si "Cerrar si confirmación débil" está
                    # ON y el modo NO es "End of Day" (esos corren hasta el cierre por diseño).
                    _cut_weak = bool(cut_weak_confirmation) and mode not in ("call_only_eod", "put_only_eod")
                    if _body_dir < _thr_body:
                        _adverse = _body_dir < -_thr_body   # movimiento adverso SIGNIFICATIVO (no ruido)
                        if _adverse:
                            _cut_reason = "wrong_direction"
                            # FLIP con vela adversa significativa (no doji/ruido): reemplaza por la pierna
                            # OPUESTA al cierre de la 1ª vela (entrada+15m) corriendo hasta el cierre del día.
                            if flip_on_wrong_direction:
                                _flip = True
                            else:
                                day_end_ts = _cw_end   # cortar al cierre de la vela de confirmación
                        elif _cut_weak:
                            # DOJI/sin convicción: cortar SOLO si el checkbox lo pide (y no es End of Day);
                            # si no, la operación NO corta por doji y sigue hasta su salida normal / cierre.
                            _cut_reason = "weak_confirmation"
                            day_end_ts = _cw_end
            except Exception:
                pass   # si falla la lectura del subyacente, no filtra (corre normal)
        if _flip:
            # Reemplazar la señal por la pierna OPUESTA, entrando a ~9:45 y corriendo al cierre.
            _opp = "PUT" if tipo == "CALL" else "CALL"
            if mode in ("call_only", "call_only_eod"):
                mode = "put_only_eod" if mode == "call_only_eod" else "put_only"
                inv_call, inv_put = 0.0, float(inversion)
            else:
                mode = "call_only_eod" if mode == "put_only_eod" else "call_only"
                inv_call, inv_put = float(inversion), 0.0
            order_ts = _cw_end
            base = {**base, "tipo": f"{tipo}→{_opp}", "hora": _cw_end.strftime("%H:%M")}
        # Siempre INTRADÍA (entra y sale el mismo día). Con Auto-DTE intradía, `_intraday_expiry`
        # apunta al contrato del venc. más cercano, pero igual sale ese día al umbral/stop/cierre.
        if int(dte) <= 0 and _intraday_expiry is None:
            validate_0dte_session(dl, ticker, fecha)   # 0DTE: validar. Auto-DTE: ya sabemos que no hay.
        _umb = float(umbral_pct) / 100.0
        _stp = float(stop_pct) / 100.0
        # 'plus': ambas piernas se venden a más tardar a las 16:00 (Horario de salida).
        _plus_time = exit_t if mode in ("both_plus", "call_or_put_plus") else None
        it = run_next_iteration(
            dl, ticker, fecha, lo, hi, inv_call, inv_put, order_ts, day_end_ts,
            exit_threshold_pct=_umb, exit_metric="total", stop_loss_pct=_stp,
            iteration_idx=int(iteration_idx), mode=mode,
            refuerzo_loss_threshold_pct=float(refuerzo_loss_pct), refuerzo_max_count=int(refuerzo_max),
            apply_refuerzo=bool(apply_refuerzo),
            ext_min=lo, ext_max=hi, selection_criterion=selection_criterion, dte=int(dte),
            overnight_exit_time=exit_t, spread_cfg=spread_cfg,
            entry_at_ask=entry_at_ask, exit_at_bid=exit_at_bid, nbbo_timeline=nbbo_timeline,
            search_window_min=search_window_min,
            call_exit_threshold_pct=_umb, call_stop_loss_pct=_stp,
            put_exit_threshold_pct=_umb, put_stop_loss_pct=_stp,
            exit_plus_threshold_pct=_umb, exit_plus_time=_plus_time,
            option_expiry=_intraday_expiry,
        )
        if _cut_reason and not _flip and it is not None:
            it.exit_reason = _cut_reason
            if getattr(it, "call_exit_reason", ""):
                it.call_exit_reason = _cut_reason
            if getattr(it, "put_exit_reason", ""):
                it.put_exit_reason = _cut_reason
        return {**base, "status": "ok", "iteration": it, "error": None}
    except NoMatchError as e:
        return {**base, "status": "error", "iteration": None,
                "error": f"Sin contrato (rango/spread): {e}"}
    except Exception as e:
        return {**base, "status": "error", "iteration": None, "error": str(e)}

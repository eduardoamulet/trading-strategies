"""Playbook — página de Herramientas: el veredicto por día de la semana (nivel CARTERA) con su
escenario/condiciones, el rango evaluado y la reevaluación en background.

Movido desde Configuración §6 (2026-07-03). La lógica vive en `playbook_store.py` (puro):
data/playbook.json + playbook_job.json; la reevaluación corre `run_ucbatch.py` en una consola
aparte (480 escenarios) y la tabla se actualiza sola al terminar. El panel de Backtesting consume
este playbook con la opción «(playbook automático)» de «Seleccionar configuración».
"""
from __future__ import annotations

import datetime as _dtm
import json
from pathlib import Path

import pandas as pd
import streamlit as st

import playbook_store as _pbs

HERE = Path(__file__).parent
TICKER_INFO_PATH = HERE / "ticker_info.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


st.title("📘 Playbook")
st.caption("El **playbook** dice qué días operar y con qué **escenario/condiciones** (evaluado "
           "a nivel **cartera** por el motor de interpretación). El panel de Backtesting lo "
           "aplica solo con la opción **«(playbook automático)»** de «Seleccionar "
           "configuración»: según el día de la semana de las fechas cargadas. Fuente de las "
           "condiciones: el template del batch (hoja «Backtesting scenarios»).")

# ── Job de reevaluación en curso / terminado ──
_st, _job = _pbs.check_job()
if _st == "done":
    with st.spinner("La reevaluación terminó — interpretando resultados y actualizando…"):
        try:
            _pbs.finalize_job()
            st.success("✅ Playbook actualizado con la reevaluación.")
        except Exception as _fe:  # noqa: BLE001
            st.error(f"El batch terminó pero no pude interpretar el results: {_fe}")
elif _st == "running":
    st.info(f"⏳ **Reevaluación corriendo** en una consola aparte (iniciada {_job.get('started')}) "
            f"· rango {_job.get('rango', ['?', '?'])[0]} → {_job.get('rango', ['?', '?'])[1]} · "
            f"{_job.get('n_dias', '?')} días × {_job.get('n_escenarios', '?')} escenarios "
            f"(~20 s/día). La tabla se actualiza sola al terminar.")
    if st.button("🔄 Chequear estado ahora", key="pb_check"):
        st.rerun()

# ── Tabla del playbook vigente ──
_pb = _pbs.load_playbook()
if not _pb:
    st.warning("Todavía no hay playbook guardado — corré una **Reevaluación** abajo (o generá "
               "un results detallado y cargalo en Backtesting → «Interpretar resultados»).")
else:
    st.markdown(f"**Evaluado del {_pb.get('evaluado_desde')} al {_pb.get('evaluado_hasta')}** · "
                f"{_pb.get('tickers')} · {_pb.get('n_dias', '?')} días hábiles · "
                f"{_pb.get('n_posiciones', 0):,} posiciones · generado {_pb.get('generado_en')}")
    if _pb.get("modo"):
        _cob = _pb.get("cobertura") or {}
        st.caption(f"🔁 Modo **{_pb['modo']}** · almacén acumulado: "
                   f"{_cob.get('desde')} → {_cob.get('hasta')} ({_cob.get('dias')} días en "
                   "data/bt_results.db) — se actualiza solo cada mañana con la Tarea Programada "
                   "(backtestea únicamente los días nuevos).")
    _REC_COLOR = {"OPERAR": "#16a34a", "NO OPERAR": "#dc2626"}
    _rows_pb = [{"Día": d, "Escenario": i.get("scenario"),
                 "WR %": i.get("win_rate"), "ROI cartera %": i.get("avg_roi"),
                 "Sharpe": i.get("sharpe"), "n": i.get("n"),
                 "Veredicto": i.get("recommendation"),
                 "Condiciones (del template)": i.get("config_txt", "—")}
                for d, i in (_pb.get("per_day") or {}).items()]
    st.dataframe(pd.DataFrame(_rows_pb).style.map(
        lambda v: f"color:{_REC_COLOR.get(v, '')};font-weight:700" if v in _REC_COLOR else "",
        subset=["Veredicto"]), use_container_width=True, hide_index=True)
    _pt = _pb.get("por_ticker") or []
    if _pt:
        try:
            _dtpb = pd.DataFrame(_pt)
            _pivpb = _dtpb.pivot(index="Ticker", columns="Día", values="Recomendación")
            _pivpb = _pivpb.reindex(columns=[c for c in ("Lun", "Mar", "Mié", "Jue", "Vie")
                                             if c in _pivpb.columns])
            st.caption("Desglose día × ticker (verde = OPERAR):")
            st.dataframe(_pivpb.style.map(
                lambda v: (f"background-color:{_REC_COLOR.get(v, '')}22;"
                           f"color:{_REC_COLOR.get(v, '')};font-weight:700")
                if v in _REC_COLOR else ""), use_container_width=True)
        except Exception:  # noqa: BLE001
            pass

# ── Reevaluar ──
st.divider()
st.markdown("**🔁 Reevaluar playbook** — corre el batch completo (480 escenarios) sobre el "
            "rango/tickers elegidos en una consola aparte y actualiza la tabla al terminar:")
_pb_c1, _pb_c2, _pb_c3 = st.columns([1, 1, 2])
_pb_d0 = _pb_c1.date_input("Desde", value=_dtm.date(2026, 4, 1), key="pb_d0")
_pb_d1 = _pb_c2.date_input("Hasta", value=_dtm.date(2026, 6, 24), key="pb_d1")
_ti_all = sorted(_read_json(TICKER_INFO_PATH, {}).keys()) or ["IWM", "QQQ", "SPY"]
_pb_tks = _pb_c3.multiselect("Tickers", _ti_all,
                             default=[t for t in ("QQQ", "SPY", "IWM") if t in _ti_all],
                             key="pb_tks")
if st.button("🔁 Reevaluar Playbook", type="primary", key="pb_reeval",
             disabled=(_st == "running") or not _pb_tks):
    try:
        if _pb_d1 < _pb_d0:
            st.error("Rango invertido: «Hasta» es anterior a «Desde».")
        else:
            _j = _pbs.launch_reevaluation(_pb_d0.isoformat(), _pb_d1.isoformat(), _pb_tks)
            st.success(f"🚀 Reevaluación lanzada en una consola aparte: {_j['n_dias']} días × "
                       f"{_j['n_escenarios']} escenarios (~{_j['n_dias'] * 20 // 60} min). "
                       "Podés seguir usando la app; esta tabla se actualiza al terminar.")
            st.rerun()
    except Exception as _le:  # noqa: BLE001
        st.error(f"No pude lanzar la reevaluación: {_le}")
st.caption("⚠️ Regla de oro (validada con feb–mar vs abr–jun): un día es confiable recién "
           "cuando el MISMO escenario pasa el gate en **dos ventanas consecutivas** — un "
           "veredicto de una sola ventana es in-sample.")

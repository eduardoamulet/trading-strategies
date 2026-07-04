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
                f"{_pb.get('n_posiciones', 0):,} posiciones · generado {_pb.get('generado_en')}"
                + (f" · 🧩 combinación **{_pb['combination_nombre']}**"
                   if _pb.get("combination_nombre") else ""))
    if _pb.get("modo"):
        _cob = _pb.get("cobertura") or {}
        st.caption(f"🔁 Modo **{_pb['modo']}** · almacén acumulado: "
                   f"{_cob.get('desde')} → {_cob.get('hasta')} ({_cob.get('dias')} días en "
                   "data/bt_results.db) — se actualiza solo cada mañana con la Tarea Programada "
                   "(backtestea únicamente los días nuevos).")
    _REC_COLOR = {"OPERAR": "#16a34a", "NO OPERAR": "#dc2626"}
    _EST_ICON = {"operable": "🟢 operable", "candidato": "🟡 candidato",
                 "suspendido": "⏸ suspendido", "sin ventaja": "— sin ventaja"}
    _tiene_estado = any(i.get("estado") for i in (_pb.get("per_day") or {}).values())
    _rows_pb = [{"Día": d, "Escenario": i.get("scenario"),
                 "WR %": i.get("win_rate"),
                 **({"WR P5 %": i.get("wr_p5")} if _tiene_estado else {}),
                 "ROI cartera %": i.get("avg_roi"),
                 "Sharpe": i.get("sharpe"), "n": i.get("n"),
                 "Veredicto": i.get("recommendation"),
                 **({"Estado": _EST_ICON.get(i.get("estado"), i.get("estado") or "—")}
                    if _tiene_estado else {}),
                 "Condiciones (del template)": i.get("config_txt", "—")}
                for d, i in (_pb.get("per_day") or {}).items()]
    st.dataframe(pd.DataFrame(_rows_pb).style.map(
        lambda v: f"color:{_REC_COLOR.get(v, '')};font-weight:700" if v in _REC_COLOR else "",
        subset=["Veredicto"]), use_container_width=True, hide_index=True)
    _rv = _pb.get("regimen_vol")
    if _rv and _rv.get("alerta"):
        st.warning(f"🌊 **Régimen de volatilidad alterado** ({_rv.get('ticker')}): la vol "
                   f"realizada de las últimas 5 sesiones es **{_rv.get('ratio')}×** la mediana "
                   "de 60 sesiones (>2×). Los estados no-operables siguen bloqueados; considerá "
                   "reducir tamaño en los 🟢 hasta que normalice.")
    if _pb.get("revision_anticipada"):
        st.warning("⏰ **CUSUM fuera de banda** en ≥1 día: el ROI realizado viene sistemáticamente "
                   "por debajo del prometido — corresponde una revisión anticipada de parámetros "
                   "(`python window_sweep.py`).")
    if _tiene_estado:
        st.caption("**WR P5 %** = límite inferior creíble (percentil 5 de la posterior Beta) del "
                   "win-rate — el gate exige P5>55%, no el WR puntual (inmune a muestras chicas). "
                   "**Estado** (máquina de supervivencia sobre las dos mitades de la ventana): "
                   "🟢 operable = pasa el gate en ambas mitades (el automático SOLO aplica estos) · "
                   "🟡 candidato = pasa solo en la reciente (edge sin confirmar) · ⏸ suspendido = "
                   "edge decaído, kill-switch (3 sesiones seguidas perdedoras / ROI acumulado "
                   "≤ −15%), calibración rota o churn · — sin ventaja.")
        with st.expander("ℹ️ Motivo del estado por día"):
            for _d, _i in (_pb.get("per_day") or {}).items():
                if _i.get("estado_motivo"):
                    st.markdown(f"- **{_d}** ({_EST_ICON.get(_i.get('estado'), '?')}): "
                                f"{_i['estado_motivo']}")
        with st.expander("🩺 Monitores de vigencia (calibración · CUSUM · churn · retador)"):
            _mrows = []
            for _d, _i in (_pb.get("per_day") or {}).items():
                _ch = _i.get("churn") or {}
                _rt = _i.get("retador") or {}
                _mrows.append({
                    "Día": _d,
                    "WR reciente (10) %": _i.get("wr_reciente", "—"),
                    "P5 prometido %": _i.get("wr_p5", "—"),
                    "Calibración": ("❌ rota" if _i.get("calib_alerta")
                                    else ("✓" if _i.get("wr_reciente") is not None else "—")),
                    "CUSUM (dev / banda)": (f"{_i.get('cusum_dev')} / {_i.get('cusum_umbral')}"
                                            + (" ⚠" if _i.get("cusum_alerta") else "")
                                            if _i.get("cusum_dev") is not None else "—"),
                    "Churn campeón": (f"{int(_ch['tasa'] * 100)}% de {_ch['n']}"
                                      + (" ❌" if _ch.get("alerta") else "")
                                      if _ch.get("tasa") is not None
                                      else f"n={_ch.get('n', 0)} (insuf.)"),
                    "Retador": (f"{_rt.get('scenario')} ({_rt.get('racha')}/5)" if _rt else "—"),
                })
            st.dataframe(pd.DataFrame(_mrows), use_container_width=True, hide_index=True)
            st.caption("**Calibración**: WR realizado de las últimas 10 sesiones del campeón vs "
                       "su P5 prometido — por debajo del piso creíble → el día se suspende. "
                       "**CUSUM**: Σ(ROI−prometido) de 10 sesiones vs la banda −2σ√10 — fuera de "
                       "banda NO suspende, pide revisión anticipada de parámetros. **Churn**: % "
                       "de re-agregaciones en que cambió el campeón (>30% suspende por "
                       "fragilidad). **Retador**: escenario operable que domina al campeón en P5 "
                       "— lo reemplaza recién tras 5 re-agregaciones consecutivas dominando "
                       "(histéresis anti flip-flop); si el campeón muere, la promoción es "
                       "inmediata.")
    _reg = _pb.get("regimen") or []
    if _reg:
        with st.expander("🌡 Régimen de mercado (md_score a la entrada) — informativo"):
            st.caption("P(ganar) del escenario elegido según el régimen ex-ante del Market "
                       "Direction Engine (promedio del día). **No condiciona el gate** — con "
                       "pocos meses el n por bucket es chico: leer como tendencia.")
            st.dataframe(pd.DataFrame(_reg), use_container_width=True, hide_index=True)
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

# ── Combinaciones de Backtesting ──
st.divider()
st.markdown("## 🧩 Combinaciones de Backtesting")
st.caption("Cada combinación nace de un **Excel de variables** (columnas = variables, filas = "
           "valores posibles) y genera sus escenarios como el **producto cartesiano completo** "
           "de los valores. La combinación **ACTIVA** es la que usan el job diario de las 05:00 "
           "y la Reevaluación. Reemplaza al template estático de 480 escenarios (migrado como "
           "«Template 480 (legacy)»).")
import combinations as _cmb

try:
    _cmb.ensure_legacy()                    # migración única del template 480 → combinations.db
except Exception as _me:  # noqa: BLE001
    st.warning(f"No pude migrar el template legacy: {_me}")

_up_comb = st.file_uploader("📤 Importar nueva combinación (Excel de variables)", type=["xlsx"],
                            key="comb_upload", accept_multiple_files=True,
                            help="Hoja «Backtesting variables»: columnas = variables (seed + "
                                 "condiciones), filas = valores posibles de cada una. Cada "
                                 "archivo crea una combinación NUEVA (no sobrescribe nada). "
                                 "Podés arrastrar VARIOS a la vez: se importa uno por archivo.")
if _up_comb and st.button(f"➕ Importar combinación{'es' if len(_up_comb) > 1 else ''} "
                          f"({len(_up_comb)})", key="comb_import", type="primary"):
    _errs = 0
    for _f_comb in _up_comb:
        try:
            _reg = _cmb.import_file(_f_comb)
            _n_esp = _cmb.expected_scenarios(_reg)
            st.success(f"✅ **{_reg['nombre']}** importada — generará **{_n_esp:,}** escenarios "
                       "(producto cartesiano). Tocá «⚙️ Generar escenarios» en su contenedor.")
        except ValueError as _ie:
            _errs += 1
            st.error(f"❌ `{_f_comb.name}`: {_ie}")
    if not _errs:
        st.rerun()          # con errores NO re-corremos: que los mensajes queden legibles

_activa = _cmb.active_combination()
_combos = _cmb.list_combinations()
if not _combos:
    st.info("No hay combinaciones todavía — importá un Excel de variables arriba.")
_EST_COMB = {"importada": "📥 importada", "generada": "✅ generada"}
for _co in _combos:
    _es_activa = _co["id"] == _activa
    _titulo = (f"🧩 {_co['nombre']}"
               + (" · ⭐ ACTIVA" if _es_activa else "")
               + f" — {_EST_COMB.get(_co['estado'], _co['estado'])}"
               + (f" · {_co['n_escenarios']:,} escenarios" if _co["n_escenarios"] else ""))
    with st.expander(_titulo, expanded=False):
        _esp = _cmb.expected_scenarios(_co)
        st.markdown(f"**Archivo**: `{_co['archivo']}` · **Creada**: {_co['creado_en']} · "
                    f"**Estado**: {_co['estado']}"
                    + (f" · **Generada**: {_co['generado_en']}" if _co.get("generado_en") else ""))
        # 📋 Información IMPORTADA del archivo: seed (globales) + variables con TODOS sus valores.
        _seed_co = dict(_co.get("seed") or {})
        if _seed_co:
            _SEED_LBL = {"tickers": "Tickers", "fecha_inicial": "Fecha inicial",
                         "fecha_final": "Fecha final"}
            _seed_rows = [{"Parámetro": _SEED_LBL.get(_k, _k),
                           "Valor": ", ".join(_v) if isinstance(_v, list) else str(_v)}
                          for _k, _v in _seed_co.items()]
            st.markdown("**📋 Seed importado** — globales de la corrida (fechas/tickers se "
                        "overridean en cada corrida):")
            st.dataframe(pd.DataFrame(_seed_rows), hide_index=True, use_container_width=True,
                         height=min(38 + 35 * len(_seed_rows), 320))
        if _co["id"] == _cmb.LEGACY_ID:
            st.caption("Migración del template estático — sus 480 escenarios conservan los IDs "
                       "originales (C001–C480) y toda la historia del almacén les pertenece.")
        elif _co.get("variables"):
            _var_rows = [{"Variable": _k, "n": len(_v),
                          "Valores": (" · ".join(_v) if any(str(x).strip() for x in _v)
                                      else "(vacía — no participa)")}
                         for _k, _v in _co["variables"].items()]
            st.markdown("**🧬 Variables importadas** — el producto cartesiano de sus valores "
                        f"genera los escenarios (= **{_esp:,}**):")
            st.dataframe(pd.DataFrame(_var_rows), hide_index=True, use_container_width=True,
                         height=min(38 + 35 * len(_var_rows), 600))
            if _esp > 50_000:
                st.warning(f"⚠️ {_esp:,} escenarios: el batch de UN día son ~{_esp * 3:,} "
                           "backtests — el job diario puede tardar bastante y el almacén crece "
                           "rápido. Considerá recortar valores en el Excel.")
        st.markdown(f"**Escenarios generados: {_co['n_escenarios']:,}**")
        _ac1, _ac2, _ac3 = st.columns(3)
        if _co["id"] != _cmb.LEGACY_ID and _ac1.button(
                f"⚙️ Generar escenarios ({_esp:,})", key=f"comb_gen_{_co['id']}",
                help="Producto cartesiano completo → se persisten en la base (re-generar "
                     "reemplaza los de ESTA combinación)."):
            with st.spinner(f"Generando {_esp:,} escenarios…"):
                _n_gen = _cmb.generate_scenarios(_co["id"])
            st.success(f"✅ {_n_gen:,} escenarios generados y persistidos.")
            st.rerun()
        if not _es_activa and _ac2.button(
                "⭐ Usar en el playbook", key=f"comb_act_{_co['id']}",
                disabled=(_co["estado"] != "generada" or not _co["n_escenarios"]),
                help="La vuelve la combinación ACTIVA: el job diario, la Reevaluación y el "
                     "veredicto del playbook pasan a usar SUS escenarios (el almacén y el "
                     "veredicto están segregados por combinación)."):
            _cmb.set_active(_co["id"])
            st.success(f"⭐ «{_co['nombre']}» es ahora la combinación activa.")
            st.rerun()
        if not _es_activa and _ac3.button("🗑 Eliminar", key=f"comb_del_{_co['id']}",
                                          help="Borra la combinación y sus escenarios de la "
                                               "base (su historia en el almacén NO se borra)."):
            try:
                _cmb.delete_combination(_co["id"])
                st.rerun()
            except ValueError as _de:
                st.error(str(_de))

# ── Reevaluar ──
st.divider()
_c_act = _cmb.get_combination(_activa) if _activa else None
st.markdown("**🔁 Reevaluar playbook** — corre el batch completo de la combinación **activa** "
            + (f"(«{_c_act['nombre']}», {_c_act['n_escenarios']:,} escenarios) " if _c_act else "")
            + "sobre el rango/tickers elegidos en una consola aparte y actualiza la tabla al "
              "terminar:")
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

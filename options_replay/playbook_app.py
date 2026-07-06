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


@st.cache_data(ttl=3600, show_spinner=False)
def _cov_ligera(_cid: str) -> dict:
    """Cobertura LIGERA por combinación para las tarjetas (filas · días · última fecha).
    bt_store.coverage() corre 3 escaneos completos por combinación (y el de engine_version lee
    la TABLA de 4 GB, no el índice) — medido 2026-07-06: ~288 s de page-load con 5 tarjetas
    (185 s solo la de 4.6M filas). Acá: MAX(fecha) por seek de índice (instantáneo) + UN solo
    recorrido index-only para contar (~20 s la gigante, una vez), cacheado 1 h. El almacén es
    append-only: la cobertura solo cambia al ingestar o borrar — en esos eventos la página
    llama `_cov_ligera.clear()`, así el TTL largo nunca miente."""
    import bt_store as _b
    with _b._connect() as _con:  # noqa: SLF001 — mismo WAL/busy_timeout que el resto del módulo
        _hasta = _con.execute("SELECT MAX(fecha) FROM bt_results WHERE combination=?",
                              (_cid,)).fetchone()[0]
        if not _hasta:
            return {"filas": 0, "dias": 0, "hasta": None}
        _n, _nd = _con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT fecha) FROM bt_results WHERE combination=?",
            (_cid,)).fetchone()
    return {"filas": int(_n or 0), "dias": int(_nd or 0), "hasta": _hasta}


st.title("📘 Playbook")
st.caption("El **playbook** dice qué días operar y con qué **escenario/condiciones** (evaluado "
           "a nivel **cartera** por el motor de interpretación). El panel de Backtesting lo "
           "aplica solo con la opción **«(playbook automático)»** de «Seleccionar "
           "configuración»: según el día de la semana de las fechas cargadas. Fuente de las "
           "condiciones: el template del batch (hoja «Backtesting scenarios»).")

# ── Job de reevaluación en curso / terminado (estado en reeval_runs, no en archivos) ──
_st, _job = _pbs.check_job()
if _st == "done":
    # ASÍNCRONO: el veredicto de una combinación gigante tarda minutos en interpretarse —
    # se delega a un proceso aparte (claim atómico: N sesiones → 1 solo finalizador) y la
    # página queda libre al instante.
    _cov_ligera.clear()          # acaba de aterrizar una ingesta → recontar la cobertura
    _pbs.spawn_finalize(_job["id"])
    st.info(f"⏳ Reevaluación **{_job.get('id')}** terminada "
            f"(+{(_job or {}).get('n_filas_nuevas') or 0:,} filas ya en el almacén) — "
            "**interpretando el veredicto en segundo plano**. La página queda libre; "
            "refrescá en unos minutos y la tabla aparece actualizada.")
    if st.button("🔄 Chequear estado ahora", key="pb_fin_check"):
        st.rerun()
elif _st == "finalizing":
    st.info(f"⏳ **Interpretando el veredicto** de la reevaluación {_job.get('id')} en segundo "
            f"plano ({(_job.get('n_filas_nuevas') or 0):,} filas) — la página queda libre. "
            "Con combinaciones gigantes puede tardar unos minutos.")
    if st.button("🔄 Chequear estado ahora", key="pb_fin_check2"):
        st.rerun()
elif _st == "running":
    st.info(f"⏳ **Reevaluación corriendo** en una consola aparte (iniciada {_job.get('started')}) "
            f"· rango {_job.get('rango', ['?', '?'])[0]} → {_job.get('rango', ['?', '?'])[1]} · "
            f"{_job.get('n_dias', '?')} días × {_job.get('n_escenarios', '?')} escenarios · "
            "ingesta directa al almacén. La tabla se actualiza sola al terminar.")
    if st.button("🔄 Chequear estado ahora", key="pb_check"):
        st.rerun()
elif _st == "failed":
    st.error(f"❌ La reevaluación **{_job.get('id')}** de «{_job.get('combination_nombre')}» "
             f"terminó **{_job.get('estado')}**: {_job.get('error') or 'sin detalle'} — "
             "el detalle queda en el historial de ejecuciones (abajo).")
    if st.button("Descartar aviso", key="pb_fail_ack"):
        import bt_store as _bts_ack
        _bts_ack.mark_run_finalized(_job["id"])
        st.rerun()

# ── Playbook vigente: SÍNTESIS + monitores (las TABLAS viven en la tarjeta de cada
#    combinación, abajo — pedido UX 2026-07-05: no duplicarlas acá) ──
_pb = _pbs.load_playbook()
if not _pb:
    st.warning("Todavía no hay playbook guardado — corré una **Reevaluación** abajo (o generá "
               "un results detallado y cargalo en Backtesting → «Interpretar resultados»).")
else:
    st.markdown(f"**Vigente** (lo aplica «(playbook automático)» en Backtesting): "
                f"🧩 **{_pbs.display_name(_pb)}** · evaluado "
                f"{_pb.get('evaluado_desde')} → {_pb.get('evaluado_hasta')} · "
                f"{_pb.get('n_posiciones', 0):,} posiciones · generado {_pb.get('generado_en')}")
    _op_vig = [d for d, i in (_pb.get("per_day") or {}).items()
               if str(i.get("recommendation", "")).upper() == "OPERAR"
               and i.get("estado") in (None, "operable")]
    st.caption(("🟢 Días que el automático aplicaría hoy: **" + ", ".join(_op_vig) + "**"
                if _op_vig else "🔴 Hoy el automático no aplicaría ningún día (ninguno OPERAR "
                                "+ operable).")
               + " · Las tablas completas del veredicto están en la **tarjeta de cada "
                 "combinación** (más abajo).")
    _EST_ICON = {"operable": "🟢 operable", "candidato": "🟡 candidato",
                 "suspendido": "⏸ suspendido", "sin ventaja": "— sin ventaja"}
    _tiene_estado = any(i.get("estado") for i in (_pb.get("per_day") or {}).values())
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
        with st.expander("ℹ️ Motivo del estado por día (y cómo leer P5/estados)"):
            st.caption("**WR P5 %** = límite inferior creíble (percentil 5 de la posterior Beta) "
                       "del win-rate — el gate exige P5>55%, no el WR puntual (inmune a muestras "
                       "chicas). **Estado**: 🟢 operable = pasa el gate en las DOS mitades de la "
                       "ventana (el automático SOLO aplica estos) · 🟡 candidato = pasa solo en "
                       "la reciente · ⏸ suspendido = edge decaído, kill-switch (3 sesiones "
                       "seguidas perdedoras / ROI acumulado ≤ −15%), calibración rota o churn · "
                       "— sin ventaja.")
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

# ⭐ Selector de la ACTIVA fuera de las tarjetas (pedido UX: sin expandir ni buscar el botón).
# Equivale a «⭐ Usar en el playbook»; solo lista combinaciones con escenarios generados.
_elegibles = [c for c in _combos if c.get("estado") == "generada" and c.get("n_escenarios")]
if _elegibles:
    _nom_el = {}
    for _c_el in _elegibles:                      # nombres duplicados → sufijo con el id corto
        _n_el = _pbs.display_name(_c_el.get("nombre") or _c_el["id"])
        if sum(1 for x in _elegibles
               if _pbs.display_name(x.get("nombre") or x["id"]) == _n_el) > 1:
            _n_el += f" · {_c_el['id'][-6:]}"
        _nom_el[_c_el["id"]] = _n_el
    _ids_el = list(_nom_el)
    # SINCRONIZAR el widget con la activa REAL antes de instanciarlo: si la activa cambió por
    # OTRA vía (botón «⭐ Usar en el playbook» de una tarjeta, CLI), el valor viejo guardado en
    # session_state la revertía en el próximo rerun (el selectbox «ganaba» siempre). Con la
    # siembra previa + on_change, el selectbox solo actúa cuando el USUARIO lo toca.
    if _activa in _ids_el and st.session_state.get("comb_activa_sel") != _activa:
        st.session_state["comb_activa_sel"] = _activa

    def _cambiar_activa_cb():
        _nv = st.session_state.get("comb_activa_sel")
        if _nv and _nv != _cmb.active_combination():
            try:
                _cmb.set_active(_nv)
                _pbs.promote_active_playbook(_nv)   # la síntesis refleja a la nueva activa YA
            except ValueError as _ae:  # noqa: BLE001
                st.session_state["_comb_act_err"] = str(_ae)

    st.selectbox(
        "⭐ Combinación ACTIVA — la usan el job diario de las 05:00, la Reevaluación y el "
        "veredicto del playbook",
        _ids_el, index=(_ids_el.index(_activa) if _activa in _ids_el else 0),
        format_func=lambda cid: _nom_el.get(cid, cid), key="comb_activa_sel",
        on_change=_cambiar_activa_cb,
        help="Cambiarla acá equivale al botón «⭐ Usar en el playbook» de cada tarjeta. El "
             "almacén y el veredicto están segregados por combinación — no se mezcla nada.")
    if st.session_state.pop("_comb_act_err", None):
        st.error(st.session_state.get("_comb_act_err") or "No se pudo activar esa combinación.")

def _render_tablas_playbook(pb_c: dict) -> None:
    """Las DOS tablas del veredicto (por día de la semana + desglose día×ticker) — mismo
    formato que la sección principal, en versión compacta para las tarjetas de combinación."""
    _RC = {"OPERAR": "#16a34a", "NO OPERAR": "#dc2626"}
    _EI = {"operable": "🟢 operable", "candidato": "🟡 candidato",
           "suspendido": "⏸ suspendido", "sin ventaja": "— sin ventaja"}
    _te = any(i.get("estado") for i in (pb_c.get("per_day") or {}).values())
    _rows = [{"Día": d, "Escenario": i.get("scenario"), "WR %": i.get("win_rate"),
              **({"WR P5 %": i.get("wr_p5")} if _te else {}),
              "ROI cartera %": i.get("avg_roi"), "Sharpe": i.get("sharpe"), "n": i.get("n"),
              "Veredicto": i.get("recommendation"),
              **({"Estado": _EI.get(i.get("estado"), i.get("estado") or "—")} if _te else {}),
              "Condiciones (del template)": i.get("config_txt", "—")}
             for d, i in (pb_c.get("per_day") or {}).items()]
    if not _rows:
        st.caption("El veredicto no tiene días evaluados.")
        return
    st.dataframe(pd.DataFrame(_rows).style.map(
        lambda v: f"color:{_RC.get(v, '')};font-weight:700" if v in _RC else "",
        subset=["Veredicto"]), use_container_width=True, hide_index=True)
    _ptc = pb_c.get("por_ticker") or []
    if _ptc:
        try:
            _dtc = pd.DataFrame(_ptc)
            _pvc = _dtc.pivot(index="Ticker", columns="Día", values="Recomendación")
            _pvc = _pvc.reindex(columns=[c for c in ("Lun", "Mar", "Mié", "Jue", "Vie")
                                         if c in _pvc.columns])
            st.caption("Desglose día × ticker (verde = OPERAR):")
            st.dataframe(_pvc.style.map(
                lambda v: (f"background-color:{_RC.get(v, '')}22;"
                           f"color:{_RC.get(v, '')};font-weight:700") if v in _RC else ""),
                use_container_width=True)
        except Exception:  # noqa: BLE001
            pass


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
            _pbs.promote_active_playbook(_co["id"])   # síntesis coherente con la nueva activa
            st.success(f"⭐ «{_co['nombre']}» es ahora la combinación activa.")
            st.rerun()
        if not _es_activa and _ac3.button("🗑 Eliminar", key=f"comb_del_{_co['id']}",
                                          help="Borrado TOTAL: la combinación, sus escenarios "
                                               "Y su historia del almacén. Pide confirmación."):
            st.session_state[f"comb_del_arm_{_co['id']}"] = True
        # Confirmación en dos pasos: borrar una combinación arrastra su HISTORIA del almacén
        # (filas de bt_results) — mostrar cuánto se va antes de tocar nada. Sin deshacer.
        if st.session_state.get(f"comb_del_arm_{_co['id']}"):
            import bt_store as _bts
            _n_hist = _bts.coverage(_co["id"])["filas"]
            st.warning(f"⚠️ **Borrado TOTAL de «{_co['nombre']}»**: {_co['n_escenarios']:,} "
                       f"escenario(s) + **{_n_hist:,} fila(s) de historia** en el almacén. "
                       "No hay deshacer.")
            _cd1, _cd2, _ = st.columns([1.8, 1, 3.6])
            if _cd1.button("🗑 Confirmar borrado total", key=f"comb_del_go_{_co['id']}",
                           type="primary"):
                try:
                    _cmb.delete_combination(_co["id"])          # valida que NO sea la activa
                    _bts.delete_rows(_co["id"])                 # purga la historia del almacén
                    _cov_ligera.clear()                         # la cobertura cacheada cambió
                    st.session_state.pop(f"comb_del_arm_{_co['id']}", None)
                    st.rerun()
                except ValueError as _de:
                    st.error(str(_de))
            if _cd2.button("Cancelar", key=f"comb_del_no_{_co['id']}"):
                st.session_state.pop(f"comb_del_arm_{_co['id']}", None)
                st.rerun()

        # ── 📊 El veredicto de ESTA combinación (sus dos tablas), persistido en su json ──
        st.markdown("---")
        st.markdown("**📊 Veredicto de esta combinación** · ventana oficial 120 días hábiles · "
                    "half-life 35 · min_n 16")
        _cov_c = _cov_ligera(_co["id"])
        if not _cov_c["filas"]:
            st.caption("Sin historia en el almacén todavía — activala y lanzá una Reevaluación "
                       "para poblarla; después el veredicto se calcula acá.")
        else:
            _pb_c = _pbs.load_playbook_for(_co["id"])
            if _pb_c:
                st.caption(f"Generado {_pb_c.get('generado_en')} · evaluado "
                           f"{_pb_c.get('evaluado_desde')} → {_pb_c.get('evaluado_hasta')} · "
                           f"almacén hasta {_cov_c.get('hasta')} ({_cov_c['dias']} días)")
                _render_tablas_playbook(_pb_c)
            else:
                st.caption(f"Historia disponible ({_cov_c['filas']:,} filas · {_cov_c['dias']} "
                           "días) — veredicto todavía sin calcular.")
            if st.button(("🔄 Recalcular veredicto" if _pb_c else "📊 Calcular veredicto"),
                         key=f"comb_pb_{_co['id']}",
                         help="Ventana oficial sobre el almacén de ESTA combinación → se guarda "
                              "en data/playbook_<id>.json (no toca el playbook vigente). Con "
                              "combinaciones gigantes tarda varios minutos."):
                with st.spinner(f"Calculando veredicto de "
                                f"«{_pbs.display_name(_co.get('nombre'))}» — con millones de "
                                "filas son varios minutos…"):
                    try:
                        _pbs.build_and_save_for(_co["id"])
                        st.rerun()
                    except Exception as _pe:  # noqa: BLE001
                        st.error(f"No pude calcular el veredicto: {_pe}")

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

# ── Historial de ejecuciones (reeval_runs): cada corrida registrada en la base ──
with st.expander("📜 Historial de ejecuciones (reevaluaciones · incrementales · verify)",
                 expanded=False):
    import bt_store as _bts_hist
    _runs_hist = _bts_hist.runs_for(limit=40)
    if not _runs_hist:
        st.caption("Sin ejecuciones registradas todavía — la primera reevaluación o el próximo "
                   "job de las 05:00 estrenan la tabla.")
    else:
        _EST_RUN = {"exitosa": "✅ exitosa", "fallida": "❌ fallida",
                    "corriendo": "⏳ corriendo", "abortada": "⚠️ abortada"}
        _hrows = [{"Inicio": r.get("started_at"), "Tipo": r.get("tipo"),
                   "Combinación": _pbs.display_name(r.get("combination_nombre")
                                                    or r.get("combination")),
                   "Estado": _EST_RUN.get(r.get("estado"), r.get("estado")),
                   "Duración (s)": (round(r["duration_s"]) if r.get("duration_s") else None),
                   "Rango": f"{r.get('fecha_desde') or '—'} → {r.get('fecha_hasta') or '—'}",
                   "Filas nuevas": r.get("n_filas_nuevas"),
                   "Posiciones": r.get("n_filas"), "Errores": r.get("n_err"),
                   "Detalle": (r.get("error_msg") or "")[:90]} for r in _runs_hist]
        st.dataframe(pd.DataFrame(_hrows), hide_index=True, use_container_width=True,
                     height=min(38 + 35 * len(_hrows), 430))

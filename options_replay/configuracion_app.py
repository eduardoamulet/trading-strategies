"""Configuración — tablas editables (no hardcodeadas) que alimentan el motor de backtest:
  1) Filtro de spread por strike (Opción 1)  → strike_spread_config.json
  2) Rango de precios de opción por ticker    → ticker_info.json

Se guardan a JSON; el motor los toma en el SIGUIENTE backtest (ambos loaders cachean por
mtime, así que no hace falta reiniciar la app).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

import ticker_prefs as _tp

HERE = Path(__file__).parent
STRIKE_CFG_PATH = HERE / "strike_spread_config.json"
TICKER_INFO_PATH = HERE / "ticker_info.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


st.title("⚙️ Configuración")
st.caption("Estas tablas alimentan el motor de backtest (Opción 1). Se guardan a JSON — no están "
           "hardcodeadas — y el motor las toma en el **siguiente backtest** (sin reiniciar).")

# ═════════════ 1) Filtro de spread por precio del contrato (ASK) ═════════════
with st.expander("1 · Filtro de spread por precio del contrato (ASK) — Opción 1", expanded=False):
    _sc = _read_json(STRIKE_CFG_PATH, {"enabled": True, "buckets": []})
    _enabled = st.checkbox(
        "Filtro de spread por precio del contrato (ASK) **activo**", value=bool(_sc.get("enabled", True)),
        help="Si lo desactivás, Opción 1 no filtra por spread (cae a la compuerta por precio del subyacente).")
    _buckets = _sc.get("buckets", [])
    _df_s = (pd.DataFrame(_buckets) if _buckets else
             pd.DataFrame(columns=["ask_min", "ask_max", "spread_min_contrato", "spread_max_contrato"]))
    # compat: configs viejos traían strike_min/strike_max → renombrar a ask_min/ask_max para el editor.
    _df_s = _df_s.rename(columns={"strike_min": "ask_min", "strike_max": "ask_max"})
    for _c in ("ask_min", "ask_max", "spread_min_contrato", "spread_max_contrato"):
        if _c not in _df_s.columns:
            _df_s[_c] = None
    _df_s = _df_s[["ask_min", "ask_max", "spread_min_contrato", "spread_max_contrato"]]
    _ed_s = st.data_editor(
        _df_s, num_rows="dynamic", use_container_width=True, hide_index=True, key="cfg_ask_ed",
        column_config={
            "ask_min": st.column_config.NumberColumn("Precio del contrato (ASK) mínimo", min_value=0.0, step=1.0),
            "ask_max": st.column_config.NumberColumn("Precio del contrato (ASK) máximo", min_value=0.0, step=1.0),
            "spread_min_contrato": st.column_config.NumberColumn("Spread mín ($/contrato)", min_value=0.0, step=1.0),
            "spread_max_contrato": st.column_config.NumberColumn("Spread máx ($/contrato)", min_value=0.0, step=1.0),
        })
    st.caption("Valores **por contrato** (×100). El bucket se elige por el **precio del contrato = ASK × 100** "
               "(ej.: ASK $0.46/acción = $46/contrato → bucket $25–$300). El motor compara el spread **por "
               "acción = valor ÷ 100** (ej.: $1–$5 = $0.01–$0.05). Un contrato cuyo spread quede FUERA de "
               "[mín, máx] de su bucket de ASK se descarta.")
    if st.button("💾 Guardar filtro de spread", type="primary", key="save_ask"):
        rows = []
        for _, r in _ed_s.iterrows():
            try:
                rows.append({"ask_min": float(r["ask_min"]), "ask_max": float(r["ask_max"]),
                             "spread_min_contrato": float(r["spread_min_contrato"]),
                             "spread_max_contrato": float(r["spread_max_contrato"])})
            except (TypeError, ValueError, KeyError):
                continue   # fila incompleta → se ignora
        out = {"enabled": bool(_enabled),
               "_comment": "Filtro de spread por PRECIO DEL CONTRATO (ASK) — Opción 1. ask_min/ask_max y "
                           "spread_*_contrato en $ POR CONTRATO (x100); el motor compara ASK*100 y spread*100.",
               "buckets": rows}
        STRIKE_CFG_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        st.success(f"Guardado: {len(rows)} bucket(s), filtro {'ACTIVO' if _enabled else 'DESACTIVADO'}. "
                   "El motor lo toma en el próximo backtest.")


# ═══════════════════════ 2) Rango de precios por ticker ═══════════════════════
with st.expander("2 · Rango de precios de opción por ticker", expanded=False):
    _ti = _read_json(TICKER_INFO_PATH, {})
    _rows = [{"Ticker": k,
              "Óptimo mín": v.get("rango_optimo_lo"), "Óptimo máx": v.get("rango_optimo_hi"),
              "Ext mín": v.get("min"), "Ext máx": v.get("max"),
              "Nombre": v.get("nombre"), "Sector": v.get("bloque_sector")}
             for k, v in sorted(_ti.items())]
    _df_t = pd.DataFrame(_rows, columns=["Ticker", "Óptimo mín", "Óptimo máx", "Ext mín", "Ext máx", "Nombre", "Sector"])
    # Columna "Opcionable" (a la derecha): ¿el ticker tiene opciones? = tiene ≥1 vencimiento (≠ "—") en
    # la BD de preferencias (§3, semana representativa). Instantáneo, sin API. "—" = no está en esa BD.
    _tp.seed()
    _opc_db = _tp.load()
    _opc_map = {}
    if not _opc_db.empty:
        for _, _pr in _opc_db.iterrows():
            _opc = any(str(_pr.get(_c, "—")) != "—"
                       for _c in ("exp_lun", "exp_mar", "exp_mie", "exp_jue", "exp_vie"))
            _opc_map[str(_pr["ticker"]).strip().upper()] = "✅" if _opc else "❌"
    _df_t["Opcionable"] = _df_t["Ticker"].astype(str).str.strip().str.upper().map(_opc_map).fillna("—")
    # Buscador por ticker (typeahead): multiselect con búsqueda nativa al tipear; podés elegir
    # varios. Filtra la VISTA. El guardado MERGEA en el set completo (no borra los tickers ocultos).
    _all_tk = _df_t["Ticker"].astype(str).tolist()
    _sel = st.multiselect(
        "🔎 Buscar ticker (typeahead)", options=_all_tk, default=[], key="cfg_ti_search_ms",
        placeholder="Tipeá para buscar · podés elegir varios · vacío = todos")
    _df_view = (_df_t[_df_t["Ticker"].isin(_sel)].reset_index(drop=True) if _sel else _df_t)
    st.caption(f"Mostrando **{len(_df_view)}** de **{len(_df_t)}** tickers."
               + (" · 💾 Guardá antes de cambiar la selección." if _sel else ""))
    _ed_t = st.data_editor(
        _df_view, num_rows="dynamic", use_container_width=True, hide_index=True,
        height=(None if _sel else 460), key="cfg_ti_ed_" + "_".join(sorted(_sel)),
        column_config={
            "Ticker": st.column_config.TextColumn("Ticker", required=True, width="small"),
            "Óptimo mín": st.column_config.NumberColumn("Óptimo mín", min_value=0.0, step=5.0),
            "Óptimo máx": st.column_config.NumberColumn("Óptimo máx", min_value=0.0, step=5.0),
            "Ext mín": st.column_config.NumberColumn("Ext mín", min_value=0.0, step=5.0),
            "Ext máx": st.column_config.NumberColumn("Ext máx", min_value=0.0, step=5.0),
            "Nombre": st.column_config.TextColumn("Nombre", disabled=True),
            "Sector": st.column_config.TextColumn("Sector", disabled=True),
            "Opcionable": st.column_config.TextColumn(
                "Opcionable", disabled=True,
                help="¿El ticker tiene opciones? ✅ = tiene ≥1 vencimiento calculado en la base de §3 "
                     "(semana representativa). ❌ = sin vencimientos (puede no ser opcionable o faltar "
                     "«Recalcular» en §3). «—» = no está en esa base."),
        })
    st.caption("Rangos de **prima por contrato** (×100; el motor usa ÷100 = por acción). **Óptimo** = rango "
               "preferido (backtest manual); **Ext** = rango extendido (más ancho), que se usa si el óptimo "
               "queda vacío — y es el rango del backtest de **señales**.")
    if st.button("💾 Guardar rangos por ticker", type="primary", key="save_ti"):
        def _num(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        # MERGE: arrancamos del set COMPLETO (preserva los tickers que el filtro oculta) y
        # aplicamos las ediciones de la VISTA. Solo se borran los tickers que estaban en la vista
        # y el usuario quitó — nunca los ocultos por el filtro.
        new = {tk: dict(v) for tk, v in _ti.items()}
        _shown = set(_df_view["Ticker"].astype(str).str.strip().str.upper())
        _edited = set()
        for _, r in _ed_t.iterrows():
            tk = str(r.get("Ticker") or "").strip().upper()
            if not tk:
                continue
            _edited.add(tk)
            e = dict(_ti.get(tk, {}))   # preservar metadata (sectores, indice, fecha, etc.)
            e["ticker"] = tk
            e["rango_optimo_lo"] = _num(r.get("Óptimo mín"))
            e["rango_optimo_hi"] = _num(r.get("Óptimo máx"))
            e["min"] = _num(r.get("Ext mín"))
            e["max"] = _num(r.get("Ext máx"))
            if e.get("rango_optimo_lo") is not None and e.get("rango_optimo_hi") is not None:
                e["rango_optimo_text"] = f"${e['rango_optimo_lo']:g} - ${e['rango_optimo_hi']:g}"
            if e.get("min") is not None and e.get("max") is not None:
                e["min_max_text"] = f"MIN ${e['min']:g} MAX ${e['max']:g}"
            new[tk] = e
        for _tk in (_shown - _edited):   # filas que estaban en la vista y se borraron en el editor
            new.pop(_tk, None)
        TICKER_INFO_PATH.write_text(json.dumps(new, indent=2, ensure_ascii=False), encoding="utf-8")
        st.success(f"Guardado: {len(new)} ticker(s) en total"
                   + (f" · editaste {len(_edited)} de la selección ({', '.join(_sel)})." if _sel else ".")
                   + " El motor lo toma en el próximo backtest.")


# ═══════ 3) Tickers preferenciales + vencimiento más temprano por día ═══════
with st.expander("3 · Tickers preferenciales y vencimiento más temprano por día", expanded=False):
    st.caption("Marcá los tickers que **preferís operar** (se guarda en la **base de datos**). La tabla "
               "muestra el **vencimiento más temprano** si entrás cada día: «mismo día» = 0DTE; "
               "«Vie (+3)» = el más cercano es ese viernes, a 3 días; «—» = sin datos (tocá *Recalcular*).")

    import ticker_prefs as _tp  # noqa: E402

    _tp.seed()
    _pref = _tp.load()
    # Inicializa el último estado conocido desde la BD (evita un guardado/toast espurio al abrir).
    if "_cfg_pref_last" not in st.session_state:
        st.session_state["_cfg_pref_last"] = {str(t): bool(p)
                                              for t, p in zip(_pref["ticker"], _pref["preferencia"])}

    # 🎨 Vista coloreada (SOLO LECTURA): fila verde = preferencial · 🟢 = prioritario (los 11).
    st.markdown("🎨 **Vista coloreada** — fila **verde** = preferencial · 🟢 = prioritario (los 11). "
                "_(Para cambiar, usá la tabla editable de abajo.)_")
    _vista = pd.DataFrame({
        "🟢": ["🟢" if t else "" for t in _pref["es_top"]],
        "Ticker": _pref["ticker"].tolist(),
        "Pref": ["✓" if p else "" for p in _pref["preferencia"]],
        "Lun": _pref["exp_lun"].tolist(), "Mar": _pref["exp_mar"].tolist(),
        "Mié": _pref["exp_mie"].tolist(), "Jue": _pref["exp_jue"].tolist(),
        "Vie": _pref["exp_vie"].tolist(),
    }).reset_index(drop=True)
    _pmask = [bool(p) for p in _pref["preferencia"]]
    st.dataframe(
        _vista.style.apply(lambda r: ['background-color: #d7f5d7' if _pmask[r.name] else ''
                                      for _ in r], axis=1),
        hide_index=True, use_container_width=True, height=300)

    # 🟢 marca los 11 PRIORITARIOS (van primero). La tabla EDITABLE (st.data_editor) no puede
    # pintar filas de color, así que el grupo prioritario se señala con 🟢 en su propia columna.
    st.markdown("✏️ **Editá las preferencias acá** (checkbox · se guarda al instante):")
    _pdisp = pd.DataFrame({
        "🟢": ["🟢" if t else "" for t in _pref["es_top"]],
        "Ticker": _pref["ticker"],
        "Preferencia": _pref["preferencia"],
        "Lun": _pref["exp_lun"], "Mar": _pref["exp_mar"], "Mié": _pref["exp_mie"],
        "Jue": _pref["exp_jue"], "Vie": _pref["exp_vie"],
    })
    _ed_p = st.data_editor(
        _pdisp, hide_index=True, use_container_width=True, height=600, key="cfg_pref_ed",
        column_config={
            "🟢": st.column_config.TextColumn("🟢", width="small", disabled=True,
                                              help="🟢 = ticker prioritario (los 11 de arriba)."),
            "Ticker": st.column_config.TextColumn("Ticker", disabled=True, width="small"),
            "Preferencia": st.column_config.CheckboxColumn(
                "Preferencia", help="Marcá los que preferís operar. Se guarda en la base al instante."),
            "Lun": st.column_config.TextColumn("Lun", disabled=True),
            "Mar": st.column_config.TextColumn("Mar", disabled=True),
            "Mié": st.column_config.TextColumn("Mié", disabled=True),
            "Jue": st.column_config.TextColumn("Jue", disabled=True),
            "Vie": st.column_config.TextColumn("Vie", disabled=True),
        })
    # Persistir cambios de Preferencia en la base, apenas el usuario toca un checkbox.
    _newpref = {str(t): bool(p) for t, p in zip(_ed_p["Ticker"], _ed_p["Preferencia"])}
    if _newpref != st.session_state.get("_cfg_pref_last"):
        _tp.save_preferencias(_newpref)
        st.session_state["_cfg_pref_last"] = _newpref
        st.toast(f"💾 Preferencias guardadas ({sum(_newpref.values())} marcados)")
        st.rerun()   # refresca la vista coloreada de arriba con el cambio
    st.caption(f"⭐ **{sum(_newpref.values())}** preferenciales · **{len(_pref)}** tickers en total. "
               "Los **11 primeros** (🟢) son los prioritarios.")

    if st.button("🔄 Recalcular vencimientos (consulta la API — puede tardar)", key="cfg_pref_recompute"):
        try:
            import config as _cfg  # noqa: E402
            from adapter_polygon import PolygonAdapter as _PA  # noqa: E402
            from downloader import Downloader as _DL  # noqa: E402
            _dl = _DL(_PA(getattr(_cfg, "POLYGON_API_KEY", "")), HERE / "data")
            with st.spinner("Calculando el vencimiento más temprano de los 51 tickers…"):
                _r = _tp.compute_expiries(_dl)
            st.session_state.pop("cfg_pref_ed", None)  # re-siembra la tabla con los vencimientos nuevos
            st.success(f"Listo: {_r['ok']}/{_r['total']} tickers con datos. Recargando…")
            st.rerun()
        except Exception as _e:
            st.error(f"No se pudo recalcular: {_e}")


# ═══════ 4) Tickers que vencen ese mismo día de la semana ═══════
with st.expander("4 · Tickers que vencen ese mismo día de la semana", expanded=False):
    st.caption("Para cada día de la semana, los tickers con **vencimiento 0DTE** ese día — derivado de "
               "la base de §3 («mismo día» = vence ese día). Si está vacío o desactualizado, tocá "
               "**🔄 Recalcular vencimientos** en §3 (arriba).")
    _tp.seed()
    _pref4 = _tp.load()
    _dias4 = [("Lun", "exp_lun"), ("Mar", "exp_mar"), ("Mié", "exp_mie"),
              ("Jue", "exp_jue"), ("Vie", "exp_vie")]
    if _pref4.empty:
        st.info("No hay tickers en la base.")
    else:
        # Un ticker vence el día W ⟺ entrando ese día su vencimiento más cercano es el MISMO día (0DTE).
        _buck4 = {lbl: _pref4.loc[_pref4[col] == "mismo día", "ticker"].tolist() for lbl, col in _dias4}
        _maxn4 = max((len(v) for v in _buck4.values()), default=0)
        if _maxn4 == 0:
            st.warning("Ningún ticker tiene vencimientos calculados todavía. Tocá "
                       "**🔄 Recalcular vencimientos** en §3.")
        else:
            # 5 columnas (Lun–Vie); cada columna lista los tickers que vencen ese día (relleno parejo).
            _tbl4 = pd.DataFrame({lbl: _buck4[lbl] + [""] * (_maxn4 - len(_buck4[lbl]))
                                  for lbl, _ in _dias4})
            st.dataframe(_tbl4, hide_index=True, use_container_width=True,
                         height=min(38 + 35 * _maxn4, 560))
            st.caption("Cuántos vencen cada día: "
                       + " · ".join(f"**{lbl}** {len(_buck4[lbl])}" for lbl, _ in _dias4)
                       + f"  ·  ({len(_pref4)} tickers en la base)")

# ═══════ 5) Universo 0DTE — gran movimiento vs vencimiento ═══════
with st.expander("5 · Universo 0DTE — gran movimiento vs vencimiento", expanded=False):
    st.caption("Los activos operables clasificados por **gran movimiento** (rango medio intradía) y "
               "**disponibilidad de 0DTE**. El 0DTE **diario** existe solo en índices "
               "(SPX/SPY/QQQ/IWM/DIA); acciones y ETFs no-core = **solo viernes**. Objetivo: cazar "
               "grandes movimientos que valoricen el contrato del día — pero el edge está en la "
               "**dirección**, no en la volatilidad (que ya está en la prima).")
    try:
        from market_direction.ui.universe_view import build_verdict_df
        from market_direction.data.universe_scan import scan_universe, save_scan, load_scan
        from market_direction.domain import ticker_profile as _tpr

        _SCAN_PATH = HERE / "data" / "market_direction" / "universe_metrics.json"
        _scan = load_scan(_SCAN_PATH)
        if _scan and _scan.get("_as_of"):
            st.caption(f"📊 Medición fresca: **{_scan['_as_of']}** (~{_scan.get('_days', 60)} días).")
        else:
            st.caption("📊 Mostrando el **snapshot base** (jun-2026). Tocá **Actualizar** para recalcular "
                       "con datos frescos del cache.")

        st.dataframe(build_verdict_df(_scan), hide_index=True, use_container_width=True, height=560)
        st.caption("**Movimiento:** ✓✓✓ ≥4% · ✓✓ 2.8–4% · ✓ 1.8–2.8% · ~ 1.2–1.8% · ✗ <1.2% "
                   "(SPY ≈ 1%/día). ⚠️ **Earnings:** evitá 0DTE de acciones en su semana de reporte "
                   "(IV inflada → puede perder aunque acierte el movimiento).")

        if st.button("🔄 Actualizar volatilidad (~30 s)", key="scan_universe_btn"):
            from datetime import datetime as _dt
            with st.spinner("Midiendo volatilidad de los activos con datos frescos del cache…"):
                _m = scan_universe(HERE / "data" / "underlying", list(_tpr.PROFILES.keys()),
                                   days=60, as_of=_dt.now().strftime("%Y-%m-%d %H:%M"))
                save_scan(_m, _SCAN_PATH)
            st.success(f"✅ Actualizado — {len(_m.get('metrics', {}))} activos medidos.")
            st.rerun()
    except Exception as _e:
        st.error(f"No se pudo cargar el universo: {_e}")

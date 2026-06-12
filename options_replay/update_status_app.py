"""Página 'Datos' (Herramientas) — estado del actualizador de tickers.

Muestra: estado de la Tarea Programada de Windows (última/próxima corrida), el
REPORTE del último run (qué tickers actualizó, días, llamadas, errores) escrito por
update_all.py en data/update_status.json, y el log (_update_all.log). Botón para
correr la actualización ahora (dispara la tarea).
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
ROOT = HERE.parent
STATUS_PATH = HERE / "data" / "update_status.json"
LOG_PATH = ROOT / "_update_all.log"
TASK_NAME = "SignalForge Update Data"

try:
    st.set_page_config(page_title="Datos", layout="wide")
except Exception:
    pass

st.title("🔄 Actualización de datos")
st.caption("Proceso que refresca la cache de Polygon (todos los tickers) hasta el día actual.")


def _ps(cmd: str):
    """Corre un comando PowerShell y devuelve (rc, stdout, stderr). Solo Windows."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, timeout=20)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _dotnet_date(v) -> str:
    """ConvertTo-Json serializa DateTime como '/Date(ms)/'. Lo pasamos a 'YYYY-MM-DD HH:MM'."""
    m = re.match(r"/Date\((\d+)(?:[-+]\d+)?\)/", str(v or ""))
    if not m:
        return str(v or "—")
    try:
        return datetime.fromtimestamp(int(m.group(1)) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(v)


def _fmt_iso(s) -> str:
    """ISO '2026-06-11T05:00:03' → '2026-06-11 05:00' (mismo formato que la última corrida)."""
    s = str(s or "")
    return s.replace("T", " ")[:16] if s and s != "—" else "—"


# ── Tarea programada ─────────────────────────────────────────────────────────
st.subheader("🗓️ Tarea programada")
_rc, _out, _err = _ps(f"Get-ScheduledTaskInfo -TaskName '{TASK_NAME}' | "
                      "Select-Object LastRunTime, NextRunTime, LastTaskResult, NumberOfMissedRuns | "
                      "ConvertTo-Json -Compress")
_info = None
if _rc == 0 and _out:
    try:
        _info = json.loads(_out)
    except Exception:
        _info = None
if _info:
    c = st.columns(4)
    c[0].metric("Última corrida", _dotnet_date(_info.get("LastRunTime")))
    c[1].metric("Próxima corrida", _dotnet_date(_info.get("NextRunTime")))
    _res = _info.get("LastTaskResult")
    c[2].metric("Último resultado", "OK ✅" if _res == 0 else (f"código {_res}" if _res is not None else "—"))
    c[3].metric("Corridas perdidas", str(_info.get("NumberOfMissedRuns") if _info.get("NumberOfMissedRuns") is not None else "—"))
else:
    st.info("No encontré la tarea **SignalForge Update Data** (o no es Windows). "
            "Se crea con la Tarea Programada que actualiza los datos cada madrugada.")

_b1, _b2, _ = st.columns([1.6, 1.6, 4])
if _b1.button("▶ Correr ahora", type="primary", use_container_width=True,
              help="Dispara la tarea ya. Corre en segundo plano y consume API de Polygon. "
                   "Refrescá esta página al rato para ver el reporte actualizado."):
    _r, _o, _e = _ps(f"Start-ScheduledTask -TaskName '{TASK_NAME}'")
    st.success("Lanzada. Corre en segundo plano (puede tardar). Refrescá en unos minutos.") \
        if _r == 0 else st.error(f"No pude lanzarla: {_e or _o}")
if _b2.button("🔄 Refrescar", use_container_width=True):
    st.rerun()

st.divider()

# ── Reporte del último run ───────────────────────────────────────────────────
st.subheader("📊 Último run (qué actualizó)")
if STATUS_PATH.exists():
    try:
        _st = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        _st = None
else:
    _st = None

if not _st:
    st.info("Todavía no hay reporte. Corré la actualización (botón de arriba) o esperá a que "
            "la tarea corra de madrugada.")
else:
    m = st.columns(4)
    m[0].metric("Inicio", _fmt_iso(_st.get("started")))
    m[1].metric("Duración", f"{_st.get('duration_min', 0)} min")
    m[2].metric("Tickers", _st.get("n_tickers", 0))
    m[3].metric("Errores", _st.get("n_errors", 0))
    st.caption(f"Ventana: **{_st.get('window_start')} → {_st.get('window_end')}**  ·  "
               f"Modo: **{_st.get('mode')}**  ·  Llamadas a opciones: **{_st.get('total_opt_calls', 0)}**  ·  "
               f"Fin: {_fmt_iso(_st.get('finished'))}")
    _rows = _st.get("tickers") or []
    if _rows:
        _df = pd.DataFrame(_rows).rename(columns={"ticker": "Ticker", "days": "Días",
                                                  "opt": "Opt. calls", "error": "Error"})
        _df["Estado"] = _df["Error"].apply(lambda e: "❌ error" if e else "✅ ok")
        st.dataframe(_df[["Ticker", "Estado", "Días", "Opt. calls", "Error"]],
                     use_container_width=True, hide_index=True,
                     column_config={"Error": st.column_config.TextColumn("Error", width="large")})

st.divider()

# ── Cobertura de datos locales por ticker ────────────────────────────────────
st.subheader("📦 Datos locales por ticker")
st.caption("Por ticker: rango de fechas con **subyacente** cacheado (Desde/Hasta/Días) y "
           "días con **opciones** cacheadas. **Atraso** = días hábiles detrás del día más "
           "reciente global (0 = al día). Clic en un encabezado para ordenar.")


@st.cache_data(ttl=60, show_spinner="Escaneando cache local…")
def _local_coverage() -> pd.DataFrame:
    """Escanea data/underlying/ y data/options/ (solo NOMBRES, no abre los parquet) y
    arma, por ticker: rango de fechas del subyacente (mín/máx + cantidad de días) y la
    cantidad de días DISTINTOS con barras de opciones cacheadas. Rápido aun con ~100k
    archivos. Nombres:  underlying TICKER_YYYY-MM-DD.parquet ;
    opciones  O_TICKER{YYMMDD}{C/P}{strike}_YYYY-MM-DD.parquet."""
    data = HERE / "data"
    u_pat = re.compile(r"^(.+)_(\d{4}-\d{2}-\d{2})\.parquet$")
    o_pat = re.compile(r"^O_([A-Za-z]+)\d{6}[CP]\d+_(\d{4}-\d{2}-\d{2})\.parquet$")
    under: dict[str, list[str]] = {}
    udir = data / "underlying"
    if udir.exists():
        for p in udir.glob("*.parquet"):
            m = u_pat.match(p.name)
            if m:
                under.setdefault(m.group(1), []).append(m.group(2))
    opt_days: dict[str, set] = {}
    odir = data / "options"
    if odir.exists():
        for p in odir.glob("*.parquet"):
            m = o_pat.match(p.name)
            if m:
                opt_days.setdefault(m.group(1).upper(), set()).add(m.group(2))
    rows = []
    for tk, ds in under.items():
        ds.sort()
        rows.append({"Ticker": tk, "Desde": ds[0], "Hasta": ds[-1], "Días": len(ds),
                     "Opc. días": len(opt_days.get(tk.upper(), ()))})
    return pd.DataFrame(rows)


_cov = _local_coverage()
if _cov.empty:
    st.info("Todavía no hay datos de subyacente cacheados en `data/underlying/`.")
else:
    _cov = _cov.copy()
    _gmax = _cov["Hasta"].max()

    def _lag_bdays(h: str) -> int:
        """Días hábiles entre el último día cacheado del ticker y el más reciente global."""
        if h >= _gmax:
            return 0
        return max(0, len(pd.bdate_range(h, _gmax)) - 1)

    _cov["Atraso"] = _cov["Hasta"].apply(_lag_bdays)
    _cov["Estado"] = _cov["Atraso"].apply(lambda n: "✅ al día" if n == 0 else "⚠️ atrasado")

    _nstale = int((_cov["Atraso"] > 0).sum())
    _mc = st.columns(4)
    _mc[0].metric("Tickers", len(_cov))
    _mc[1].metric("Día más reciente", _gmax)
    _mc[2].metric("⚠️ Atrasados", _nstale)
    _mc[3].metric("Con opciones", int((_cov["Opc. días"] > 0).sum()))
    st.caption(f"Rango global: **{_cov['Desde'].min()} → {_gmax}**  ·  "
               f"archivos subyacente: **{int(_cov['Días'].sum())}**.")

    _fc1, _fc2 = st.columns([3, 2])
    _q = _fc1.text_input("Filtrar por ticker", "", placeholder="Ej.: QQQ").strip().upper()
    _only_stale = _fc2.checkbox("⚠️ Solo atrasados", value=False,
                                help="Tickers cuyo último día cacheado es anterior al "
                                     "día más reciente global.")
    _show = _cov
    if _q:
        _show = _show[_show["Ticker"].str.contains(_q, regex=False)]
    if _only_stale:
        _show = _show[_show["Atraso"] > 0]
    _show = _show.sort_values(["Días", "Ticker"], ascending=[False, True]).reset_index(drop=True)
    st.dataframe(
        _show, use_container_width=True, hide_index=True,
        column_config={
            "Ticker": st.column_config.TextColumn("Ticker"),
            "Desde": st.column_config.TextColumn("Desde"),
            "Hasta": st.column_config.TextColumn("Hasta"),
            "Días": st.column_config.NumberColumn("Días", format="%d",
                                                  help="Días con subyacente cacheado"),
            "Opc. días": st.column_config.NumberColumn("Opc. días", format="%d",
                                                       help="Días distintos con opciones cacheadas"),
            "Atraso": st.column_config.NumberColumn("Atraso", format="%d",
                                                    help="Días hábiles detrás del más reciente (0 = al día)"),
            "Estado": st.column_config.TextColumn("Estado"),
        },
    )
    if _show.empty:
        st.caption("Ningún ticker cumple el filtro.")

st.divider()

# ── Log ──────────────────────────────────────────────────────────────────────
st.subheader("📜 Log")
if LOG_PATH.exists():
    try:
        _raw = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        _raw = f"(no pude leer el log: {e})"
    _tail = "\n".join(_raw.splitlines()[-80:])
    st.code(_tail or "(log vacío)", language="text")
    st.caption(f"Mostrando las últimas 80 líneas de `{LOG_PATH.name}`.")
else:
    st.info("Todavía no hay log (`_update_all.log`). Se crea cuando corre la actualización.")

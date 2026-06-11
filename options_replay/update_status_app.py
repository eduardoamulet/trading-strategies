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
    m[0].metric("Inicio", _st.get("started", "—"))
    m[1].metric("Duración", f"{_st.get('duration_min', 0)} min")
    m[2].metric("Tickers", _st.get("n_tickers", 0))
    m[3].metric("Errores", _st.get("n_errors", 0))
    st.caption(f"Ventana: **{_st.get('window_start')} → {_st.get('window_end')}**  ·  "
               f"Modo: **{_st.get('mode')}**  ·  Llamadas a opciones: **{_st.get('total_opt_calls', 0)}**  ·  "
               f"Fin: {_st.get('finished', '—')}")
    _rows = _st.get("tickers") or []
    if _rows:
        _df = pd.DataFrame(_rows).rename(columns={"ticker": "Ticker", "days": "Días",
                                                  "opt": "Opt. calls", "error": "Error"})
        _df["Estado"] = _df["Error"].apply(lambda e: "❌ error" if e else "✅ ok")
        st.dataframe(_df[["Ticker", "Estado", "Días", "Opt. calls", "Error"]],
                     use_container_width=True, hide_index=True,
                     column_config={"Error": st.column_config.TextColumn("Error", width="large")})

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

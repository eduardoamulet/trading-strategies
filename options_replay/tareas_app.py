"""Página 'Tareas' (Herramientas) — panel para encender/apagar procesos en segundo
plano A DEMANDA: el daemon del Live Trader y el backfill / actualización de datos.

Cada tarea muestra nombre, descripción, estado (encendida/apagada) y botones
Encender / Apagar (+ Reiniciar para el daemon). Los procesos se lanzan DETACHED, así
sobreviven a los reruns de Streamlit y al cierre de la UI. Marco genérico (lista TASKS)
para sumar más tareas fácilmente.

Seguridad: el daemon corre SIEMPRE en SANDBOX (settings.LIVE_TRADING_ENABLED = False);
nunca opera en real. El backfill solo descarga data de Polygon (lectura).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent           # options_replay
ROOT = HERE.parent                               # raíz del repo
LIVE_DIR = ROOT / "live_trader"
DB_PATH = LIVE_DIR / "data" / "live_trader.db"   # store del daemon (latido)
TASKS_DIR = HERE / "data" / "tasks"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
PY = sys.executable

try:
    st.set_page_config(page_title="Tareas", layout="wide")
except Exception:
    pass

st.title("🧰 Tareas")
st.caption("Encendé o apagá procesos en segundo plano a demanda. Corren independientes de "
           "esta UI (sobreviven a recargas y al cierre). El daemon corre en **SANDBOX** (paper).")


# ───────────────────────────── procesos genéricos (pidfile) ──────────────────────────
def _pid_alive(pid) -> bool:
    """¿El proceso con ese PID está vivo? (cross-platform)."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True)
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _kill(pid):
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(int(pid), 15)
        except Exception:
            pass


def _launch(tid: str, cmd: list, cwd: Path) -> int:
    """Lanza `cmd` DETACHED; guarda el PID en data/tasks/{tid}.pid y el log en {tid}.log."""
    flags = ((subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
             if os.name == "nt" else 0)
    _log = open(TASKS_DIR / f"{tid}.log", "a", encoding="utf-8")
    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=_log, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)
    (TASKS_DIR / f"{tid}.pid").write_text(str(p.pid))
    return p.pid


def _pidfile_pid(tid: str):
    try:
        return int((TASKS_DIR / f"{tid}.pid").read_text().strip())
    except Exception:
        return None


# ────────────────────────────── daemon (vía latido del store) ────────────────────────
def _launch_daemon():
    """Arranca el daemon como proceso independiente (igual que la página Live)."""
    _launch("daemon", [PY, "-m", "daemon.runner"], LIVE_DIR)


def _set_daemon_stopped():
    """Marca el latido como 'apagado' en el store → la página Live también lo refleja."""
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5)
        con.execute("INSERT OR REPLACE INTO meta (key, value, ts) VALUES (?,?,?)",
                    ("daemon_heartbeat", json.dumps({"stopped": True}),
                     datetime.utcnow().isoformat()))
        con.commit()
        con.close()
    except Exception:
        pass


def _daemon_state():
    """(encendido, pid, edad_del_latido_seg). Lee el latido del store del daemon (sqlite)."""
    if not DB_PATH.exists():
        return (False, None, None)
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5)
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT value, ts FROM meta WHERE key='daemon_heartbeat'").fetchone()
        con.close()
    except Exception:
        return (False, None, None)
    if not r:
        return (False, None, None)
    try:
        val = json.loads(r["value"] or "{}")
    except Exception:
        val = {}
    if val.get("stopped"):
        return (False, None, None)
    pid = val.get("pid")
    age = None
    try:
        age = (datetime.utcnow() - datetime.fromisoformat(r["ts"])).total_seconds()
    except Exception:
        pass
    on = (age is not None and age < 15) and _pid_alive(pid)
    return (on, pid, age)


# ─────────────────────────────────── registro de tareas ──────────────────────────────
TASKS = [
    {
        "id": "daemon", "icon": "🟢", "kind": "daemon",
        "name": "Daemon — Live Trader",
        "desc": "Monitorea las posiciones abiertas y las vende automáticamente al Umbral de "
                "ROI / cierre de sesión (paper · SANDBOX). Es el mismo daemon que controlás "
                "desde la página Live.",
    },
    {
        "id": "backfill", "icon": "🔄", "kind": "script",
        "name": "Backfill / actualización de datos (Polygon)",
        "desc": "Descarga y rellena el cache de Polygon (subyacente + opciones) de todos los "
                "tickers hasta hoy. Tarea de un disparo: seguí el avance en la página Datos y "
                "se apaga sola al terminar.",
        "cmd": [PY, "update_all.py"], "cwd": HERE,
    },
]


@st.fragment(run_every=3.0)
def _render_tasks():
    for t in TASKS:
        if t["kind"] == "daemon":
            on, pid, age = _daemon_state()
        else:
            pid = _pidfile_pid(t["id"])
            on, age = _pid_alive(pid), None

        with st.container(border=True):
            _c1, _c2 = st.columns([4, 1.5], vertical_alignment="center")
            with _c1:
                st.markdown(f"#### {t['icon']} {t['name']}")
                st.caption(t["desc"])
                if on:
                    _extra = f"  ·  PID {pid}" + (f"  ·  latido {age:.0f}s" if age is not None else "")
                    st.markdown(f":green[**● Encendida**]{_extra}")
                else:
                    st.markdown(":gray[**○ Apagada**]")
            with _c2:
                if on:
                    if st.button("⏹ Apagar", key=f"stop_{t['id']}", use_container_width=True):
                        _kill(pid)
                        if t["kind"] == "daemon":
                            _set_daemon_stopped()
                        else:
                            try:
                                (TASKS_DIR / f"{t['id']}.pid").unlink()
                            except Exception:
                                pass
                        st.toast(f"Apagando «{t['name']}»…")
                        st.rerun()
                    if t["kind"] == "daemon":
                        if st.button("↻ Reiniciar", key=f"restart_{t['id']}",
                                     use_container_width=True):
                            _kill(pid)
                            _launch_daemon()
                            st.toast("Reiniciando el daemon…")
                            st.rerun()
                else:
                    if st.button("▶ Encender", key=f"start_{t['id']}", type="primary",
                                 use_container_width=True):
                        if t["kind"] == "daemon":
                            _launch_daemon()
                        else:
                            _launch(t["id"], t["cmd"], t["cwd"])
                        st.toast(f"Encendiendo «{t['name']}»…")
                        st.rerun()


_render_tasks()

st.divider()
st.caption("⚠️ El daemon NUNCA opera en real (`LIVE_TRADING_ENABLED = False` · sandbox). "
           "Los logs de cada tarea quedan en `options_replay/data/tasks/{id}.log`.")

"""obs_log — logging estructurado y observabilidad, reutilizable (batch, app, Downloader).

Da: niveles (DEBUG/INFO/WARNING/ERROR), formato con timestamp + nivel + pid/tid + Correlation ID +
logger, handlers a consola y a archivo ROTATIVO (persiste tras un crash → post-mortem), un context
manager `timed` (inicio/fin + duración + stacktrace si falla), `log_exception` (traza completa +
contexto) y `resource_snapshot` (RAM del proceso y del sistema + nº de procesos python → caza OOM).

NO registra datos sensibles: nunca loguear API keys / tokens / passwords. Pasá config ya redactada
(ver `redact`). El Correlation ID sigue una misma corrida (p.ej. un batch) por todos los componentes.
"""
from __future__ import annotations

import contextvars
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent / "logs"
_CORR = contextvars.ContextVar("correlation_id", default="-")
_FMT = "%(asctime)s | %(levelname)-5s | pid=%(pid)-6d tid=%(tid)04x | %(corr_id)-14s | %(name)-16s | %(message)s"
_SENSITIVE = ("key", "token", "password", "passwd", "secret", "apikey", "api_key", "authorization", "bearer")


class _CtxFilter(logging.Filter):
    """Inyecta correlation-id + pid + tid en cada record (para el formato y el rastreo concurrente)."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.corr_id = _CORR.get()
        record.pid = os.getpid()
        record.tid = threading.get_ident() & 0xFFFF
        return True


def set_correlation_id(cid: str) -> None:
    _CORR.set(str(cid))


def correlation_id() -> str:
    return _CORR.get()


def redact(d: dict) -> dict:
    """Copia del dict con los valores sensibles enmascarados (nunca loguear keys/tokens en claro)."""
    out = {}
    for k, v in (d or {}).items():
        out[k] = "***" if any(s in str(k).lower() for s in _SENSITIVE) else v
    return out


def get_logger(name: str, *, to_file: str | None = None, level: int = logging.INFO) -> logging.Logger:
    """Logger configurado (idempotente). `to_file` = nombre de archivo en options_replay/logs/
    (rotativo, 20 MB × 3). El handler de archivo captura DEBUG; la consola, `level`."""
    lg = logging.getLogger(name)
    if getattr(lg, "_obs_configured", False):
        return lg
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    fmt = logging.Formatter(_FMT)
    ctx = _CtxFilter()
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    ch.addFilter(ctx)
    lg.addHandler(ch)
    if to_file:
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                _LOG_DIR / to_file, maxBytes=20_000_000, backupCount=3, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            fh.addFilter(ctx)
            lg.addHandler(fh)
        except Exception:   # nunca romper la app por no poder abrir el log
            pass
    lg._obs_configured = True   # type: ignore[attr-defined]
    return lg


class timed:
    """Context manager: loguea ▶START e ■END con DURACIÓN de una operación crítica; si lanza, loguea
    ✖FAIL con el stacktrace completo (y re-lanza). `slow_s` avisa si tardó más de lo esperado."""
    def __init__(self, logger: logging.Logger, op: str, *, level: int = logging.INFO,
                 slow_s: float | None = None, **ctx):
        self._lg, self._op, self._level, self._slow, self._ctx = logger, op, level, slow_s, ctx

    def __enter__(self):
        self._t0 = time.perf_counter()
        self._lg.log(self._level, ">> START %s %s", self._op, self._ctx or "")
        return self

    def __exit__(self, et, ev, tb):
        dt = time.perf_counter() - self._t0
        if et is not None:
            self._lg.error("XX FAIL  %s (%.2fs) %s\n%s", self._op, dt, self._ctx or "",
                           "".join(traceback.format_exception(et, ev, tb)))
        else:
            lvl = logging.WARNING if (self._slow and dt > self._slow) else self._level
            tag = " !!SLOW" if (self._slow and dt > self._slow) else ""
            self._lg.log(lvl, "<< END%s  %s (%.2fs) %s", tag, self._op, dt, self._ctx or "")
        return False   # no suprimir la excepción


def log_exception(logger: logging.Logger, msg: str, **ctx) -> None:
    """Loguea un ERROR con el stacktrace COMPLETO del except actual + contexto (redactado)."""
    logger.error("%s %s\n%s", msg, redact(ctx) or "", traceback.format_exc())


def resource_snapshot(logger: logging.Logger, tag: str = "") -> dict:
    """Loguea (INFO) y devuelve RAM del proceso + RAM del sistema + nº de procesos python.
    Sirve para ver la tendencia de memoria y cazar OOM (la causa sospechada del crash del batch)."""
    info: dict = {"tag": tag}
    try:
        import psutil
        p = psutil.Process()
        vm = psutil.virtual_memory()
        n_py = 0
        for pr in psutil.process_iter(["name"]):
            try:
                if (pr.info.get("name") or "").lower().startswith("python"):
                    n_py += 1
            except Exception:
                pass
        info.update(proc_rss_mb=round(p.memory_info().rss / 1e6),
                    sys_mem_pct=vm.percent, sys_used_gb=round((vm.total - vm.available) / 1e9, 1),
                    sys_total_gb=round(vm.total / 1e9, 1), python_procs=n_py)
        logger.info("RES %-10s | proc_rss=%dMB | sys_mem=%.0f%% (%.1f/%.1fGB) | python_procs=%d",
                    tag, info["proc_rss_mb"], info["sys_mem_pct"], info["sys_used_gb"],
                    info["sys_total_gb"], info["python_procs"])
    except Exception:
        logger.info("RES %-10s | (psutil no disponible — instalá psutil para snapshots de memoria)", tag)
    return info

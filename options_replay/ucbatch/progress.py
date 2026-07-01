"""Progress Reporter — progreso por consola + notificación de Windows al terminar.

Aislado de la UI: lo usa el entrypoint de terminal (run_ucbatch.py). No bloquea nada.
"""
from __future__ import annotations

import time
from pathlib import Path


def cli_progress(done: int, total: int, t0: float) -> None:
    el = time.time() - t0
    eta = (el / done * (total - done)) if done else 0.0
    print(f"\r  {el:6.0f}s · {done:,}/{total:,} ({100 * done / max(total, 1):4.0f}%) · "
          f"ETA {eta / 60:4.1f} min   ", end="", flush=True)


def notify_done(path: Path, n_ok: int, n_err: int, elapsed: float, keep_open: bool = True) -> None:
    """Popup de Windows al frente + (opcional) dejar la consola abierta."""
    msg = (f"Backtesting terminado en {elapsed / 60:.1f} min.\n\n"
           f"{n_ok:,} posiciones OK · {n_err:,} con error\n\nResultados:\n{path}")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "SignalForge — Backtesting terminado", 0x40 | 0x10000)
    except Exception:
        pass
    if keep_open:
        try:
            input("\n(Enter para cerrar esta ventana)")
        except Exception:
            pass

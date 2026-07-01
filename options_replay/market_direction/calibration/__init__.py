"""calibration — la costura de CONFIANZA, separada del score direccional.

`Calibrator` (interface) mapea score → confianza 0–1. Dos implementaciones intercambiables:
  · `HeuristicCalibrator` — promedio ponderado, PROVISIONAL (no calibrado). Bootstrap.
  · `EmpiricalCalibrator` — P(acierto) histórica del backtest, CALIBRADO. El entregable real.

`make_calibrator()` elige el empírico si ya existe la tabla del backtest; si no, el heurístico.
El engine depende solo de la interface → migrar de heurístico a empírico (y luego a ML) es un swap.
"""
from __future__ import annotations

from pathlib import Path

from .calibrator import Calibrator
from .heuristic import HeuristicCalibrator
from .empirical import EmpiricalCalibrator

_DEFAULT_TABLE = Path(__file__).resolve().parent / "calibration_table.json"


def make_calibrator(table_path=None) -> Calibrator:
    """Empírico si existe la tabla de calibración del backtest; si no, heurístico (provisional)."""
    p = Path(table_path) if table_path else _DEFAULT_TABLE
    if p.exists():
        return EmpiricalCalibrator.from_json(p)
    return HeuristicCalibrator()


__all__ = ["Calibrator", "HeuristicCalibrator", "EmpiricalCalibrator", "make_calibrator"]

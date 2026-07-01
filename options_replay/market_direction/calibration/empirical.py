"""`EmpiricalCalibrator` — confianza CALIBRADA: P(acierto histórico) del setup, medida por backtest.

Esta es la confianza "de verdad". La tabla mapea un bucket-de-score → tasa de acierto empírica
(0–1), medida backtesteando el motor SIN look-ahead (Módulo 7+). Si devuelve 0.80, es porque en ese
rango de score la dirección acertó ~80% de las veces. Cuando la tabla exista, `make_calibrator()`
cambia solo del heurístico a éste — sin tocar el resto del sistema.

Formato de la tabla (JSON): {"bucket_size": 5, "fallback": 0.5, "table": {"75": 0.61, "80": 0.67, ...}}
donde cada clave es el piso del bucket de score.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..domain import Confirmation, DirectionScore, FeatureSet


class EmpiricalCalibrator:
    """Lookup en una tabla de hit-rate del backtest (vecino más cercano si falta el bucket)."""

    def __init__(self, table: dict, bucket_size: float = 5.0, fallback: float = 0.5):
        self._table = {int(k): float(v) for k, v in table.items()}
        self.bucket_size = float(bucket_size)
        self.fallback = float(fallback)

    def _bucket(self, score: float) -> int:
        step = int(self.bucket_size)
        return int(score // self.bucket_size) * step

    def confidence(self, score: DirectionScore, features: Optional[FeatureSet] = None,
                   confirmation: Optional[Confirmation] = None) -> float:
        if not self._table:
            return self.fallback
        b = self._bucket(score.score)
        if b in self._table:
            return self._table[b]
        nearest = min(self._table, key=lambda k: abs(k - b))   # bucket más cercano disponible
        return self._table[nearest]

    @classmethod
    def from_json(cls, path) -> "EmpiricalCalibrator":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if "table" in data:
            return cls(data["table"], data.get("bucket_size", 5.0), data.get("fallback", 0.5))
        return cls(data)

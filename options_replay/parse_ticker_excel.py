"""Parser del TSV ticker_info_source.tsv (snapshot pegado del usuario) →
ticker_info.json. Reemplaza completamente el JSON existente con esta data.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
TSV_PATH = HERE / "ticker_info_source.tsv"
OUT_PATH = HERE / "ticker_info.json"


def parse_range(s: str) -> tuple[float | None, float | None]:
    if not isinstance(s, str):
        return (None, None)
    nums = re.findall(r"\$?\s*(\d+(?:\.\d+)?)", s)
    if len(nums) >= 2:
        return (float(nums[0]), float(nums[1]))
    return (None, None)


def parse_min_max(s: str) -> tuple[float | None, float | None]:
    if not isinstance(s, str):
        return (None, None)
    mn = re.search(r"MIN\s*\$?\s*(\d+(?:\.\d+)?)", s, re.IGNORECASE)
    mx = re.search(r"MAX\s*\$?\s*(\d+(?:\.\d+)?)", s, re.IGNORECASE)
    return (
        float(mn.group(1)) if mn else None,
        float(mx.group(1)) if mx else None,
    )


def _strip(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def main() -> int:
    if not TSV_PATH.exists():
        print(f"Missing: {TSV_PATH}")
        return 1
    out: dict[str, dict] = {}
    with TSV_PATH.open(encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            ticker = _strip(row.get("TICKER"))
            if not ticker:
                continue
            ticker = ticker.upper()
            ro_lo, ro_hi = parse_range(row.get("RANGO ÓPTIMO", ""))
            mm_lo, mm_hi = parse_min_max(row.get("MÍNIMO Y MÁXIMO", ""))
            out[ticker] = {
                "ticker": ticker,
                "nombre": _strip(row.get("Nombre")),
                "indice": _strip(row.get("Indice")),
                "bloque_sector": _strip(row.get("Bloque Sector")),
                "sectores": _strip(row.get("Sectores")),
                "rango_optimo_text": _strip(row.get("RANGO ÓPTIMO")),
                "rango_optimo_lo": ro_lo,
                "rango_optimo_hi": ro_hi,
                "min_max_text": _strip(row.get("MÍNIMO Y MÁXIMO")),
                "min": mm_lo,
                "max": mm_hi,
                "fecha_analisis": _strip(row.get("FECHA DE ANÁLISIS")),
            }
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    print(f"Wrote {OUT_PATH}: {len(out)} tickers")
    for t in sorted(out):
        info = out[t]
        rng = (
            f"${info['rango_optimo_lo']:.0f}-${info['rango_optimo_hi']:.0f}"
            if info['rango_optimo_lo'] is not None else "-"
        )
        print(f"  {t:6s}  rango={rng:14s}  {info['nombre'] or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

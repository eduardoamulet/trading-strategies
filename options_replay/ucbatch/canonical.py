"""Filas del runner → DataFrame CANÓNICO (formato del loader) SIN pasar por Excel.

Única fuente de verdad del camino directo al almacén: reusa las definiciones del report
(_DATA/_ENRICH, con SUS redondeos) y del loader (_RESULT_COLS/_ENRICH_HEADER_MAP), de modo que
el DataFrame directo es EQUIVALENTE al round-trip report.write() → loader.load_results()
(garantizado por tests/test_canonical.py). Lo usan el modo --ingest de run_ucbatch y
verify_integrity — el Excel deja de ser transporte/almacenamiento (y con él, el techo de
1.048.576 filas por hoja).
"""
from __future__ import annotations

import pandas as pd

from bt_analysis import loader as _ldr

from . import report as _rep


def rows_to_canonical_df(seed, rows: list) -> pd.DataFrame:
    """Convierte las filas escalares del runner al formato canónico del loader (granularidad
    DETALLADA): mismas columnas, mismos redondeos y mismo ORDEN de filas que el results xlsx
    (Ticker según el seed → Fecha → ID), para que la ingesta directa sea indistinguible de la
    ingesta vía archivo."""
    canon_data = list(zip(_ldr._RESULT_COLS[3:14], [k for k, _ in _rep._DATA]))
    decs = dict(_rep._DATA)
    enr = any(k in r for r in rows[:1] for _, k in _rep._ENRICH) if rows else False
    extra = ([_ldr._ENRICH_HEADER_MAP[lbl] for lbl, _ in _rep._ENRICH
              if lbl in _ldr._ENRICH_HEADER_MAP] if enr else [])

    _tk = {t: i for i, t in enumerate(seed.tickers)}   # orden del seed, no alfabético
    ordered = sorted(rows, key=lambda r: (_tk.get(str(r.get("Ticker")), 999),
                                          str(r.get("Fecha")), str(r.get("ID"))))
    out = []
    for r in ordered:
        d = {"id": r.get("ID"), "ticker": r.get("Ticker"), "fecha": r.get("Fecha")}
        for canon, key in canon_data:                   # mismos redondeos que report.build
            v = r.get(key)
            d[canon] = round(v, decs[key]) if isinstance(v, (int, float)) else v
        if enr:
            for lbl, key in _rep._ENRICH:
                canon = _ldr._ENRICH_HEADER_MAP.get(lbl)
                if canon:
                    v = r.get(key)
                    d[canon] = round(v, 4) if isinstance(v, float) else v
        out.append(d)

    df = pd.DataFrame(out, columns=list(_ldr._RESULT_COLS) + extra)
    for c in list(_ldr._RESULT_COLS[3:]) + [c for c in _ldr._ENRICH_NUM if c in df.columns]:
        df[c] = pd.to_numeric(df[c], errors="coerce")   # misma coerción que load_results
    return df

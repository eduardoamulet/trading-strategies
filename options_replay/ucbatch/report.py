"""Excel Report Generator — workbook de salida con el formato del template.

Independiente del engine: recibe `seed` + `rows` (dicts escalares del runner) y arma el .xlsx.
Layout (hoja «Backtesting Results»):
  r1: 📝 Meta | título «RESULTADOS DE SALIDA - Operación <TIPO>» (merge sobre las columnas de datos)
  r2: headers agrupados (Ticker, Fecha, Inversión total, Ganancia, Valor mín/máx/prom ROI, #ROI, #err)
  r3: sub-headers (ID | ROI(%)/ROI($) … | >0 / <=0)
  r4+: una fila por (ticker × día × escenario), ordenadas por Ticker → Fecha → ID.
Naming: Backtesting_Results_<TIPO>_of_<T1_T2_…>_from_<inicio>_to_<fin>.xlsx (fechas con guión largo).
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_CTR = Alignment(horizontal="center", vertical="center", wrap_text=True)
_HFILL = PatternFill("solid", fgColor="DDEBF7")
_TFILL = PatternFill("solid", fgColor="FCE4D6")

# (clave-en-row, decimales) en orden de columna de datos (col 4 en adelante)
_DATA = [("inversion", 0), ("ganancia", 2),
         ("roi_min_pct", 2), ("roi_min_usd", 2), ("roi_max_pct", 2), ("roi_max_usd", 2),
         ("roi_avg_pct", 2), ("roi_avg_usd", 2), ("n_roi_pos", 0), ("n_roi_neg", 0), ("n_err", 0)]


def output_filename(seed) -> str:
    tipo = seed.tipo.replace(" ", "_")
    tks = "_".join(seed.tickers)
    fi = seed.fecha_inicial.replace("-", "–")          # restituir guión largo (como el template)
    ff = seed.fecha_final.replace("-", "–")
    return f"Backtesting_Results_{tipo}_of_{tks}_from_{fi}_to_{ff}.xlsx"


def _headers(ws, tipo: str) -> None:
    ws["A1"] = "📝 Meta"
    ws.merge_cells("B1:N1")
    t = ws["B1"]; t.value = f"🚪 RESULTADOS DE SALIDA - Operación {tipo.upper()}"
    t.font = Font(bold=True, size=12); t.alignment = _CTR; t.fill = _TFILL
    # columnas que ocupan r2+r3 (merge vertical)
    for col, name in (("A", "ID"), ("B", "Ticker"), ("C", "Fecha"),
                      ("D", "Inversión total"), ("E", "Ganancia"), ("N", "Número de errores")):
        ws.merge_cells(f"{col}2:{col}3")
        ws[f"{col}2"] = name
    # grupos (r2 merge horizontal) + sub-headers (r3)
    for c0, c1, grp, sub0, sub1 in (("F", "G", "Valor mínimo", "ROI (%)", "ROI ($)"),
                                    ("H", "I", "Valor máximo", "ROI (%)", "ROI ($)"),
                                    ("J", "K", "Promedios de todos", "Los ROI (%)", "Los ROI ($)"),
                                    ("L", "M", "Número ROI(%)", "> 0", "<= 0")):
        ws.merge_cells(f"{c0}2:{c1}2")
        ws[f"{c0}2"] = grp
        ws[f"{c0}3"] = sub0; ws[f"{c1}3"] = sub1
    for row in (2, 3):
        for cc in range(1, 15):
            cell = ws.cell(row, cc)
            cell.font = Font(bold=True, size=9); cell.alignment = _CTR; cell.fill = _HFILL
    ws.row_dimensions[2].height = 28


def _copy_listas(wb, listas_src) -> None:
    """Copia la hoja «Listas» del template de entrada (dropdowns), si se provee."""
    if not listas_src:
        return
    try:
        from openpyxl import load_workbook
        src = load_workbook(Path(listas_src), read_only=True, data_only=True)
        if "Listas" in src.sheetnames:
            ws = wb.create_sheet("Listas")
            for r in src["Listas"].iter_rows(values_only=True):
                ws.append(list(r))
    except Exception:
        pass


def build(seed, rows: list, listas_src=None) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Backtesting Results"
    _headers(ws, seed.tipo)
    _tk = {t: i for i, t in enumerate(seed.tickers)}   # orden del seed (QQQ→SPY→IWM), no alfabético
    ordered = sorted(rows, key=lambda r: (_tk.get(str(r.get("Ticker")), 999), str(r.get("Fecha")), str(r.get("ID"))))
    for r in ordered:
        vals = [r.get("ID"), r.get("Ticker"), r.get("Fecha")]
        for key, dec in _DATA:
            v = r.get(key)
            vals.append(round(v, dec) if isinstance(v, (int, float)) else v)
        ws.append(vals)
    ws.freeze_panes = "A4"
    widths = [8, 8, 12, 13, 11, 10, 10, 10, 10, 11, 11, 9, 9, 11]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    _copy_listas(wb, listas_src)
    return wb


def write(seed, rows: list, out_dir, listas_src=None) -> Path:
    """Arma y guarda el workbook. Devuelve el Path."""
    wb = build(seed, rows, listas_src=listas_src)
    out = Path(out_dir) / output_filename(seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# ── Modo «rellenar el results file provisto»: 1 fila por ESCENARIO (agrega sus runs) ──────────
def aggregate_by_scenario(rows: list) -> dict:
    """Agrega las filas por-posición → resumen por ID usando el ROI FINAL de cada run.
    Valor mín/máx = peor/mejor run · Promedios = media de los runs · Inversión/Ganancia = sumas."""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        groups[str(r.get("ID"))].append(r)
    out = {}
    for sid, rs in groups.items():
        inv_tot = sum(float(r.get("inversion") or 0.0) for r in rs)
        gan_tot = sum(float(r.get("ganancia") or 0.0) for r in rs)
        ok = [r for r in rs if not r.get("n_err")]
        usds = [float(r.get("ganancia") or 0.0) for r in ok]
        pcts = [float(r["ganancia"]) / float(r["inversion"]) * 100.0
                for r in ok if float(r.get("inversion") or 0.0) > 0]
        out[sid] = {
            "inversion": inv_tot, "ganancia": gan_tot,
            "roi_min_pct": min(pcts) if pcts else 0.0, "roi_min_usd": min(usds) if usds else 0.0,
            "roi_max_pct": max(pcts) if pcts else 0.0, "roi_max_usd": max(usds) if usds else 0.0,
            "roi_avg_pct": (sum(pcts) / len(pcts)) if pcts else 0.0,
            "roi_avg_usd": (sum(usds) / len(usds)) if usds else 0.0,
            "n_roi_pos": sum(1 for u in usds if u > 0),
            "n_roi_neg": sum(1 for u in usds if u <= 0),
            "n_err": sum(int(r.get("n_err") or 0) for r in rs),
        }
    return out


def fill_results(seed, rows: list, template_path, out_path) -> Path:
    """Rellena el results file provisto (hoja «Backtesting Results»): 1 fila por escenario,
    matcheado por ID en la col A (filas 4+). Preserva formato/estructura del archivo. Devuelve el Path."""
    from openpyxl import load_workbook
    agg = aggregate_by_scenario(rows)
    tickers_lbl = ", ".join(seed.tickers)
    fecha_lbl = f"{seed.fecha_inicial} → {seed.fecha_final}"
    wb = load_workbook(Path(template_path))
    ws = wb["Backtesting Results"] if "Backtesting Results" in wb.sheetnames else wb.active
    _decs = [0, 2, 2, 2, 2, 2, 2, 2, 0, 0, 0]   # D..N (mismo orden que _DATA)
    for row in ws.iter_rows(min_row=4):
        sid = str(row[0].value or "").strip()
        a = agg.get(sid)
        if not sid or a is None:
            continue
        row[1].value = tickers_lbl          # B · Ticker (alcance)
        row[2].value = fecha_lbl            # C · Fecha (rango)
        for i, (key, dec) in enumerate(zip([k for k, _ in _DATA], _decs)):
            v = a.get(key)
            row[3 + i].value = round(v, dec) if isinstance(v, (int, float)) else v
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out

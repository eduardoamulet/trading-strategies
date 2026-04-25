"""Generate Trade_Journal_Template.xlsx with full conditional formatting."""
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from pathlib import Path

OUT = Path(r"C:\Users\ROG ZEPHYRUS\OneDrive\Documents\Claude AI\Traiding\Trade_Journal_Template_v2.xlsx")

# ---- Styles ----
ARIAL = "Arial"
BOLD = Font(name=ARIAL, size=10, bold=True)
BODY = Font(name=ARIAL, size=10)
TITLE = Font(name=ARIAL, size=14, bold=True)
HEADER_FILL = PatternFill("solid", start_color="CCE5FF")   # light blue
GRAY_FILL   = PatternFill("solid", start_color="D9D9D9")
GREEN_FILL  = PatternFill("solid", start_color="C6EFCE")
RED_FILL    = PatternFill("solid", start_color="FFC7CE")
YELLOW_FILL = PatternFill("solid", start_color="FFEB9C")

GREEN_FONT  = Font(name=ARIAL, size=10, color="006100")
RED_FONT    = Font(name=ARIAL, size=10, color="9C0006")
YELLOW_FONT = Font(name=ARIAL, size=10, color="9C6500")

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT   = Alignment(horizontal="left",   vertical="center", wrap_text=True)

thin = Side(style="thin", color="808080")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)


def border_range(ws, rng: str) -> None:
    for row in ws[rng]:
        for cell in row:
            cell.border = BORDER


# ============================================================================
# Sheet 1: Checklist Pre-Trade
# ============================================================================
wb = Workbook()
s1 = wb.active
s1.title = "Checklist Pre-Trade"

s1["A1"] = "DATE:"; s1["A1"].font = BOLD
s1["C1"] = "TICKER:"; s1["C1"].font = BOLD
s1["E1"] = "RANGO PRECIO:"; s1["E1"].font = BOLD

# Checklist header row 3
for col, val in zip(["A3", "B3", "C3", "D3"], ["No", "REQUISITOS", "SE CUMPLE", "NO SE CUMPLE"]):
    s1[col] = val
    s1[col].font = BOLD
    s1[col].fill = HEADER_FILL
    s1[col].alignment = CENTER

conds = [
    "Reunión FED (Cada 45 días)",
    "EARNING (Cada 3 meses)",
    "BOLLINGER: 15/Hora/Diario - Punto medio Diario (Resistencia/Soporte)",
    "PROMEDIOS MÓVILES: Techos/Pisos - Analizar HORA/DIA",
    "PUNTOS DE RUPTURA DE LÍNEAS DE TENDENCIA",
    "SALTO AL ALZA (GAP) / SALTO A LA BAJA (GAP)",
    "Precio BID - ASK - Diferencia",
]
for i, cond in enumerate(conds):
    r = 4 + i
    s1.cell(row=r, column=1, value=i + 1).alignment = CENTER
    s1.cell(row=r, column=2, value=cond).alignment = LEFT
    if i == 3:  # condition 4: free text
        s1.merge_cells(start_row=r, start_column=3, end_row=r, end_column=4)
        c = s1.cell(row=r, column=3, value="(describe análisis HORA/DIA aquí)")
        c.font = Font(name=ARIAL, size=10, italic=True, color="808080")
        c.alignment = LEFT
    else:
        s1.cell(row=r, column=3).alignment = CENTER
        s1.cell(row=r, column=4).alignment = CENTER

border_range(s1, "A3:D10")
s1.row_dimensions[6].height = 30  # BOLLINGER row
s1.row_dimensions[7].height = 30  # PROMEDIOS row

# BID/ASK/Diferencia
s1["B11"] = "   BID"; s1["B11"].font = BOLD
s1["D11"] = "ASK"; s1["D11"].font = BOLD
s1["B12"] = "   Diferencia"; s1["B12"].font = BOLD
s1["C12"] = "=IFERROR(E11-C11,\"\")"
s1["C12"].number_format = "$#,##0.00"
for coord in ["C11", "E11", "C12"]:
    s1[coord].border = BORDER
    s1[coord].alignment = CENTER

# Weekly table (row 14)
s1["A14"] = 8
s1["B14"] = "ANÁLISIS SEMANAL"
for c in "AB":
    cell = s1[f"{c}14"]
    cell.font = BOLD
    cell.fill = HEADER_FILL

headers_week = ["Día", "Distancia", "Spot Price", "Strike Price"]
for i, h in enumerate(headers_week):
    c = s1.cell(row=15, column=2 + i, value=h)
    c.font = BOLD
    c.fill = GRAY_FILL
    c.alignment = CENTER

dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]
for i, d in enumerate(dias):
    s1.cell(row=16 + i, column=2, value=d).font = BOLD

border_range(s1, "B15:E20")

# Trade blocks
trade_headers = [
    "Fecha Exp", "Tipo", "Fecha", "Hora",
    "N° Contratos", "Trade Price", "Rentabilidad $", "Plan %",
]
for trade_num, start_row in [(9, 22), (10, 26)]:
    s1.cell(row=start_row, column=1, value=trade_num).font = BOLD
    t = s1.cell(row=start_row, column=2, value=f"TRADE #{trade_num - 8}")
    t.font = BOLD
    t.fill = HEADER_FILL
    for i, h in enumerate(trade_headers):
        c = s1.cell(row=start_row + 1, column=2 + i, value=h)
        c.font = BOLD
        c.fill = GRAY_FILL
        c.alignment = CENTER
    # Number formats on input row
    input_row = start_row + 2
    s1.cell(row=input_row, column=7).number_format = "$#,##0.00"  # Trade Price
    s1.cell(row=input_row, column=8).number_format = "$#,##0.00"  # Rentabilidad $
    s1.cell(row=input_row, column=9).number_format = "0.00%"      # Plan %
    border_range(s1, f"B{start_row + 1}:I{input_row}")

# Bottom summary
s1["B30"] = "TODAS LAS CONDICIONES SE CUMPLEN:"
s1["B30"].font = Font(name=ARIAL, size=12, bold=True)
s1["D30"] = '=IF(COUNTIF(C4:C10,"<>")+COUNTA(C7)>=7,"SÍ","REVISAR")'
s1["D30"].font = Font(name=ARIAL, size=12, bold=True)
s1["D30"].alignment = CENTER

# Conditional formatting on cell D30
s1.conditional_formatting.add(
    "D30",
    CellIsRule(operator="equal", formula=['"SÍ"'], fill=GREEN_FILL, font=GREEN_FONT),
)
s1.conditional_formatting.add(
    "D30",
    CellIsRule(operator="equal", formula=['"REVISAR"'], fill=YELLOW_FILL, font=YELLOW_FONT),
)

# SE CUMPLE / NO SE CUMPLE conditional formatting (C4:D10, skip C7 which is merged)
for col in ["C", "D"]:
    for r in [4, 5, 6, 8, 9, 10]:  # skip row 7 (merged)
        addr = f"{col}{r}"
        s1.conditional_formatting.add(
            addr,
            FormulaRule(formula=[f'LEN(TRIM({addr}))>0'], fill=GREEN_FILL if col == "C" else RED_FILL),
        )

# Data validation for SE CUMPLE / NO SE CUMPLE (any mark is valid)
dv_check = DataValidation(type="list", formula1='"X,✓"', allow_blank=True)
s1.add_data_validation(dv_check)
for r in [4, 5, 6, 8, 9, 10]:
    dv_check.add(f"C{r}")
    dv_check.add(f"D{r}")

# Column widths
widths = {1: 5, 2: 45, 3: 15, 4: 18, 5: 15, 6: 15, 7: 15, 8: 18, 9: 12}
for col, w in widths.items():
    s1.column_dimensions[get_column_letter(col)].width = w

s1.sheet_view.zoomScale = 100

# Default font for used cells
for row in s1.iter_rows():
    for cell in row:
        if cell.font.name is None:
            cell.font = BODY

# ============================================================================
# Sheet 2: Trade Log
# ============================================================================
s2 = wb.create_sheet("Trade Log")

log_headers = [
    "#", "Fecha", "Ticker", "Rango Precio",
    "1. FED", "2. Earning", "3. Bollinger", "4. MM",
    "5. Ruptura", "6. Gap", "7. BID-ASK OK",
    "BID", "ASK", "Diferencia", "Spot", "Strike",
    "Fecha Exp", "Tipo", "Hora", "N° Contratos", "Trade Price",
    "Rentabilidad $", "Rentabilidad %", "Plan %",
    "Status", "Notas",
]
for i, h in enumerate(log_headers):
    c = s2.cell(row=1, column=i + 1, value=h)
    c.font = BOLD
    c.fill = HEADER_FILL
    c.alignment = CENTER
    c.border = BORDER

# Freeze top row
s2.freeze_panes = "A2"

# Column widths
log_widths = [5, 12, 10, 14, 8, 10, 11, 8, 10, 8, 12, 10, 10, 12, 10, 10,
              12, 8, 8, 11, 12, 14, 14, 10, 20, 30]
for i, w in enumerate(log_widths):
    s2.column_dimensions[get_column_letter(i + 1)].width = w

# Number formats
money_cols = [12, 13, 14, 15, 16, 21, 22]  # BID, ASK, Dif, Spot, Strike, Trade Price, Rent $
pct_cols = [23, 24]  # Rent %, Plan %
date_cols = [2, 17]  # Fecha, Fecha Exp

for r in range(2, 52):
    for c in money_cols:
        s2.cell(row=r, column=c).number_format = "$#,##0.00"
    for c in pct_cols:
        s2.cell(row=r, column=c).number_format = "0.00%"
    for c in date_cols:
        s2.cell(row=r, column=c).number_format = "dd/mm/yyyy"
    # Formulas
    s2.cell(row=r, column=1, value=f"=IF(B{r}=\"\",\"\",ROW()-1)")
    s2.cell(row=r, column=14, value=(
        f"=IFERROR(IF(AND(L{r}<>\"\",M{r}<>\"\"),M{r}-L{r},\"\"),\"\")"
    ))
    s2.cell(row=r, column=23, value=(
        f"=IFERROR(V{r}/(U{r}*T{r}),\"\")"
    ))

# Apply borders to used range
border_range(s2, f"A1:Z51")

# Data validations — SÍ / NO dropdowns for condition columns (E:K = 5..11)
dv_yesno = DataValidation(type="list", formula1='"SÍ,NO"', allow_blank=True)
s2.add_data_validation(dv_yesno)
for col in range(5, 12):  # E to K
    letter = get_column_letter(col)
    dv_yesno.add(f"{letter}2:{letter}51")

# Tipo dropdown (CALL/PUT)
dv_tipo = DataValidation(type="list", formula1='"CALL,PUT"', allow_blank=True)
s2.add_data_validation(dv_tipo)
dv_tipo.add("R2:R51")  # column 18

# Status dropdown
dv_status = DataValidation(
    type="list",
    formula1='"Abierto,Cerrado Ganador,Cerrado Perdedor,Cancelado"',
    allow_blank=True,
)
s2.add_data_validation(dv_status)
dv_status.add("Y2:Y51")  # column 25

# Conditional formatting on condition columns (E:K)
for col in range(5, 12):
    letter = get_column_letter(col)
    rng = f"{letter}2:{letter}51"
    s2.conditional_formatting.add(
        rng,
        CellIsRule(operator="equal", formula=['"SÍ"'], fill=GREEN_FILL, font=GREEN_FONT),
    )
    s2.conditional_formatting.add(
        rng,
        CellIsRule(operator="equal", formula=['"NO"'], fill=RED_FILL, font=RED_FONT),
    )

# Conditional formatting on Status (Y)
s2.conditional_formatting.add(
    "Y2:Y51",
    CellIsRule(operator="equal", formula=['"Cerrado Ganador"'], fill=GREEN_FILL, font=GREEN_FONT),
)
s2.conditional_formatting.add(
    "Y2:Y51",
    CellIsRule(operator="equal", formula=['"Cerrado Perdedor"'], fill=RED_FILL, font=RED_FONT),
)
s2.conditional_formatting.add(
    "Y2:Y51",
    CellIsRule(operator="equal", formula=['"Abierto"'], fill=YELLOW_FILL, font=YELLOW_FONT),
)

# Conditional formatting on Rentabilidad $ column V (22) - color based on sign
s2.conditional_formatting.add(
    "V2:V51",
    CellIsRule(operator="greaterThan", formula=["0"], fill=GREEN_FILL),
)
s2.conditional_formatting.add(
    "V2:V51",
    CellIsRule(operator="lessThan", formula=["0"], fill=RED_FILL),
)

# Default font for all cells in the range
for row in s2.iter_rows(min_row=1, max_row=51, max_col=26):
    for cell in row:
        if not cell.font.bold:
            cell.font = BODY

# ============================================================================
# Sheet 3: Estadísticas
# ============================================================================
s3 = wb.create_sheet("Estadísticas")

s3.merge_cells("A1:B1")
s3["A1"] = "ESTADÍSTICAS DEL JOURNAL"
s3["A1"].font = TITLE
s3["A1"].fill = HEADER_FILL
s3["A1"].alignment = CENTER

stats = [
    ("Total trades registrados",   '=COUNTA(\'Trade Log\'!B2:B51)',                    "0"),
    ("Trades cerrados ganadores",  '=COUNTIF(\'Trade Log\'!Y2:Y51,"Cerrado Ganador")', "0"),
    ("Trades cerrados perdedores", '=COUNTIF(\'Trade Log\'!Y2:Y51,"Cerrado Perdedor")',"0"),
    ("Trades abiertos",            '=COUNTIF(\'Trade Log\'!Y2:Y51,"Abierto")',         "0"),
    ("Win Rate %",                 '=IFERROR(B3/(B3+B4),0)',                            "0.00%"),
    ("",                           None,                                                None),
    ("Rentabilidad total ($)",     '=SUM(\'Trade Log\'!V2:V51)',                       "$#,##0.00"),
    ("Rentabilidad promedio ($)",  '=IFERROR(AVERAGEIF(\'Trade Log\'!V2:V51,"<>0"),0)',"$#,##0.00"),
    ("Mayor ganancia ($)",         '=IFERROR(MAX(\'Trade Log\'!V2:V51),0)',            "$#,##0.00"),
    ("Mayor pérdida ($)",          '=IFERROR(MIN(\'Trade Log\'!V2:V51),0)',            "$#,##0.00"),
    ("",                           None,                                                None),
    ("Gross Profit ($)",           '=SUMIF(\'Trade Log\'!V2:V51,">0")',                "$#,##0.00"),
    ("Gross Loss ($)",             '=SUMIF(\'Trade Log\'!V2:V51,"<0")',                "$#,##0.00"),
    ("Profit Factor",              '=IFERROR(B13/ABS(B14),0)',                          "0.00"),
    ("",                           None,                                                None),
    ("Rentabilidad % promedio",    '=IFERROR(AVERAGEIF(\'Trade Log\'!W2:W51,"<>0"),0)',"0.00%"),
    ("Plan % cumplido promedio",   '=IFERROR(AVERAGEIF(\'Trade Log\'!X2:X51,"<>0"),0)',"0.00%"),
]

for i, (label, formula, fmt) in enumerate(stats):
    r = 2 + i
    s3.cell(row=r, column=1, value=label).font = BOLD
    if formula is not None:
        cell = s3.cell(row=r, column=2, value=formula)
        if fmt:
            cell.number_format = fmt
        cell.font = BODY

# Highlight Win Rate and Profit Factor
s3["B6"].font = Font(name=ARIAL, size=11, bold=True)
s3["B15"].font = Font(name=ARIAL, size=11, bold=True)

# Conditional formatting on Win Rate (green if >=50%)
s3.conditional_formatting.add(
    "B6",
    CellIsRule(operator="greaterThanOrEqual", formula=["0.5"], fill=GREEN_FILL, font=GREEN_FONT),
)
s3.conditional_formatting.add(
    "B6",
    CellIsRule(operator="lessThan", formula=["0.5"], fill=RED_FILL, font=RED_FONT),
)

# Profit Factor conditional formatting
s3.conditional_formatting.add(
    "B15",
    CellIsRule(operator="greaterThanOrEqual", formula=["1"], fill=GREEN_FILL, font=GREEN_FONT),
)
s3.conditional_formatting.add(
    "B15",
    CellIsRule(operator="lessThan", formula=["1"], fill=RED_FILL, font=RED_FONT),
)

# Rentabilidad total coloring
s3.conditional_formatting.add(
    "B8",
    CellIsRule(operator="greaterThan", formula=["0"], fill=GREEN_FILL, font=GREEN_FONT),
)
s3.conditional_formatting.add(
    "B8",
    CellIsRule(operator="lessThan", formula=["0"], fill=RED_FILL, font=RED_FONT),
)

s3.column_dimensions["A"].width = 35
s3.column_dimensions["B"].width = 20
border_range(s3, "A2:B18")

# ============================================================================
# Save
# ============================================================================
wb.save(OUT)
print(f"Created: {OUT}")
print(f"Sheets: {wb.sheetnames}")

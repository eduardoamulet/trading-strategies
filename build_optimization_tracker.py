"""Build the Strategy Optimization Tracker XLSX.

Creates a workbook to systematically track backtest results for the
Strategy_Trend_Reversal BB - 1H strategy across all volatility profiles.
"""
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

OUT = Path(__file__).resolve().parent / "Strategy_Optimization_Tracker.xlsx"   # ruta-independiente

ARIAL = "Arial"
BOLD = Font(name=ARIAL, size=10, bold=True)
BOLD_WHITE = Font(name=ARIAL, size=10, bold=True, color="FFFFFF")
BODY = Font(name=ARIAL, size=10)
TITLE = Font(name=ARIAL, size=14, bold=True, color="FFFFFF")
ITALIC = Font(name=ARIAL, size=9, italic=True, color="606060")

TITLE_FILL = PatternFill("solid", start_color="1F4E78")
HEADER_FILL = PatternFill("solid", start_color="CCE5FF")
GREEN = PatternFill("solid", start_color="C6EFCE")
RED = PatternFill("solid", start_color="FFC7CE")
YELLOW = PatternFill("solid", start_color="FFEB9C")
GRAY = PatternFill("solid", start_color="D9D9D9")

A_SECTION = PatternFill("solid", start_color="6AA84F")
B_SECTION = PatternFill("solid", start_color="BF9000")
C_SECTION = PatternFill("solid", start_color="E69138")
D_SECTION = PatternFill("solid", start_color="CC0000")

thin = Side(style="thin", color="808080")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)

SECTION_FILL = {"A": A_SECTION, "B": B_SECTION, "C": C_SECTION, "D": D_SECTION}


def border_range(ws, rng):
    for row in ws[rng]:
        for cell in row:
            cell.border = BORDER


wb = Workbook()

# ============================================================================
# Sheet 1: README
# ============================================================================
s1 = wb.active
s1.title = "README"

s1.merge_cells("A1:G1")
s1["A1"] = "STRATEGY OPTIMIZATION TRACKER — Trend Reversal BB"
s1["A1"].font = TITLE
s1["A1"].fill = TITLE_FILL
s1["A1"].alignment = CENTER
s1.row_dimensions[1].height = 28

readme = [
    ("", ""),
    ("OBJETIVO", "Encontrar la configuración óptima de la estrategia Trend Reversal para cada perfil de volatilidad (A/B/C/D)."),
    ("", ""),
    ("PARÁMETROS FIJOS (validados anteriormente)", ""),
    ("  BB Length", "20"),
    ("  BB Multiplier", "2.0"),
    ("  Macro trend filter", "ON (D/SMA50/slope 5)"),
    ("  Long-only (Enable SHORT = OFF)", "ON"),
    ("  Require SMA20 reset + distance", "ON / 0.3%"),
    ("  Cooldown", "8 bars"),
    ("  Use trailing chandelier stop", "ON"),
    ("  Chandelier ATR length", "22"),
    ("", ""),
    ("PARÁMETROS FIJOS POR DISEÑO DE ESTRATEGIA", ""),
    ("  Timeframe de ejecución", "1 HORA (fijo según Yoel)"),
    ("  Timeframe de confirmación", "15 MIN (fijo según Yoel)"),
    ("", ""),
    ("PARÁMETROS VARIABLES (grid search)", ""),
    ("  Chandelier ATR multiplier", "2.0, 2.5, 3.0"),
    ("", ""),
    ("FASES DE TESTING", ""),
    ("  FASE 1 — Sensibilidad", "3 combos × 4 representativos (1 por perfil) = 12 tests"),
    ("  FASE 2 — Validación", "Mejor config × 2 peers por perfil = 8 tests"),
    ("  TOTAL", "20 tests"),
    ("", ""),
    ("CÓMO USAR", ""),
    ("  1. Abre la hoja 'Fase 1' para ver la lista de tests pendientes", ""),
    ("  2. En TradingView, configura el BACKTEST con los parámetros indicados", ""),
    ("  3. Ejecuta el backtest y copia los resultados (PF, WR, Net $, etc.)", ""),
    ("  4. Pégalos en la fila correspondiente → el Status cambia automáticamente", ""),
    ("  5. Alternativamente: manda el XLSX del export a Claude — él llena la fila", ""),
    ("", ""),
    ("INTERPRETACIÓN DE RESULTADOS", ""),
    ("  PF > 1.0 y WR > 30%", "Estrategia rentable en ese activo/config"),
    ("  PF 0.9 — 1.0", "Breakeven con comisiones 0.05%, rentable con broker $0"),
    ("  PF < 0.9", "No rentable, descartar config"),
    ("", ""),
    ("HOJAS DEL WORKBOOK", ""),
    ("  README", "Esta hoja"),
    ("  Fase 1", "Grid search por perfil (16 tests)"),
    ("  Fase 2", "Validación de config ganadora (8 tests)"),
    ("  Resumen", "Configuración óptima por perfil (auto-calculado)"),
    ("  Leyenda", "Baseline y métricas ya conocidas"),
]

for i, (label, val) in enumerate(readme, start=2):
    if label.strip().isupper() or label in ("PARÁMETROS FIJOS (validados anteriormente)",
                                             "PARÁMETROS VARIABLES (grid search)",
                                             "FASES DE TESTING", "CÓMO USAR",
                                             "INTERPRETACIÓN DE RESULTADOS", "HOJAS DEL WORKBOOK"):
        s1.cell(row=i, column=1, value=label).font = BOLD
        s1.cell(row=i, column=1).fill = HEADER_FILL
        s1.merge_cells(start_row=i, start_column=1, end_row=i, end_column=7)
    else:
        s1.cell(row=i, column=1, value=label).font = BODY
        if val:
            s1.cell(row=i, column=2, value=val).font = BODY

s1.column_dimensions["A"].width = 42
s1.column_dimensions["B"].width = 55


# ============================================================================
# Sheet 2: Fase 1 — Grid search
# ============================================================================
s2 = wb.create_sheet("Fase 1")

s2.merge_cells("A1:N1")
s2["A1"] = "FASE 1 — GRID SEARCH (Sensibilidad de parámetros)"
s2["A1"].font = TITLE
s2["A1"].fill = TITLE_FILL
s2["A1"].alignment = CENTER
s2.row_dimensions[1].height = 24

headers = [
    "#", "Perfil", "Ticker", "Timeframe", "Chandelier",
    "Net Profit $", "Net Profit %", "PF", "Win Rate %", "W/L Ratio",
    "# Trades", "Max DD %", "Status", "Notas",
]
for i, h in enumerate(headers):
    c = s2.cell(row=3, column=i + 1, value=h)
    c.font = BOLD
    c.fill = HEADER_FILL
    c.alignment = CENTER
    c.border = BORDER
s2.row_dimensions[3].height = 22

# Pre-fill tests. "done" items have known results from conversation history.
tests_phase1 = [
    # (Profile, Ticker, TF, Cha, Known_NetProfit, Known_PF, Known_WR, Known_WL, Known_Trades, Known_MaxDD)
    # TF is FIXED at 1H per strategy design (Yoel: 1H execution + 15m confirmation)
    # --- Profile A (SPY) ---
    ("A", "SPY", "1H", 2.0, None, None, None, None, None, None),
    ("A", "SPY", "1H", 2.5, None, None, None, None, None, None),
    ("A", "SPY", "1H", 3.0, None, None, None, None, None, None),
    # --- Profile B (QQQ) ---
    ("B", "QQQ", "1H", 2.0, None, None, None, None, None, None),
    ("B", "QQQ", "1H", 2.5, -74.47, 0.921, 30.52, 2.098, 308, 1.52),
    ("B", "QQQ", "1H", 3.0, -164.47, 0.87, 28.34, 2.20, 307, 2.44),
    # --- Profile C (AAPL) ---
    ("C", "AAPL", "1H", 2.0, None, None, None, None, None, None),
    ("C", "AAPL", "1H", 2.5, None, None, None, None, None, None),
    ("C", "AAPL", "1H", 3.0, None, None, None, None, None, None),
    # --- Profile D (TSLA) ---
    ("D", "TSLA", "1H", 2.0, None, None, None, None, None, None),
    ("D", "TSLA", "1H", 2.5, None, None, None, None, None, None),
    ("D", "TSLA", "1H", 3.0, None, None, None, None, None, None),
]

for i, row in enumerate(tests_phase1):
    profile, ticker, tf, cha, net, pf, wr, wl, trades, dd = row
    r = 4 + i
    s2.cell(row=r, column=1, value=i + 1).alignment = CENTER
    # Profile cell colored
    pc = s2.cell(row=r, column=2, value=profile)
    pc.font = BOLD_WHITE
    pc.fill = SECTION_FILL[profile]
    pc.alignment = CENTER
    s2.cell(row=r, column=3, value=ticker).font = BOLD
    s2.cell(row=r, column=3).alignment = CENTER
    s2.cell(row=r, column=4, value=tf).alignment = CENTER
    s2.cell(row=r, column=5, value=cha).alignment = CENTER
    s2.cell(row=r, column=5).number_format = "0.0"
    # Metrics
    if net is not None:
        s2.cell(row=r, column=6, value=net).number_format = "$#,##0.00"
        s2.cell(row=r, column=7, value=f"=F{r}/10000").number_format = "0.00%"
        s2.cell(row=r, column=8, value=pf).number_format = "0.000"
        s2.cell(row=r, column=9, value=wr / 100).number_format = "0.00%"
        s2.cell(row=r, column=10, value=wl).number_format = "0.000"
        s2.cell(row=r, column=11, value=trades).alignment = CENTER
        s2.cell(row=r, column=12, value=dd / 100).number_format = "0.00%"
    # Status formula — auto based on PF
    s2.cell(row=r, column=13, value=(
        f'=IF(H{r}="","Pendiente",'
        f'IF(H{r}>=1,"Rentable",'
        f'IF(H{r}>=0.9,"Breakeven","No rentable")))'
    ))
    s2.cell(row=r, column=13).alignment = CENTER
    s2.cell(row=r, column=13).font = BOLD

# Borders on data range
border_range(s2, f"A3:N{3 + len(tests_phase1)}")

# Column widths
widths = [4, 8, 10, 10, 10, 14, 12, 10, 10, 10, 10, 10, 14, 30]
for i, w in enumerate(widths):
    s2.column_dimensions[get_column_letter(i + 1)].width = w

# Freeze header
s2.freeze_panes = "A4"

# Conditional formatting — Status column
status_range = f"M4:M{3 + len(tests_phase1)}"
s2.conditional_formatting.add(status_range, CellIsRule(operator="equal", formula=['"Rentable"'], fill=GREEN))
s2.conditional_formatting.add(status_range, CellIsRule(operator="equal", formula=['"Breakeven"'], fill=YELLOW))
s2.conditional_formatting.add(status_range, CellIsRule(operator="equal", formula=['"No rentable"'], fill=RED))
s2.conditional_formatting.add(status_range, CellIsRule(operator="equal", formula=['"Pendiente"'], fill=GRAY))

# PF coloring
pf_range = f"H4:H{3 + len(tests_phase1)}"
s2.conditional_formatting.add(pf_range, CellIsRule(operator="greaterThanOrEqual", formula=["1"], fill=GREEN))
s2.conditional_formatting.add(pf_range, CellIsRule(operator="between", formula=["0.9", "0.999"], fill=YELLOW))
s2.conditional_formatting.add(pf_range, CellIsRule(operator="lessThan", formula=["0.9"], fill=RED))

# Net Profit coloring
np_range = f"F4:F{3 + len(tests_phase1)}"
s2.conditional_formatting.add(np_range, CellIsRule(operator="greaterThan", formula=["0"], fill=GREEN))
s2.conditional_formatting.add(np_range, CellIsRule(operator="lessThan", formula=["0"], fill=RED))

# ============================================================================
# Sheet 3: Fase 2 — Validación
# ============================================================================
s3 = wb.create_sheet("Fase 2")

s3.merge_cells("A1:N1")
s3["A1"] = "FASE 2 — VALIDACIÓN DE CONFIG GANADORA (Robustez por perfil)"
s3["A1"].font = TITLE
s3["A1"].fill = TITLE_FILL
s3["A1"].alignment = CENTER
s3.row_dimensions[1].height = 24

# Note row
s3.merge_cells("A2:N2")
s3["A2"] = "Después de Fase 1: usa la mejor config de cada perfil y valida en 2 peers para confirmar robustez. Evita overfitting."
s3["A2"].font = ITALIC
s3["A2"].alignment = CENTER

for i, h in enumerate(headers):
    c = s3.cell(row=4, column=i + 1, value=h)
    c.font = BOLD
    c.fill = HEADER_FILL
    c.alignment = CENTER
    c.border = BORDER

tests_phase2 = [
    # (Profile, Ticker, TF_placeholder, Cha_placeholder)
    ("A", "DIA", "TBD", "TBD"),
    ("A", "GLD", "TBD", "TBD"),
    ("B", "IWM", "TBD", "TBD"),
    ("B", "SMH", "TBD", "TBD"),
    ("C", "MSFT", "TBD", "TBD"),
    ("C", "META", "TBD", "TBD"),
    ("D", "NVDA", "TBD", "TBD"),
    ("D", "AMD", "TBD", "TBD"),
]

for i, (profile, ticker, tf, cha) in enumerate(tests_phase2):
    r = 5 + i
    s3.cell(row=r, column=1, value=i + 1).alignment = CENTER
    pc = s3.cell(row=r, column=2, value=profile)
    pc.font = BOLD_WHITE
    pc.fill = SECTION_FILL[profile]
    pc.alignment = CENTER
    s3.cell(row=r, column=3, value=ticker).font = BOLD
    s3.cell(row=r, column=3).alignment = CENTER
    s3.cell(row=r, column=4, value=tf).alignment = CENTER
    s3.cell(row=r, column=5, value=cha).alignment = CENTER
    s3.cell(row=r, column=13, value=(
        f'=IF(H{r}="","Pendiente",'
        f'IF(H{r}>=1,"Rentable",'
        f'IF(H{r}>=0.9,"Breakeven","No rentable")))'
    ))
    s3.cell(row=r, column=13).alignment = CENTER
    s3.cell(row=r, column=13).font = BOLD

border_range(s3, f"A4:N{4 + len(tests_phase2)}")

for i, w in enumerate(widths):
    s3.column_dimensions[get_column_letter(i + 1)].width = w

s3.freeze_panes = "A5"

# Conditional formatting
status_range3 = f"M5:M{4 + len(tests_phase2)}"
s3.conditional_formatting.add(status_range3, CellIsRule(operator="equal", formula=['"Rentable"'], fill=GREEN))
s3.conditional_formatting.add(status_range3, CellIsRule(operator="equal", formula=['"Breakeven"'], fill=YELLOW))
s3.conditional_formatting.add(status_range3, CellIsRule(operator="equal", formula=['"No rentable"'], fill=RED))
s3.conditional_formatting.add(status_range3, CellIsRule(operator="equal", formula=['"Pendiente"'], fill=GRAY))

pf_range3 = f"H5:H{4 + len(tests_phase2)}"
s3.conditional_formatting.add(pf_range3, CellIsRule(operator="greaterThanOrEqual", formula=["1"], fill=GREEN))
s3.conditional_formatting.add(pf_range3, CellIsRule(operator="between", formula=["0.9", "0.999"], fill=YELLOW))
s3.conditional_formatting.add(pf_range3, CellIsRule(operator="lessThan", formula=["0.9"], fill=RED))

np_range3 = f"F5:F{4 + len(tests_phase2)}"
s3.conditional_formatting.add(np_range3, CellIsRule(operator="greaterThan", formula=["0"], fill=GREEN))
s3.conditional_formatting.add(np_range3, CellIsRule(operator="lessThan", formula=["0"], fill=RED))

# ============================================================================
# Sheet 4: Resumen
# ============================================================================
s4 = wb.create_sheet("Resumen")

s4.merge_cells("A1:H1")
s4["A1"] = "RESUMEN — CONFIGURACIÓN ÓPTIMA POR PERFIL"
s4["A1"].font = TITLE
s4["A1"].fill = TITLE_FILL
s4["A1"].alignment = CENTER
s4.row_dimensions[1].height = 24

summary_headers = ["Perfil", "Mejor Ticker Fase 1", "TF Óptimo", "Chandelier Óptimo",
                   "Mejor PF", "Mejor Net $", "Config Recomendada", "Validado (Fase 2)"]
for i, h in enumerate(summary_headers):
    c = s4.cell(row=3, column=i + 1, value=h)
    c.font = BOLD
    c.fill = HEADER_FILL
    c.alignment = CENTER
    c.border = BORDER

for i, profile in enumerate("ABCD"):
    r = 4 + i
    pc = s4.cell(row=r, column=1, value=profile)
    pc.font = BOLD_WHITE
    pc.fill = SECTION_FILL[profile]
    pc.alignment = CENTER
    # Use INDEX/MATCH to find the best PF row per profile in Fase 1
    rng = f"'Fase 1'!$B$4:$B$15"
    # Best ticker (of winning row)
    s4.cell(row=r, column=2, value=(
        f'=IFERROR(INDEX(\'Fase 1\'!$C$4:$C$15,'
        f'MATCH(MAX(IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15)),'
        f'IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15),0)),"-")'
    ))
    # Best TF
    s4.cell(row=r, column=3, value=(
        f'=IFERROR(INDEX(\'Fase 1\'!$D$4:$D$15,'
        f'MATCH(MAX(IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15)),'
        f'IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15),0)),"-")'
    ))
    # Best Chandelier
    s4.cell(row=r, column=4, value=(
        f'=IFERROR(INDEX(\'Fase 1\'!$E$4:$E$15,'
        f'MATCH(MAX(IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15)),'
        f'IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15),0)),"-")'
    ))
    s4.cell(row=r, column=4).number_format = "0.0"
    # Best PF
    s4.cell(row=r, column=5, value=(
        f'=IFERROR(MAX(IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15)),"-")'
    ))
    s4.cell(row=r, column=5).number_format = "0.000"
    # Best Net
    s4.cell(row=r, column=6, value=(
        f'=IFERROR(INDEX(\'Fase 1\'!$F$4:$F$15,'
        f'MATCH(MAX(IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15)),'
        f'IF({rng}="{profile}",\'Fase 1\'!$H$4:$H$15),0)),"-")'
    ))
    s4.cell(row=r, column=6).number_format = "$#,##0.00"
    # Config
    s4.cell(row=r, column=7, value=(
        f'=IF(E{r}="-","Pendiente",C{r}&" / "&D{r}&"x ATR")'
    ))
    s4.cell(row=r, column=7).alignment = CENTER
    s4.cell(row=r, column=7).font = BOLD
    # Validation status (% peers confirmados)
    peer_range = f"'Fase 2'!$B$5:$B$12"
    pf_range_f2 = f"'Fase 2'!$H$5:$H$12"
    s4.cell(row=r, column=8, value=(
        f'=IFERROR(COUNTIFS({peer_range},"{profile}",{pf_range_f2},">=1")&"/2","0/2")'
    ))
    s4.cell(row=r, column=8).alignment = CENTER

border_range(s4, "A3:H7")

widths_s4 = [8, 18, 12, 18, 12, 14, 25, 18]
for i, w in enumerate(widths_s4):
    s4.column_dimensions[get_column_letter(i + 1)].width = w

pf_range_s4 = "E4:E7"
s4.conditional_formatting.add(pf_range_s4, CellIsRule(operator="greaterThanOrEqual", formula=["1"], fill=GREEN))
s4.conditional_formatting.add(pf_range_s4, CellIsRule(operator="between", formula=["0.9", "0.999"], fill=YELLOW))
s4.conditional_formatting.add(pf_range_s4, CellIsRule(operator="lessThan", formula=["0.9"], fill=RED))

# ============================================================================
# Sheet 5: Leyenda / Baseline
# ============================================================================
s5 = wb.create_sheet("Leyenda")

s5.merge_cells("A1:D1")
s5["A1"] = "LEYENDA DE PERFILES + BASELINE CONOCIDO"
s5["A1"].font = TITLE
s5["A1"].fill = TITLE_FILL
s5["A1"].alignment = CENTER
s5.row_dimensions[1].height = 24

legend = [
    ("Perfil", "Descripción", "ATR% diario", "IV histórica"),
    ("A", "Baja vol - ETFs broad market", "~0.8-1.2%", "12-18%"),
    ("B", "Vol media - ETFs tech / stables", "~1.3-1.8%", "20-28%"),
    ("C", "Vol media-alta - Big Tech / finanzas", "~1.8-2.5%", "25-40%"),
    ("D", "Alta vol - Growth / semis / leveraged", "~3-5%", "45-70%"),
]
for i, row in enumerate(legend):
    r = 3 + i
    for j, val in enumerate(row):
        c = s5.cell(row=r, column=j + 1, value=val)
        if i == 0:
            c.font = BOLD
            c.fill = HEADER_FILL
        else:
            c.font = BODY
            if j == 0:
                c.font = BOLD_WHITE
                c.fill = SECTION_FILL[val]
                c.alignment = CENTER
        c.border = BORDER

# Widths
s5.column_dimensions["A"].width = 8
s5.column_dimensions["B"].width = 42
s5.column_dimensions["C"].width = 14
s5.column_dimensions["D"].width = 14

wb.save(OUT)
print(f"Created: {OUT}")
print(f"Sheets: {wb.sheetnames}")

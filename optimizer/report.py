"""Generate the XLSX optimization report with heatmaps per ticker."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import ColorScaleRule, CellIsRule
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
IN_CSV = ROOT / "optimizer_results" / "grid_full.csv"
OUT = ROOT / "Strategy_Optimization_Report.xlsx"

# Volatility profile mapping for our 12 tickers
PROFILE = {
    # A
    "SPY": "A", "DIA": "A", "GLD": "A",
    # B
    "QQQ": "B", "IWM": "B", "SMH": "B",
    # C
    "AAPL": "C", "MSFT": "C", "META": "C",
    # D
    "TSLA": "D", "NVDA": "D", "AMD": "D",
}

PROFILE_COLOR = {
    "A": "6AA84F",  # green
    "B": "BF9000",  # yellow
    "C": "E69138",  # orange
    "D": "CC0000",  # red
}

ARIAL = "Arial"
BOLD = Font(name=ARIAL, size=10, bold=True)
BOLD_WHITE = Font(name=ARIAL, size=10, bold=True, color="FFFFFF")
BODY = Font(name=ARIAL, size=10)
TITLE = Font(name=ARIAL, size=14, bold=True, color="FFFFFF")

TITLE_FILL = PatternFill("solid", start_color="1F4E78")
HEADER_FILL = PatternFill("solid", start_color="CCE5FF")
GREEN = PatternFill("solid", start_color="C6EFCE")
RED = PatternFill("solid", start_color="FFC7CE")
YELLOW = PatternFill("solid", start_color="FFEB9C")
GRAY = PatternFill("solid", start_color="D9D9D9")

thin = Side(style="thin", color="808080")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def border_range(ws, rng):
    for row in ws[rng]:
        for cell in row:
            cell.border = BORDER


def fill_title(ws, title: str, *, merge: str):
    ws.merge_cells(merge)
    c = ws[merge.split(":")[0]]
    c.value = title
    c.font = TITLE
    c.fill = TITLE_FILL
    c.alignment = CENTER
    row = int("".join(ch for ch in merge.split(":")[0] if ch.isdigit()))
    ws.row_dimensions[row].height = 26


def fmt_headers(ws, row, headers):
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=i + 1, value=h)
        c.font = BOLD
        c.fill = HEADER_FILL
        c.alignment = CENTER
        c.border = BORDER


def main():
    df = pd.read_csv(IN_CSV)
    df["profile"] = df["ticker"].map(PROFILE)
    wb = Workbook()

    # ============================================================
    # Sheet 1: Resumen ejecutivo — config óptima por ticker
    # ============================================================
    s1 = wb.active
    s1.title = "Resumen"

    fill_title(s1, "RESUMEN EJECUTIVO — Config óptima por ticker", merge="A1:L1")

    # Get best by Net Profit (user priority) and by PF
    best_net = df.loc[df.groupby("ticker")["net_profit"].idxmax()].copy()
    best_net = best_net.sort_values(["profile", "net_profit"], ascending=[True, False])

    headers = ["Perfil", "Ticker", "Chandelier ATR multiplier", "Chandelier ATR length",
               "Macro SMA length", "Cooldown bars", "Reset distance %",
               "Trades", "Win Rate", "PF", "Net $", "Max DD %"]
    fmt_headers(s1, 3, headers)

    for i, (_, row) in enumerate(best_net.iterrows()):
        r = 4 + i
        pc = s1.cell(row=r, column=1, value=row["profile"])
        pc.font = BOLD_WHITE
        pc.fill = PatternFill("solid", start_color=PROFILE_COLOR[row["profile"]])
        pc.alignment = CENTER

        s1.cell(row=r, column=2, value=row["ticker"]).font = BOLD
        s1.cell(row=r, column=2).alignment = CENTER
        s1.cell(row=r, column=3, value=row["trail_atr_mult"]).number_format = "0.0"
        s1.cell(row=r, column=4, value=int(row["trail_atr_len"])).alignment = CENTER
        s1.cell(row=r, column=5, value=int(row["macro_sma_len"])).alignment = CENTER
        s1.cell(row=r, column=6, value=int(row["cooldown_bars"])).alignment = CENTER
        s1.cell(row=r, column=7, value=row["reset_distance_pct"]).number_format = "0.00"
        s1.cell(row=r, column=8, value=int(row["total_trades"])).alignment = CENTER
        s1.cell(row=r, column=9, value=row["win_rate"]).number_format = "0.00%"
        s1.cell(row=r, column=10, value=row["profit_factor"]).number_format = "0.000"
        s1.cell(row=r, column=11, value=row["net_profit"]).number_format = "$#,##0.00"
        s1.cell(row=r, column=12, value=row["max_drawdown_pct"]).number_format = "0.00%"

    n = len(best_net)
    border_range(s1, f"A3:L{3 + n}")

    widths = [8, 10, 14, 14, 14, 12, 14, 9, 10, 9, 12, 10]
    for i, w in enumerate(widths):
        s1.column_dimensions[get_column_letter(i + 1)].width = w

    s1.freeze_panes = "A4"
    # Wrap headers and increase header row height
    s1.row_dimensions[3].height = 34
    for col in range(1, len(headers) + 1):
        s1.cell(row=3, column=col).alignment = CENTER

    # Conditional formatting on PF and Net
    pf_rng = f"J4:J{3 + n}"
    s1.conditional_formatting.add(pf_rng, CellIsRule(operator="greaterThanOrEqual", formula=["2"], fill=GREEN))
    s1.conditional_formatting.add(pf_rng, CellIsRule(operator="between", formula=["1", "1.999"], fill=YELLOW))
    s1.conditional_formatting.add(pf_rng, CellIsRule(operator="lessThan", formula=["1"], fill=RED))

    net_rng = f"K4:K{3 + n}"
    s1.conditional_formatting.add(net_rng, CellIsRule(operator="greaterThan", formula=["0"], fill=GREEN))
    s1.conditional_formatting.add(net_rng, CellIsRule(operator="lessThan", formula=["0"], fill=RED))

    # Note at bottom
    note_row = 3 + n + 2
    s1.cell(row=note_row, column=1,
            value="NOTA: Datos Alpaca 2020-2026 (mercado alcista dominante). "
                  "PF absoluto infla; el ranking relativo sí es confiable.").font = BOLD
    s1.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=12)
    s1.cell(row=note_row, column=1).alignment = Alignment(wrap_text=True, vertical="center")
    s1.row_dimensions[note_row].height = 30

    # ============================================================
    # Sheet 2: Por perfil — mejor config consolidada
    # ============================================================
    s2 = wb.create_sheet("Por Perfil")
    fill_title(s2, "CONFIG ÓPTIMA POR PERFIL DE VOLATILIDAD", merge="A1:J1")

    # Aggregate: for each profile, find the config that works BEST on average
    # across all tickers in that profile.
    agg = (
        df.groupby(["profile", "trail_atr_mult", "trail_atr_len",
                    "macro_sma_len", "cooldown_bars", "reset_distance_pct"])
          .agg({"profit_factor": "mean", "net_profit": "mean",
                "win_rate": "mean", "total_trades": "mean",
                "max_drawdown_pct": "mean"})
          .reset_index()
    )
    best_profile = agg.loc[agg.groupby("profile")["net_profit"].idxmax()].copy()

    headers = ["Perfil", "Chandelier ATR multiplier", "Chandelier ATR length",
               "Macro SMA length", "Cooldown bars", "Reset distance %",
               "Avg PF", "Avg Net $", "Avg Win Rate", "Avg Trades"]
    fmt_headers(s2, 3, headers)

    for i, (_, row) in enumerate(best_profile.iterrows()):
        r = 4 + i
        pc = s2.cell(row=r, column=1, value=row["profile"])
        pc.font = BOLD_WHITE
        pc.fill = PatternFill("solid", start_color=PROFILE_COLOR[row["profile"]])
        pc.alignment = CENTER

        s2.cell(row=r, column=2, value=row["trail_atr_mult"]).number_format = "0.0"
        s2.cell(row=r, column=3, value=int(row["trail_atr_len"])).alignment = CENTER
        s2.cell(row=r, column=4, value=int(row["macro_sma_len"])).alignment = CENTER
        s2.cell(row=r, column=5, value=int(row["cooldown_bars"])).alignment = CENTER
        s2.cell(row=r, column=6, value=row["reset_distance_pct"]).number_format = "0.00"
        s2.cell(row=r, column=7, value=row["profit_factor"]).number_format = "0.000"
        s2.cell(row=r, column=8, value=row["net_profit"]).number_format = "$#,##0.00"
        s2.cell(row=r, column=9, value=row["win_rate"]).number_format = "0.00%"
        s2.cell(row=r, column=10, value=row["total_trades"]).number_format = "0.0"

    n2 = len(best_profile)
    border_range(s2, f"A3:J{3 + n2}")

    widths2 = [8, 14, 14, 14, 12, 14, 10, 12, 12, 10]
    for i, w in enumerate(widths2):
        s2.column_dimensions[get_column_letter(i + 1)].width = w
    s2.row_dimensions[3].height = 34

    # ============================================================
    # Sheet 3: Top 20 global
    # ============================================================
    s3 = wb.create_sheet("Top 20 Global")
    fill_title(s3, "TOP 20 CONFIGURACIONES (por Net Profit absoluto)", merge="A1:L1")

    top = df.sort_values("net_profit", ascending=False).head(20).copy()
    headers = ["Rank", "Perfil", "Ticker", "Chandelier ATR multiplier",
               "Chandelier ATR length", "Macro SMA length", "Cooldown bars",
               "Reset distance %", "Trades", "Win Rate", "PF", "Net $"]
    fmt_headers(s3, 3, headers)

    for i, (_, row) in enumerate(top.iterrows()):
        r = 4 + i
        s3.cell(row=r, column=1, value=i + 1).alignment = CENTER
        pc = s3.cell(row=r, column=2, value=row["profile"])
        pc.font = BOLD_WHITE
        pc.fill = PatternFill("solid", start_color=PROFILE_COLOR[row["profile"]])
        pc.alignment = CENTER
        s3.cell(row=r, column=3, value=row["ticker"]).font = BOLD
        s3.cell(row=r, column=3).alignment = CENTER
        s3.cell(row=r, column=4, value=row["trail_atr_mult"]).number_format = "0.0"
        s3.cell(row=r, column=5, value=int(row["trail_atr_len"])).alignment = CENTER
        s3.cell(row=r, column=6, value=int(row["macro_sma_len"])).alignment = CENTER
        s3.cell(row=r, column=7, value=int(row["cooldown_bars"])).alignment = CENTER
        s3.cell(row=r, column=8, value=row["reset_distance_pct"]).number_format = "0.00"
        s3.cell(row=r, column=9, value=int(row["total_trades"])).alignment = CENTER
        s3.cell(row=r, column=10, value=row["win_rate"]).number_format = "0.00%"
        s3.cell(row=r, column=11, value=row["profit_factor"]).number_format = "0.000"
        s3.cell(row=r, column=12, value=row["net_profit"]).number_format = "$#,##0.00"

    border_range(s3, "A3:L23")

    widths3 = [6, 8, 10, 14, 14, 14, 12, 14, 9, 10, 9, 12]
    for i, w in enumerate(widths3):
        s3.column_dimensions[get_column_letter(i + 1)].width = w

    s3.freeze_panes = "A4"
    s3.row_dimensions[3].height = 34

    # ============================================================
    # Sheet 4+: One sheet per ticker with TOP 10 + heatmap
    # ============================================================
    for ticker in sorted(df["ticker"].unique()):
        g = df[df["ticker"] == ticker]
        profile = PROFILE[ticker]
        ws = wb.create_sheet(f"{profile}_{ticker}")

        # Title with profile color
        ws.merge_cells("A1:I1")
        t = ws["A1"]
        t.value = f"[{profile}] {ticker} — Grid search ({len(g)} combos)"
        t.font = TITLE
        t.fill = PatternFill("solid", start_color=PROFILE_COLOR[profile])
        t.alignment = CENTER
        ws.row_dimensions[1].height = 24

        # Best config info box
        best_row = g.loc[g["net_profit"].idxmax()]
        ws.cell(row=3, column=1, value="MEJOR CONFIG (por Net Profit):").font = BOLD
        info_lines = [
            f"Chandelier: {best_row['trail_atr_mult']}× ATR({int(best_row['trail_atr_len'])})",
            f"Macro SMA: {int(best_row['macro_sma_len'])}",
            f"Cooldown: {int(best_row['cooldown_bars'])} bars",
            f"Reset distance: {best_row['reset_distance_pct']}%",
            f"PF: {best_row['profit_factor']:.3f}  |  Net: ${best_row['net_profit']:.2f}  "
            f"|  Win Rate: {best_row['win_rate']:.1%}  |  Trades: {int(best_row['total_trades'])}",
        ]
        for i, line in enumerate(info_lines):
            ws.cell(row=4 + i, column=1, value=line).font = BODY

        # Top 10 by Net Profit
        top_start = 10
        ws.cell(row=top_start, column=1, value=f"TOP 10 configs por Net Profit").font = BOLD
        ws.cell(row=top_start, column=1).fill = HEADER_FILL

        top_headers = ["Rank", "Chandelier ATR multiplier", "Chandelier ATR length",
                       "Macro SMA length", "Cooldown bars", "Reset distance %",
                       "Trades", "Win Rate", "PF", "Net $"]
        for i, h in enumerate(top_headers):
            c = ws.cell(row=top_start + 1, column=i + 1, value=h)
            c.font = BOLD
            c.fill = HEADER_FILL
            c.alignment = CENTER
            c.border = BORDER

        top10 = g.sort_values("net_profit", ascending=False).head(10)
        for i, (_, row) in enumerate(top10.iterrows()):
            r = top_start + 2 + i
            ws.cell(row=r, column=1, value=i + 1).alignment = CENTER
            ws.cell(row=r, column=2, value=row["trail_atr_mult"]).number_format = "0.0"
            ws.cell(row=r, column=3, value=int(row["trail_atr_len"])).alignment = CENTER
            ws.cell(row=r, column=4, value=int(row["macro_sma_len"])).alignment = CENTER
            ws.cell(row=r, column=5, value=int(row["cooldown_bars"])).alignment = CENTER
            ws.cell(row=r, column=6, value=row["reset_distance_pct"]).number_format = "0.00"
            ws.cell(row=r, column=7, value=int(row["total_trades"])).alignment = CENTER
            ws.cell(row=r, column=8, value=row["win_rate"]).number_format = "0.00%"
            ws.cell(row=r, column=9, value=row["profit_factor"]).number_format = "0.000"
            ws.cell(row=r, column=10, value=row["net_profit"]).number_format = "$#,##0.00"
        border_range(ws, f"A{top_start+1}:J{top_start+11}")

        # Heatmap: Chandelier × Cooldown (marginalized over other params, use mean PF)
        hm_start = top_start + 14
        ws.cell(row=hm_start, column=1, value="HEATMAP: PF promedio por Chandelier × Cooldown").font = BOLD
        ws.cell(row=hm_start, column=1).fill = HEADER_FILL

        pivot = g.pivot_table(
            index="trail_atr_mult", columns="cooldown_bars",
            values="profit_factor", aggfunc="mean",
        )

        # Write pivot
        ws.cell(row=hm_start + 1, column=1, value="Chandelier \\ Cooldown").font = BOLD
        for j, col in enumerate(pivot.columns):
            c = ws.cell(row=hm_start + 1, column=2 + j, value=f"CD={col}")
            c.font = BOLD
            c.fill = HEADER_FILL
            c.alignment = CENTER
        for i, idx in enumerate(pivot.index):
            ws.cell(row=hm_start + 2 + i, column=1, value=f"{idx}×").font = BOLD
            for j, col in enumerate(pivot.columns):
                val = pivot.at[idx, col]
                ws.cell(row=hm_start + 2 + i, column=2 + j, value=float(val))
                ws.cell(row=hm_start + 2 + i, column=2 + j).number_format = "0.000"
        # Heatmap colors
        first_col = 2
        last_col = 1 + len(pivot.columns)
        hm_range = f"{get_column_letter(first_col)}{hm_start+2}:{get_column_letter(last_col)}{hm_start+1+len(pivot.index)}"
        ws.conditional_formatting.add(
            hm_range,
            ColorScaleRule(start_type="min", start_color="F8696B",
                           mid_type="percentile", mid_value=50, mid_color="FFEB84",
                           end_type="max", end_color="63BE7B"),
        )
        border_range(ws, f"A{hm_start+1}:{get_column_letter(last_col)}{hm_start+1+len(pivot.index)}")

        # Column widths (wider to fit long header names)
        ws.column_dimensions["A"].width = 8
        widths_t = [8, 14, 14, 14, 12, 14, 9, 10, 9, 12]
        for i, w in enumerate(widths_t):
            ws.column_dimensions[get_column_letter(i + 1)].width = w
        ws.row_dimensions[top_start + 1].height = 34

    # Save
    wb.save(OUT)
    print(f"Saved: {OUT}")
    print(f"Sheets: {wb.sheetnames}")


if __name__ == "__main__":
    main()

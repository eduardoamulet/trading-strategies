"""Vista de la «Simulación Intradía» — builders PUROS de HTML (sin Streamlit).

`build_matrix_html` arma la matriz Activo × minuto con celdas SOLO color (CALL verde · PUT rojo ·
NO TRADE gris) y un tooltip moderno al hacer hover (con el TradeSignal completo). El HTML se mantiene
liviano: cada celda solo lleva `data-tk`/`data-mn`; todos los datos van en UN blob JSON y un único
tooltip flotante los muestra por JS. `legend_html` y `signal_lines` son helpers de presentación.
"""
from __future__ import annotations

ACTION_COLOR = {"CALL": "#16a34a", "PUT": "#dc2626", "NO TRADE": "#9ca3af"}


def _heat_color(action: str, confidence) -> str:
    """Color de celda en modo HEATMAP: verde (CALL) / rojo (PUT) con la LIGHTNESS según la confianza
    (más confianza → más oscuro), gris para NO TRADE. Confianza 0.4 → claro · 1.0 → oscuro."""
    if action == "CALL":
        hue, sat = 142, 62
    elif action == "PUT":
        hue, sat = 2, 68
    else:
        return "#6b7280"   # NO TRADE — gris fijo
    c = max(0.0, min(1.0, float(confidence or 0.0)))
    t = max(0.0, min(1.0, (c - 0.4) / 0.6))
    return f"hsl({hue}, {sat}%, {72 - 47 * t:.0f}%)"

# CSS del mapa. Se renderiza en el DOM PRINCIPAL (st.markdown) → los enlaces de celda funcionan y NO
# hay iframe que recorte ni deje espacio en blanco. Tooltip = atributo `title` nativo (no se recorta).
_MATRIX_CSS = """
<style>
.simx-wrap{overflow:auto;max-height:690px;border:1px solid #2a2e39;border-radius:8px;background:#0e1117;
  margin:2px 0 6px 0;}
.simx-wrap table{border-collapse:separate;border-spacing:0;font-family:ui-sans-serif,system-ui;}
.simx-wrap th.simx-corner,.simx-wrap th.simx-tk{position:sticky;left:0;z-index:3;background:#161a23;
  color:#d1d4dc;font-size:11px;font-weight:600;text-align:right;padding:2px 8px;white-space:nowrap;}
.simx-wrap thead th{position:sticky;top:0;z-index:2;background:#161a23;color:#8b93a7;font-size:9px;
  font-weight:500;padding:2px 0;white-space:nowrap;}
.simx-wrap th.simx-corner{z-index:4;top:0;}
.simx-wrap td{padding:0;}
.simx-wrap a.simx-c{display:block;width:10px;min-width:10px;height:22px;text-decoration:none;}
.simx-wrap a.simx-c:hover{outline:2px solid #facc15;outline-offset:-2px;}
</style>
"""


def _tooltip(tk: str, mn: str, s: dict) -> str:
    """Texto del tooltip nativo (atributo title) — el TradeSignal completo con saltos de línea."""
    def _n(x):
        return "—" if x is None else x
    rr = s.get("risk_reward")
    lines = [
        f"{tk} · {mn} · {s.get('action', '—')}",
        f"Confianza: {round((s.get('confidence') or 0) * 100)}%  ·  Score: {_n(s.get('score'))}",
        f"Market Strength: {_n(s.get('market_strength'))}  ·  Trend: {_n(s.get('trend'))}",
        f"Entry: {_n(s.get('entry_price'))}  ·  Stop: {_n(s.get('stop'))}  ·  Target: {_n(s.get('target'))}",
        f"Risk Reward: {'—' if rr is None else '1:' + str(rr)}",
    ]
    rs = s.get("reasons") or []
    if rs:
        lines.append("Razones: " + " · ".join(map(str, rs)))
    esc = [ln.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;") for ln in lines]
    return "&#10;".join(esc)


def build_matrix_html(sim: dict, *, heatmap: bool = False) -> str:
    """HTML de la matriz Activo×minuto para **st.markdown** (DOM principal). Cada celda es un ENLACE
    clicable (`?sim_cell=TICKER|MINUTO`) con tooltip nativo (title). `heatmap=True` → intensidad =
    confianza; si no, color plano por acción."""
    minutes = sim.get("minutes", [])
    tickers = sim.get("tickers", [])
    results = sim.get("results", {})
    head_cells = "".join(
        f'<th>{mn if mn.endswith((":00", ":15", ":30", ":45")) else ""}</th>' for mn in minutes)
    _rl = sim.get("row_label", "Activo")            # «Fecha» en modo rango con 1 activo
    head = f'<thead><tr><th class="simx-corner">{_rl} \\ Hora</th>{head_cells}</tr></thead>'
    body_rows = ""
    for tk in tickers:
        cells = ""
        for mn in minutes:
            s = results.get(tk, {}).get(mn, {})
            act = s.get("action", "NO TRADE")
            color = (_heat_color(act, s.get("confidence")) if heatmap
                     else ACTION_COLOR.get(act, "#9ca3af"))
            cells += (f'<td><a class="simx-c" style="background:{color}" href="?sim_cell={tk}|{mn}" '
                      f'target="_self" title="{_tooltip(tk, mn, s)}">&#8203;</a></td>')
        body_rows += f'<tr><th class="simx-tk">{tk}</th>{cells}</tr>'
    return (_MATRIX_CSS
            + f'<div class="simx-wrap"><table>{head}<tbody>{body_rows}</tbody></table></div>')


def legend_html(heatmap: bool = False) -> str:
    """Leyenda. Plano: 🟢 CALL · 🔴 PUT · ⚪ NO TRADE. Heatmap: barras de gradiente (claro→oscuro =
    confianza) para CALL y PUT + gris NO TRADE."""
    if not heatmap:
        return (
            '<div style="display:flex;gap:16px;align-items:center;font-size:13px;margin:2px 0 6px 0;">'
            '<span><span style="color:#16a34a;font-size:15px;">■</span> CALL</span>'
            '<span><span style="color:#dc2626;font-size:15px;">■</span> PUT</span>'
            '<span><span style="color:#9ca3af;font-size:15px;">■</span> NO TRADE</span>'
            '</div>'
        )
    _bar = ("display:inline-block;width:96px;height:13px;border-radius:3px;"
            "vertical-align:middle;border:1px solid #33384a;")
    _green = "linear-gradient(to right, hsl(142,62%,72%), hsl(142,62%,25%))"
    _red = "linear-gradient(to right, hsl(2,68%,72%), hsl(2,68%,25%))"
    return (
        '<div style="display:flex;gap:18px;align-items:center;font-size:12px;margin:2px 0 6px 0;'
        'flex-wrap:wrap;">'
        f'<span>CALL <span style="{_bar}background:{_green};"></span></span>'
        f'<span>PUT <span style="{_bar}background:{_red};"></span></span>'
        '<span><span style="color:#9ca3af;font-size:15px;">■</span> NO TRADE</span>'
        '<span style="color:#8b93a7;">← claro ≈60% · oscuro ≈100% (confianza)</span>'
        '</div>'
    )


def signal_lines(sig: dict) -> list[tuple[str, str]]:
    """(etiqueta, valor) del TradeSignal para el panel lateral. Puro; la UI decide cómo renderizar."""
    def _n(x):
        return "—" if x is None else x
    rr = sig.get("risk_reward")
    return [
        ("Confianza", f"{(sig.get('confidence') or 0) * 100:.0f}%"),
        ("Score", f"{_n(sig.get('score'))}"),
        ("Market Strength", f"{_n(sig.get('market_strength'))}"),
        ("Trend", f"{_n(sig.get('trend'))}"),
        ("Entry Price", f"{_n(sig.get('entry_price'))}"),
        ("Stop", f"{_n(sig.get('stop'))}"),
        ("Target", f"{_n(sig.get('target'))}"),
        ("Risk Reward", "—" if rr is None else f"1:{rr}"),
    ]

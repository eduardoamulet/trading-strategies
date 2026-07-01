"""Medidor (gauge) de dirección — SVG semicircular a partir de un `TradeSignal`.

Construcción PURA (devuelve un string SVG): PUT (rojo, izq) · NEUTRAL (gris) · CALL (verde, der),
con la aguja apuntando al score 0–100 y la acción en el centro. La página lo renderiza; así se
testea sin Streamlit.
"""
from __future__ import annotations

import math

_COLORS = {"CALL": "#16a34a", "PUT": "#dc2626", "NO TRADE": "#6b7280"}


def _pt(cx: float, cy: float, r: float, deg: float):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy - r * math.sin(a)   # SVG y hacia abajo → −sin


def _arc(cx: float, cy: float, r: float, a1: float, a2: float) -> str:
    """Arco de a1 a a2 (grados, a1>a2) sobre el semicírculo superior."""
    x1, y1 = _pt(cx, cy, r, a1)
    x2, y2 = _pt(cx, cy, r, a2)
    return f"M {x1:.1f} {y1:.1f} A {r} {r} 0 0 1 {x2:.1f} {y2:.1f}"


def _score_to_angle(score: float) -> float:
    """score 0→180° (izq/PUT) · 50→90° (arriba) · 100→0° (der/CALL)."""
    return 180.0 - 1.8 * max(0.0, min(100.0, score))


def build_gauge_html(signal) -> str:
    score = float(getattr(signal, "score", 50.0) or 50.0)
    action = signal.action.value if hasattr(signal, "action") else "NO TRADE"
    conf = float(getattr(signal, "confidence", 0.0) or 0.0)
    color = _COLORS.get(action, "#6b7280")

    cx, cy, r = 160.0, 158.0, 120.0
    put = _arc(cx, cy, r, 180, 135)      # 0–25  → rojo
    neu = _arc(cx, cy, r, 135, 45)       # 25–75 → gris
    call = _arc(cx, cy, r, 45, 0)        # 75–100 → verde
    nx, ny = _pt(cx, cy, r - 26, _score_to_angle(score))

    # Ancho tope + centrado → no se escala a lo ancho de la columna ni se recorta.
    return (
        '<div style="max-width:340px;margin:0 auto">'
        f'<svg width="100%" viewBox="0 0 320 196" xmlns="http://www.w3.org/2000/svg"'
        f' font-family="system-ui,-apple-system,sans-serif" role="img"'
        f' aria-label="Medidor de dirección: {action}, fuerza {score:.0f} de 100">'
        f'<path d="{put}" stroke="#dc2626" stroke-width="18" fill="none" stroke-linecap="round" opacity="0.9"/>'
        f'<path d="{neu}" stroke="#cbd5e1" stroke-width="18" fill="none"/>'
        f'<path d="{call}" stroke="#16a34a" stroke-width="18" fill="none" stroke-linecap="round" opacity="0.9"/>'
        f'<line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{color}" stroke-width="5" stroke-linecap="round"/>'
        f'<circle cx="{cx}" cy="{cy}" r="8" fill="{color}"/>'
        f'<text x="{cx}" y="102" text-anchor="middle" font-size="30" font-weight="800" fill="{color}">{action}</text>'
        f'<text x="{cx}" y="128" text-anchor="middle" font-size="13" fill="#64748b">Fuerza {score:.0f}/100 · conf {conf:.0%}</text>'
        f'<text x="28" y="186" text-anchor="middle" font-size="12" font-weight="700" fill="#dc2626">PUT</text>'
        f'<text x="292" y="186" text-anchor="middle" font-size="12" font-weight="700" fill="#16a34a">CALL</text>'
        '</svg></div>'
    )

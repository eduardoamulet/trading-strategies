"""Sistema de estilos centralizado de SignalForge — escala tipográfica + iconografía.

ÚNICA fuente de la jerarquía visual de títulos/subtítulos de TODAS las páginas. Se inyecta una sola
vez desde el entrypoint `trading_suite.py` (que corre antes de cada página vía `st.navigation`), así
que NO hay que repetir tamaños por página ni hardcodearlos.

Para reajustar la escala en toda la app, editá las variables de `:root` (design tokens) — nada más.
Los iconos de los títulos son emojis inline → escalan con el font-size del heading (no se tocan
aparte). El scope es el ÁREA DE CONTENIDO principal (`stMainBlockContainer`); la barra lateral
conserva su propio layout (cada página densa la estiliza a su medida).
"""
from __future__ import annotations

import streamlit as st

# ── Design tokens: escala tipográfica de títulos ────────────────────────────────────────────────
# Jerarquía clara y compacta (antes los defaults de Streamlit eran ~2.75rem para el H1 → enormes).
#   H1 título de página · H2 sección · H3 subsección · H4 sub-subsección · H5 menor.
_THEME_CSS = """
<style>
:root {
  --sf-h1: 1.75rem;   /* Título de página      (st.title    / #)    */
  --sf-h2: 1.35rem;   /* Sección               (st.header   / ##)   */
  --sf-h3: 1.15rem;   /* Subsección            (st.subheader/ ###)  */
  --sf-h4: 1.05rem;   /* Sub-subsección        (#### )              */
  --sf-h5: 0.95rem;   /* Menor                 (##### )             */
}
/* Escala aplicada SOLO al contenido principal — la barra lateral tiene su propio layout. */
[data-testid="stMainBlockContainer"] h1 { font-size: var(--sf-h1) !important; font-weight: 700 !important; line-height: 1.2 !important; }
[data-testid="stMainBlockContainer"] h2 { font-size: var(--sf-h2) !important; font-weight: 700 !important; line-height: 1.25 !important; }
[data-testid="stMainBlockContainer"] h3 { font-size: var(--sf-h3) !important; font-weight: 600 !important; line-height: 1.3 !important; }
[data-testid="stMainBlockContainer"] h4 { font-size: var(--sf-h4) !important; font-weight: 600 !important; line-height: 1.3 !important; }
[data-testid="stMainBlockContainer"] h5 { font-size: var(--sf-h5) !important; font-weight: 600 !important; line-height: 1.3 !important; }
/* Primer título de la página pegado al tope (sin el margen grande por defecto del H1). */
[data-testid="stMainBlockContainer"] h1:first-child { margin-top: 0 !important; padding-top: 0 !important; }
</style>
"""


def apply_theme() -> None:
    """Inyecta la escala tipográfica global. Llamar UNA vez desde el entrypoint (aplica a todas las
    páginas del menú). Es solo CSS → no cambia ninguna funcionalidad."""
    st.markdown(_THEME_CSS, unsafe_allow_html=True)

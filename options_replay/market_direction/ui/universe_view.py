"""Construcción (pura) de la vista del universo operable, a partir del registro de `TickerProfile`.

Sin dependencias de Streamlit: devuelve un DataFrame y textos listos para que la página los renderice.
Así se testea sin levantar la app.
"""
from __future__ import annotations

import pandas as pd

from ..domain import ticker_profile as tp

_DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def tradeable_today_summary(weekday: int):
    """(lista de tickers operables hoy, mensaje markdown) según el día de la semana (0=lunes)."""
    profs = tp.tradeable_on(weekday)
    tickers = [p.ticker for p in profs]
    day = _DAYS_ES[weekday] if 0 <= weekday < 7 else "?"
    if weekday >= 5:
        return tickers, f"Hoy es **{day}**: mercado cerrado, sin 0DTE."
    if weekday == 4:
        return tickers, (f"Hoy es **{day}**: hay 0DTE en los **{len(tickers)}** activos "
                         f"(núcleo diario **+ todo el roster de viernes** se activa hoy). 🔥")
    diarios = ", ".join(tickers)
    return tickers, (f"Hoy es **{day}**: 0DTE solo en los **{len(tickers)} diarios** → {diarios}. "
                     f"El **roster de viernes** (NVDA, AMD, TSLA, COIN…) se activa el **viernes**.")


def _group(p: tp.TickerProfile) -> str:
    if p.in_daily_core:
        return "🎯 Núcleo diario"
    if p.in_friday_roster:
        return "⚡ Roster viernes"
    return "—"


def build_universe_df(weekday: int | None = None, filtro: str = "Todos") -> pd.DataFrame:
    """Tabla del universo (ordenada por movimiento desc). `filtro` ∈
    {Operables hoy, Núcleo diario, Roster viernes, Todos}."""
    profs = tp.all_profiles()
    if filtro == "Operables hoy" and weekday is not None:
        profs = [p for p in profs if p.tradeable_on(weekday)]
    elif filtro == "Núcleo diario":
        profs = [p for p in profs if p.in_daily_core]
    elif filtro == "Roster viernes":
        profs = [p for p in profs if p.in_friday_roster]

    rows = []
    for p in profs:
        hoy = "✅" if (weekday is not None and p.tradeable_on(weekday)) else "—"
        rows.append({
            "Activo": p.ticker,
            "Nombre": p.name,
            "Clase": p.asset_class.value,
            "Movimiento": f"{p.movement_symbol} {p.daily_range_pct:.1f}%",
            "×SPY": round(p.vol_x_spy, 1),
            "0DTE": p.zero_dte.value,
            "Grupo": _group(p),
            "¿Hoy?": hoy,
        })
    return pd.DataFrame(rows)


def _verdict(zero_dte: tp.ZeroDTE, rng: float) -> str:
    """Veredicto para el objetivo «gran movimiento + vence el mismo día»."""
    if zero_dte == tp.ZeroDTE.DAILY:
        return "🎯 Mejor para diario" if rng >= 1.5 else "🗓️ Diario, calmo"
    if rng >= 2.8:
        return "⚡ Solo viernes (gran mov.)"
    if rng >= 1.8:
        return "⚡ Solo viernes"
    return "➖ Flojo (viernes)"


def build_verdict_df(scan: dict | None = None) -> pd.DataFrame:
    """Tabla «Activo · Gran movimiento · 0DTE mismo día · Veredicto», ordenada por movimiento desc.
    Si `scan` (del escáner de volatilidad) trae métricas frescas, sobreescriben el snapshot."""
    metrics = (scan or {}).get("metrics", {})
    items = []
    for p in tp.all_profiles():
        m = metrics.get(p.ticker, {})
        rng = m.get("daily_range_pct", p.daily_range_pct)
        sym = tp.movement_symbol_for(rng)
        items.append((rng, {
            "Activo": p.ticker,
            "Gran movimiento": f"{sym} {rng:.1f}%",
            "0DTE mismo día": p.zero_dte.value,
            "Veredicto": _verdict(p.zero_dte, rng),
        }))
    items.sort(key=lambda t: t[0], reverse=True)
    return pd.DataFrame([r for _, r in items])

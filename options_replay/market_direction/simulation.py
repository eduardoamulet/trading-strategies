"""Coordinador de la «Simulación Intradía» — reproduce minuto a minuto el Market Direction Engine.

LÓGICA PURA (sin Streamlit): recorre cada minuto de la sesión y llama al motor **sin modificarlo**,
guardando el `TradeSignal` COMPLETO por (ticker, minuto). Solo COORDINA los componentes existentes.

Optimización (sin tocar el motor): un único `CachingProvider` memoiza `session`/`previous_session`
por (ticker, fecha), así los ~390 minutos de un activo reusan la MISMA lectura → Polygon se pega 1×
por activo (y 1× por ancla SPY/QQQ/IWM de la confirmación). El modelo y el generador se comparten
(son stateless). Lo que se recomputa por minuto es solo el corte causal `up_to(minuto)` + las
features sobre ese corte — que es justo lo que da la evolución sin look-ahead.
"""
from __future__ import annotations

from typing import Callable, Optional

from .data import default_provider
from .data.caching_provider import CachingProvider
from .decision import SignalGenerator
from .engine import market_direction_engine
from .model import RuleBasedModel

RTH_START_MIN = 9 * 60 + 30   # 09:30 ET
RTH_END_MIN = 16 * 60         # 16:00 ET


def _to_min(hhmm: str) -> int:
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def minute_range(start_hhmm: str = "09:30", end_hhmm: str = "16:00") -> list[str]:
    """Lista de 'HH:MM' desde `start` (incl.) hasta `end` (excl.). Ej: 09:30–16:00 → 390 minutos
    (la última barra operable es 15:59; el cierre 16:00 no se evalúa)."""
    a, b = _to_min(start_hhmm), _to_min(end_hhmm)
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(a, max(a, b))]


def simulate_session(tickers, date: str, start_hhmm: str = "09:30", end_hhmm: str = "16:00", *,
                     provider=None, progress_cb: Optional[Callable] = None) -> dict:
    """Corre el motor por (ticker × minuto). Devuelve un dict con `minutes` + `results`
    ({ticker: {minuto: TradeSignal.to_dict()}}). `progress_cb(done, total, ticker, minute)` alimenta
    la barra de progreso. NO modifica el motor: solo lo invoca con un provider cacheado + model/gen
    compartidos."""
    minutes = minute_range(start_hhmm, end_hhmm)
    tickers = [str(t).upper().strip() for t in tickers if str(t).strip()]
    provider = provider or CachingProvider(default_provider())   # 1 lectura por ticker/fecha
    model = RuleBasedModel()
    generator = SignalGenerator()
    results: dict = {}
    total = max(1, len(tickers) * len(minutes))
    done = 0
    for tk in tickers:
        col: dict = {}
        for mn in minutes:
            sig = market_direction_engine(tk, date, mn, provider=provider,
                                          model=model, generator=generator)
            col[mn] = sig.to_dict()
            done += 1
            if progress_cb and (done % 15 == 0 or done == total):
                progress_cb(done, total, tk, mn)
        results[tk] = col
    return {"date": date, "start": start_hhmm, "end": end_hhmm,
            "minutes": minutes, "tickers": tickers, "results": results}


def results_to_rows(sim: dict) -> list[dict]:
    """Aplana la simulación a filas (1 por ticker×minuto) para exportar a CSV/Excel."""
    rows = []
    for tk in sim.get("tickers", []):
        for mn, sig in sim.get("results", {}).get(tk, {}).items():
            rows.append({
                "Activo": tk, "Fecha": sim.get("date"), "Hora": mn,
                "Acción": sig.get("action"), "Confianza": sig.get("confidence"),
                "Score": sig.get("score"), "Market Strength": sig.get("market_strength"),
                "Trend": sig.get("trend"), "Entry Price": sig.get("entry_price"),
                "Stop": sig.get("stop"), "Target": sig.get("target"),
                "Risk Reward": sig.get("risk_reward"),
                "Reasons": " · ".join(sig.get("reasons") or []),
            })
    return rows

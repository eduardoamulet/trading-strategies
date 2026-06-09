"""Núcleo de estrategia COMPARTIDO entre el backtest (options_replay/engine.py) y el
trading en vivo (live_trader/). Funciones PURAS, sin I/O, sin Streamlit, sin Polygon ni
broker → así la MISMA regla decide en backtest y en vivo (un solo lugar = sin drift).

Lo que vive acá (lo que históricamente estaba duplicado y podía divergir):
  - max_spread_for_price(): la compuerta de spread por bucket de precio (+ override).
  - itm_depth() / itm_rank() / atm_rank(): qué significa "ATM" y "1-ITM".
  - selection_key(): el orden de selección "Opción 1" (menor spread → 1-ITM → liquidez).
  - exit_decision(): la regla de salida (Umbral de ROI / Stop), profit gana empates.
  - passes_liquidity(): gate de OI/volumen.

Unidades de exit_decision: TODO en PORCENTAJE (roi 50.0 = 50%). El engine trabaja
internamente en fracción (0.5) → multiplicá por 100 al cruzar. La REGLA es la misma.

Tabla canónica de spread (la fuente única; engine y live deberían apuntar acá):
"""
from __future__ import annotations

from typing import Optional, Sequence

# Buckets canónicos: [precio_min, precio_max) -> spread máximo aceptable ($).
# Mismos valores que options_replay/engine.py (defaults) y live_trader/settings.py.
DEFAULT_SPREAD_BUCKETS: list[dict] = [
    {"price_min": 0,    "price_max": 100,       "max_spread": 0.03},
    {"price_min": 100,  "price_max": 300,       "max_spread": 0.05},
    {"price_min": 300,  "price_max": 600,       "max_spread": 0.10},
    {"price_min": 600,  "price_max": 1200,      "max_spread": 0.25},
    {"price_min": 1200, "price_max": 1_000_000, "max_spread": 0.50},
]


def max_spread_for_price(spot: float, buckets: Sequence[dict] | None = None,
                         override: Optional[float] = None) -> float:
    """Máximo spread aceptable para un subyacente a precio `spot`.

    Prioridad (idéntica al engine):
      1) `override` (la UI lo setea con 'Spread máximo ($)') → valor PLANO, manda.
      2) el bucket de precio donde cae `spot`.
      3) sin bucket → inf (sin filtro).
    """
    if override is not None:
        return float(override)
    for b in (buckets if buckets is not None else DEFAULT_SPREAD_BUCKETS):
        if b["price_min"] <= spot < b["price_max"]:
            return float(b["max_spread"])
    return float("inf")


def itm_depth(strike: float, spot: float, side: str) -> float:
    """Profundidad ITM ($). >= 0 = in-the-money; < 0 = out-of-the-money.
    CALL: spot - strike (ITM si el spot está por ENCIMA del strike).
    PUT : strike - spot (ITM si el spot está por DEBAJO del strike)."""
    s = (side or "").upper()
    right = "C" if s in ("C", "CALL") else "P"
    return (spot - strike) if right == "C" else (strike - spot)


def itm_rank(depth: float) -> float:
    """Ranking para preferir el "1-ITM" (la menor profundidad ITM primero) y mandar
    los OTM al final. Mismo criterio que engine._itm_rank."""
    return depth if depth >= 0 else abs(depth) + 1e6


def atm_rank(strike: float, spot: float) -> float:
    """Ranking ATM: el strike más cercano al spot primero."""
    return abs(strike - spot)


def _spread_key(spread: Optional[float]) -> float:
    """Spread redondeado a centavo (None → 9.99, va al final). Igual que engine._sp."""
    return round(spread, 2) if spread is not None else 9.99


def selection_key(spread: Optional[float], depth: float, liquidity: float = 0.0,
                  mode: str = "itm") -> tuple:
    """Clave de orden para elegir UN contrato entre candidatos ("Opción 1" canónica).

    Orden (menor = mejor), idéntico al engine para selection_criterion='spread':
      1) menor spread (a centavo),
      2) cercanía a 1-ITM  (mode='itm', default)  ó  cercanía a ATM (mode='atm'),
      3) mayor liquidez (volumen en backtest / open interest en vivo).

    `depth` = itm_depth(strike, spot, side). En mode='atm' se usa |strike-spot|, que se
    deriva de depth (|depth| cuando el lado define el signo) — para ser exactos, pasá
    abs(strike-spot) como `depth` si querés ATM puro; acá mode='itm' es el de Opción 1.
    """
    closeness = itm_rank(depth) if mode == "itm" else abs(depth)
    return (_spread_key(spread), closeness, -(liquidity or 0.0))


def passes_liquidity(open_interest: int, volume: int, min_oi: int, min_vol: int) -> bool:
    """True si el contrato pasa los mínimos de liquidez (OI y volumen)."""
    return (open_interest or 0) >= min_oi and (volume or 0) >= min_vol


def exit_decision(roi_pct: float, umbral_pct: float,
                  stop_pct: float) -> Optional[str]:
    """Regla de salida (todo en PORCENTAJE). Devuelve:
      - 'take_profit'  si roi >= umbral,
      - 'stop_loss'    si roi <= stop,
      - None           si sigue abierta.
    El profit GANA los empates (se evalúa primero) — igual que el engine, donde
    profit_pos <= loss_pos elige el take-profit.
    """
    if roi_pct >= umbral_pct:
        return "take_profit"
    if roi_pct <= stop_pct:
        return "stop_loss"
    return None

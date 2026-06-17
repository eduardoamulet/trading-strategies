"""Intraday options replay engine — 0 DTE only, premium-range filtered.

Given (ticker, date, premium range, time window):
- Forces expiry == date (0 DTE only)
- Picks CALL and PUT whose entry premium (open of first bar in window) falls
  inside [premium_min, premium_max], among those the strike closest to ATM
  (spot at session start).
- Builds the minute-by-minute table for the whole window with PnL accumulated
  from the entry premium.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Optional

import pandas as pd

from adapter_polygon import PolygonAdapter
from downloader import Downloader

# Núcleo de estrategia COMPARTIDO con live_trader (raíz del repo). Lo agrego al path
# con append (prioridad baja) para no tapar módulos locales de options_replay.
import sys as _sys  # noqa: E402
from pathlib import Path as _PathRoot  # noqa: E402
_ROOT = str(_PathRoot(__file__).resolve().parent.parent)
if _ROOT not in _sys.path:
    _sys.path.append(_ROOT)
import strategy_core  # noqa: E402

DEFAULT_TIME_START = time(9, 30)
DEFAULT_TIME_END = time(16, 0)
MAX_STRIKES_TO_PROBE = 25  # per side; safety cap
# Opción 1 filtra el rango de prima por el ASK del NBBO (no por el open del bar). El ASK
# puede diferir del open, así que se ensancha el rango de fetch de quotes por este margen
# (en $ de prima) para no perder candidatos cuyo ASK caiga en rango aunque el open no.
_ASK_MARGIN = 0.5


@dataclass
class StrikeProbe:
    strike: float
    opening_premium: Optional[float]
    occ: str
    in_range: bool                      # dentro del Rango Óptimo (premium)
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread: Optional[float] = None
    volume: float = 0.0
    in_extended: bool = False           # dentro del Rango Extendido (Min-Max)
    itm_depth: float = 0.0              # >0 = ITM, <0 = OTM (CALL: spot-strike; PUT: strike-spot)
    right: str = ""
    spread_ok: bool = True             # pasó el filtro de spread


# --- Spread config (umbral máx por bucket de precio del subyacente) ---
import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_SPREAD_CONFIG_PATH = _Path(__file__).parent / "spread_config.json"


def _default_spread_config() -> dict:
    return {
        "enable_spread_filter": True,
        "buckets": [
            {"price_min": 0, "price_max": 100, "max_spread": 0.03},
            {"price_min": 100, "price_max": 300, "max_spread": 0.05},
            {"price_min": 300, "price_max": 600, "max_spread": 0.10},
            {"price_min": 600, "price_max": 1200, "max_spread": 0.25},
            {"price_min": 1200, "price_max": 1_000_000, "max_spread": 0.50},
        ],
    }


def load_spread_config() -> dict:
    if not _SPREAD_CONFIG_PATH.exists():
        return _default_spread_config()
    try:
        with _SPREAD_CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = _json.load(f)
        cfg.setdefault("enable_spread_filter", True)
        cfg.setdefault("buckets", _default_spread_config()["buckets"])
        return cfg
    except Exception:
        return _default_spread_config()


def _max_spread_for_price(spot: float, cfg: dict) -> float:
    """Máximo spread aceptable. Si la config trae '_max_spread_override' (lo setea la
    UI con 'Spread máximo ($)'), ese valor PLANO manda sobre los buckets. Si no, el
    bucket de precio donde cae el subyacente.

    Delega en strategy_core (núcleo compartido con live) → MISMA compuerta en backtest
    y en vivo. Comportamiento idéntico al previo (verificado en test_strategy_core.py)."""
    return strategy_core.max_spread_for_price(
        spot, buckets=cfg.get("buckets", []), override=cfg.get("_max_spread_override"))


# Opción 1 ("menor spread"): rango [min, max] de spread permitido según el PRECIO DEL
# CONTRATO (ASK). Los valores viven en strike_spread_config.json (editable desde la sección
# Configuración de la UI), NO hardcodeados. Rechaza tanto lo más ancho que el máx como lo más
# angosto que el mín. None = ASK fuera de los buckets → cae a la compuerta por precio.
# Default (fallback si el JSON falta/corrupto). Por ACCIÓN (= POR CONTRATO ÷100):
# ASK $25–300/contrato = $0.25–3.00/acción, etc.
_DEFAULT_ASK_SPREAD = (
    (0.25, 3.00, 0.01, 0.05),    # ASK $25–300/contrato   → spread $1–5  por contrato
    (3.00, 6.00, 0.06, 0.10),    # ASK $300–600/contrato  → spread $6–10
    (6.00, 12.00, 0.11, 0.25),   # ASK $600–1200/contrato → spread $11–25
)
_ASK_SPREAD_PATH = _Path(__file__).parent / "strike_spread_config.json"
_ask_cfg_cache: dict = {"mtime": None, "buckets": None}


def load_ask_spread_config() -> list:
    """Buckets (ask_min, ask_max, spread_min, spread_max) POR ACCIÓN, desde
    strike_spread_config.json. Sus campos ('ask_min'/'ask_max' y 'spread_*_contrato') están
    POR CONTRATO (×100) → se dividen ÷100 a por-acción. Cache por mtime: la UI edita el JSON y
    el siguiente backtest lo toma sin reiniciar. enabled=False → lista vacía (sin filtro; cae a
    la compuerta por precio). Compat: acepta 'strike_min'/'strike_max' de configs viejos."""
    try:
        mt = _ASK_SPREAD_PATH.stat().st_mtime
    except OSError:
        mt = None
    if _ask_cfg_cache["buckets"] is not None and _ask_cfg_cache["mtime"] == mt:
        return _ask_cfg_cache["buckets"]
    buckets = list(_DEFAULT_ASK_SPREAD)
    if mt is not None:
        try:
            with _ASK_SPREAD_PATH.open(encoding="utf-8") as f:
                data = _json.load(f)
            if not data.get("enabled", True):
                buckets = []
            else:
                buckets = [(float(b.get("ask_min", b.get("strike_min"))) / 100.0,
                            float(b.get("ask_max", b.get("strike_max"))) / 100.0,
                            float(b["spread_min_contrato"]) / 100.0,
                            float(b["spread_max_contrato"]) / 100.0)
                           for b in data.get("buckets", [])]
        except Exception:
            buckets = list(_DEFAULT_ASK_SPREAD)
    _ask_cfg_cache.update(mtime=mt, buckets=buckets)
    return buckets


def _ask_spread_range(ask: float):
    """(min, max) de spread por acción permitido para ese PRECIO DEL CONTRATO (ASK, por acción)
    según la config, o None si el ASK está fuera de los buckets → cae a la compuerta por precio."""
    if ask is None:
        return None
    for _lo, _hi, _smin, _smax in load_ask_spread_config():
        if _lo <= ask < _hi:             # medio-abierto [min, max) — spec: $25<=ASK<$300, etc.
            return (_smin, _smax)
    return None


def _quote_is_sane(q: dict, ref_premium: Optional[float] = None) -> bool:
    """¿El NBBO es usable, o es basura de la subasta de apertura (09:30:00)? Rechaza
    quote vacío, invertido/cruzado (bid≥ask), sin oferta (ask≤0) o cuyo mid se aleja
    >2x del precio de REFERENCIA (el open del bar = un trade real). El snapshot del
    primer segundo suele venir bid/ask ~10x fuera de rango (ej. 16.80/20.06 en una
    opción que vale 1.80)."""
    bid, ask = q.get("bid"), q.get("ask")
    if bid is None or ask is None or ask <= 0 or bid > ask:
        return False
    if ref_premium and ref_premium > 0:
        mid = (bid + ask) / 2.0
        if mid < 0.5 * ref_premium or mid > 2.0 * ref_premium:
            return False
    return True


def _robust_quote(downloader, occ: str, date: str, ts: pd.Timestamp,
                  ref_premium: Optional[float] = None, max_step_min: int = 2) -> dict:
    """NBBO de entrada robusto al quote-basura de las 09:30:00 (subasta de apertura).

    Pide el quote en `ts`; si NO es sano (_quote_is_sane), reintenta en ts+1min y
    ts+2min y devuelve el PRIMERO sano. Si ninguno lo es, devuelve el último (degrada
    al comportamiento previo). `ref_premium` = open del bar de entrada. Verificado: a
    las 09:30:00 los spreads son 3–4$ y a las 09:31 colapsan a 1–4¢."""
    last = {"bid": None, "ask": None, "spread": None}
    for step in range(max_step_min + 1):
        qts = ts if step == 0 else ts + pd.Timedelta(minutes=step)
        try:
            q = downloader.option_quote(occ, date, qts)
        except Exception:
            q = {"bid": None, "ask": None, "spread": None}
        last = q
        if _quote_is_sane(q, ref_premium):
            return q
    return last


@dataclass
class IterationResult:
    iteration: int
    invest_call: float
    invest_put: float
    premium_min: float
    premium_max: float
    exit_threshold_pct: float
    exit_metric: str   # "total" | "call" | "put"
    stop_loss_pct: float
    start_dt: pd.Timestamp
    end_dt: pd.Timestamp
    exit_reason: str   # "100%_threshold" | "stop_loss" | "session_end"
    spot_at_start: float
    call_strike: float
    put_strike: float
    call_occ: str
    put_occ: str
    call_entry_premium: float
    put_entry_premium: float
    call_exit_premium: float
    put_exit_premium: float
    initial_total: float
    final_total: float
    max_total: float
    max_total_dt: pd.Timestamp
    min_total: float
    min_total_dt: pd.Timestamp
    call_probes: list = field(default_factory=list, repr=False)
    put_probes: list = field(default_factory=list, repr=False)
    df: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)
    mode: str = "both"   # "both" | "call_only" | "put_only"
    call_fallback: bool = False   # True si se eligió por cercanía (sin match en rango)
    put_fallback: bool = False    # True si se eligió por cercanía (sin match en rango)
    # --- Metadata de spread/NBBO de la pierna seleccionada ---
    call_bid: Optional[float] = None
    call_ask: Optional[float] = None
    call_spread: Optional[float] = None
    call_range_tier: str = ""     # "optimo" | "extended" | "fallback"
    put_bid: Optional[float] = None
    put_ask: Optional[float] = None
    put_spread: Optional[float] = None
    put_range_tier: str = ""
    # --- Salidas INDEPENDIENTES por pierna (modo "call_or_put") ---
    # Índice de fila (en `df`) donde cada pierna se vendió, y por qué.
    call_exit_idx: Optional[int] = None
    put_exit_idx: Optional[int] = None
    call_exit_reason: str = ""   # "100%_threshold" | "stop_loss" | "session_end"
    put_exit_reason: str = ""
    # --- Modo "both_refuerzo" (martingala POR PIERNA). Si está seteado, gain_total/
    #     invest_total se leen de acá (capital y ganancia MULTI-TRANCHE). Claves:
    #     {"gain","invest","n","idxs","events","n_call","n_put"} donde events =
    #     [{"idx","leg":'CALL'|'PUT',"price"}, ...] (un evento por refuerzo, mismo tipo).
    refuerzo: Optional[dict] = None

    @property
    def gain_call(self) -> float:
        if not self.call_entry_premium:
            return 0.0
        return self.invest_call * ((self.call_exit_premium / self.call_entry_premium) - 1.0)

    @property
    def gain_put(self) -> float:
        if not self.put_entry_premium:
            return 0.0
        return self.invest_put * ((self.put_exit_premium / self.put_entry_premium) - 1.0)

    @property
    def gain_total(self) -> float:
        if self.refuerzo is not None:
            return float(self.refuerzo.get("gain", 0.0))
        return self.gain_call + self.gain_put

    @property
    def invest_total(self) -> float:
        if self.refuerzo is not None:
            return float(self.refuerzo.get("invest", self.invest_call + self.invest_put))
        return self.invest_call + self.invest_put

    @property
    def pnl_pct_combined(self) -> float:
        """% combined gain at exit (Call% + Put%)."""
        pct_c = ((self.call_exit_premium / self.call_entry_premium) - 1.0) if self.call_entry_premium else 0.0
        pct_p = ((self.put_exit_premium / self.put_entry_premium) - 1.0) if self.put_entry_premium else 0.0
        return pct_c + pct_p


@dataclass
class MultiIterationResult:
    ticker: str
    date: str
    expiry: str
    premium_min: float
    premium_max: float
    time_start: time
    time_end: time
    invest_call_per_iter: float
    invest_put_per_iter: float
    exit_threshold_pct: float
    iterations: list = field(default_factory=list)

    @property
    def n_iterations(self) -> int:
        return len(self.iterations)

    @property
    def total_invested(self) -> float:
        return sum(i.invest_total for i in self.iterations)

    @property
    def total_gain(self) -> float:
        return sum(i.gain_total for i in self.iterations)

    @property
    def final_capital(self) -> float:
        return self.total_invested + self.total_gain

    @property
    def roi_pct(self) -> float:
        return self.total_gain / self.total_invested if self.total_invested else 0.0


@dataclass
class ReplayResult:
    ticker: str
    date: str
    expiry: str
    premium_min: float
    premium_max: float
    time_start: time
    time_end: time
    spot_at_start: float
    call_strike: float
    put_strike: float
    call_occ: str
    put_occ: str
    call_entry_premium: float
    put_entry_premium: float
    initial_total: float
    final_total: float
    max_total: float
    max_total_dt: pd.Timestamp
    min_total: float
    min_total_dt: pd.Timestamp
    call_probes: list[StrikeProbe] = field(default_factory=list, repr=False)
    put_probes: list[StrikeProbe] = field(default_factory=list, repr=False)
    df: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)

    @property
    def pnl_final_abs(self) -> float:
        return self.final_total - self.initial_total

    @property
    def pnl_final_pct(self) -> float:
        return self.pnl_final_abs / self.initial_total if self.initial_total else 0.0

    @property
    def pnl_max_abs(self) -> float:
        return self.max_total - self.initial_total

    @property
    def pnl_max_pct(self) -> float:
        return self.pnl_max_abs / self.initial_total if self.initial_total else 0.0

    @property
    def pnl_min_abs(self) -> float:
        return self.min_total - self.initial_total

    @property
    def pnl_min_pct(self) -> float:
        return self.pnl_min_abs / self.initial_total if self.initial_total else 0.0


class NoMatchError(ValueError):
    """Ningún strike dentro del rango Óptimo/Extendido pasó la compuerta de spread.

    Sin fallback: si esto ocurre, NO se compra ese leg en ese día."""

    def __init__(self, side: str, probes: list[StrikeProbe], premium_min: float, premium_max: float):
        self.side = side
        self.probes = probes
        self.premium_min = premium_min
        self.premium_max = premium_max
        info = []
        for p in probes[:8]:
            if p.opening_premium is None:
                info.append(f"strike={p.strike:.2f} open=N/A")
            else:
                _sp = f"{p.spread:.2f}" if p.spread is not None else "N/A"
                info.append(f"strike={p.strike:.2f} open={p.opening_premium:.2f} spread={_sp}")
        super().__init__(
            f"No hay contrato {side} 0 DTE con premium en [{premium_min:.2f}, "
            f"{premium_max:.2f}] USD (óptimo/extendido) que pase la compuerta de "
            f"spread. Sin fallback → no se compra. {len(probes)} strikes probados; "
            f"muestra (USD): {'; '.join(info)}"
        )


def _probe_premium_range(
    downloader: Downloader,
    ticker: str,
    date: str,
    candidates: pd.DataFrame,
    right: str,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    premium_min: float,
    premium_max: float,
    max_probe: int,
    ext_min: Optional[float] = None,
    ext_max: Optional[float] = None,
    spot: Optional[float] = None,
    spread_cfg: Optional[dict] = None,
    selection_criterion: str = "itm",   # "itm" (cercano a ITM) | "spread" (menor spread) | "value" (prima ≈ value_target, IGNORA spread) | "itm_first" (1-ITM más cercano, IGNORA spread y rango)
    value_target: float = 2.0,          # objetivo de prima ($) para selection_criterion="value"
) -> tuple[Optional[StrikeProbe], list[StrikeProbe], str]:
    """Selector de contrato.

    Opción 1 (selection_criterion="spread") — el contrato más líquido (menor spread)
    dentro del rango de prima, en 4 pasos:
    1-2. CASCADA por prima (filtrando por el ASK del NBBO): los que caen en el Rango
         Óptimo; si NINGUNO, los del Rango Extendido; si ninguno tampoco → no se compra.
    3.   FILTRO DE SPREAD por PRECIO DEL CONTRATO (ASK) sobre ESE set: se conservan los que
         tienen spread (ask-bid) DENTRO del rango [mín,máx] del bucket de su ASK (_ask_spread_range).
         Un 'Spread máx' explícito (override) reemplaza el rango por un techo. Si ninguno
         cumple → no se compra (NO se cruza al otro tier).
    4.   SELECCIÓN: el de MENOR spread; empate → MAYOR ask; empate → el primero (orden por
         cercanía a ATM). tier="optimo"/"extended".
    Otros criterios: "itm_first" (Opción 2 = el 1-ITM, ignora spread y rango) · "value"
    (prima ≈ value_target) · "itm" (legado, cascada previa).
    Devuelve (probe_elegido, todos_los_probes, tier). tier="" si no hubo compra.

    Retro-compat: ext_min/ext_max default al rango óptimo; si spread_cfg es None o el
    filtro está deshabilitado, se omite el filtro de spread.
    """
    if ext_min is None:
        ext_min = premium_min
    if ext_max is None:
        ext_max = premium_max
    if spread_cfg is None:
        spread_cfg = load_spread_config()

    spread_enabled = bool(spread_cfg.get("enable_spread_filter", True))
    max_spread = _max_spread_for_price(spot, spread_cfg) if spot is not None else float("inf")
    # Opción 1 = criterio "spread": filtra el rango de prima por el ASK del NBBO (no por el
    # open del bar) y aplica la compuerta de spread como RANGO [mín,máx] por bucket de PRECIO
    # DEL CONTRATO (ASK) (_ask_spread_range). Un 'Spread máx'/ceiling explícito (override)
    # reemplaza el rango por un techo. Los demás criterios mantienen el flujo previo.
    _opt1 = (selection_criterion == "spread")
    _override = spread_cfg.get("_max_spread_override")
    # Rango de fetch de quotes = unión de óptimo y extendido. En Opción 1 se ensancha
    # ±_ASK_MARGIN porque el filtro es por ASK y el ASK puede diferir del open del bar.
    fetch_lo = min(premium_min, ext_min)
    fetch_hi = max(premium_max, ext_max)

    probes: list[StrikeProbe] = []
    for i, (_, row) in enumerate(candidates.iterrows()):
        if i >= max_probe:
            break
        strike = float(row["strike_price"])
        # Usar el ticker real del chain — índices tienen OCC root distinto.
        occ = row.get("ticker") or PolygonAdapter.build_occ(ticker, date, right, strike)
        bars = downloader.option(occ, date)
        bars_in = bars[(bars["timestamp"] >= start_ts) & (bars["timestamp"] <= end_ts)] if not bars.empty else bars
        if bars_in.empty:
            probes.append(StrikeProbe(strike=strike, opening_premium=None, occ=occ,
                                      in_range=False, right=right))
            continue
        opening = float(bars_in.iloc[0]["open"])
        volume = float(bars_in.iloc[0].get("volume", 0) or 0)
        itm_depth = (spot - strike) if (spot is not None and right == "C") else \
                    ((strike - spot) if (spot is not None and right == "P") else 0.0)

        bid = ask = spread = None
        spread_ok = True
        # NBBO: en Opción 1 (filtro on) cotizamos para FILTRAR POR ASK y medir el spread
        # (rango ensanchado ±_ASK_MARGIN, porque el ASK puede diferir del open). En los demás
        # criterios, solo si el open cae en el rango de fetch (comportamiento previo).
        _want_q = ((_opt1 and spread_enabled
                    and (fetch_lo - _ASK_MARGIN) <= opening <= (fetch_hi + _ASK_MARGIN))
                   or ((not _opt1) and spread_enabled and fetch_lo <= opening <= fetch_hi))
        if _want_q:
            q = _robust_quote(downloader, occ, date, start_ts, ref_premium=opening)
            bid, ask, spread = q.get("bid"), q.get("ask"), q.get("spread")

        # Paso 2 — rango de prima: Opción 1 filtra por el ASK; el resto, por el open del bar.
        if _opt1 and spread_enabled:
            in_opt = (ask is not None) and (premium_min <= ask <= premium_max)
            in_ext = (ask is not None) and (ext_min <= ask <= ext_max)
        else:
            in_opt = premium_min <= opening <= premium_max
            in_ext = ext_min <= opening <= ext_max

        # Paso 3 — compuerta de spread: Opción 1 = RANGO [mín,máx] por bucket de PRECIO DEL
        # CONTRATO (ASK) (o el override = techo si el usuario lo fijó). Los demás criterios usan
        # el máximo por precio del subyacente.
        if spread_enabled and spread is not None:
            if _opt1:
                if _override is not None:
                    spread_ok = spread <= float(_override)   # override = techo explícito (solo máx)
                else:
                    _r = _ask_spread_range(ask)
                    # RANGO por ASK: rechaza spread MÁS CHICO que el mín y MÁS ANCHO que el máx.
                    # round(...,2): el spread está en centavos; sin esto el ruido float (0.00999…
                    # en vez de 0.01) rechaza un contrato que está JUSTO en el borde del bucket.
                    _rs = round(spread, 2)
                    spread_ok = (_r[0] <= _rs <= _r[1]) if _r is not None else (_rs <= max_spread)
            else:
                spread_ok = spread <= max_spread

        probes.append(StrikeProbe(
            strike=strike, opening_premium=opening, occ=occ,
            in_range=in_opt, bid=bid, ask=ask, spread=spread, volume=volume,
            in_extended=in_ext, itm_depth=itm_depth, right=right, spread_ok=spread_ok,
        ))

    valid = [p for p in probes if p.opening_premium is not None]
    if not valid:
        return None, probes, ""

    def _itm_rank(p: StrikeProbe):
        # ITM: menor profundidad = "1-ITM"; OTM: después de todos los ITM.
        return p.itm_depth if p.itm_depth >= 0 else abs(p.itm_depth) + 1e6

    def _sp(p: StrikeProbe):
        return round(p.spread, 2) if p.spread is not None else 9.99

    def _select(cands: list[StrikeProbe]) -> StrikeProbe:
        # Acá se decide CUÁL contrato se elige entre los candidatos:
        #   "spread"    (opción 1): el de MENOR spread (desempate: MAYOR ASK, luego el primero).
        #   "value"     (opción 2 vieja): IGNORA el spread; el de prima de entrada MÁS
        #               CERCANA a value_target ($2 default), igual CALL y PUT. Desempate: ITM.
        #   "itm_first" (opción 2): IGNORA el spread; el MÁS CERCANO a ITM (menor itm_depth
        #               ≥ 0 = el "1-ITM", primer strike dentro del dinero). Único criterio.
        #   "itm"       (legado): de los 2 de menor spread, el más cercano a ITM.
        if selection_criterion == "spread":
            # Paso 4 (tu spec): menor spread → desempate por MAYOR ask → el primero de la lista
            # (min es estable → respeta el orden por cercanía a ATM). Sin desempate por ITM.
            return min(cands, key=lambda p: (_sp(p), -(p.ask if p.ask is not None else 0.0)))
        if selection_criterion == "value":
            return min(cands, key=lambda p: (round(abs((p.opening_premium or 0.0) - value_target), 4),
                                             _itm_rank(p)))
        if selection_criterion == "itm_first":
            return min(cands, key=_itm_rank)
        top2 = sorted(cands, key=lambda p: (_sp(p), _itm_rank(p)))[:2]
        return min(top2, key=lambda p: (_itm_rank(p), _sp(p)))

    def _passes_spread(p: StrikeProbe) -> bool:
        # "value" e "itm_first" (opción 2) NO consideran el spread → la compuerta no aplica.
        if selection_criterion in ("value", "itm_first"):
            return True
        if not spread_enabled:
            return True
        return p.spread_ok

    # Opción 2 ("itm_first") y la vieja "value": IGNORAN el rango óptimo/extendido Y el
    # spread. Eligen entre TODOS los strikes con prima válida según el criterio:
    #   · "value"     → prima de entrada más cercana a value_target (desempate por ITM).
    #   · "itm_first" → el más cercano a ITM (1-ITM); la cercanía a ITM es el ÚNICO criterio.
    # El rango óptimo/extendido y la compuerta de spread NO aplican a estos criterios.
    if selection_criterion in ("value", "itm_first"):
        pick = _select(valid)
        # Traer el NBBO del contrato ELEGIDO solo para el display (su prima puede caer
        # fuera del rango de fetch, así que aún no se pidió). NO afecta la selección.
        if spread_enabled and pick.spread is None and pick.occ:
            q = _robust_quote(downloader, pick.occ, date, start_ts, ref_premium=pick.opening_premium)
            pick.bid, pick.ask, pick.spread = q.get("bid"), q.get("ask"), q.get("spread")
        return pick, probes, selection_criterion

    # Opción 1 (criterio "spread") — tu spec de 4 pasos:
    #   Paso 1-2: primer tier de prima NO vacío por ASK (Óptimo; si vacío, Extendido).
    #   Paso 3:   sobre ESE set, filtro de spread por strike (rango mín-máx). NO se cruza al
    #             otro tier: si el tier elegido queda sin nadie tras el spread → no se compra.
    #   Paso 4:   _select = menor spread → menor ASK → el primero de la lista.
    if _opt1:
        opt_set = [p for p in valid if p.in_range]        # ASK en Rango Óptimo
        ext_set = [p for p in valid if p.in_extended]     # ASK en Rango Extendido
        if opt_set:
            base, tier = opt_set, "optimo"
        elif ext_set:
            base, tier = ext_set, "extended"
        else:
            return None, probes, ""
        passed = [p for p in base if _passes_spread(p)]   # Paso 3: rango de spread por strike
        if not passed:
            return None, probes, ""
        return _select(passed), probes, tier

    # Legacy "itm": cascada previa (Óptimo+spread, luego Extendido+spread). SIN fallback.
    cand_opt = [p for p in valid if p.in_range and _passes_spread(p)]
    if cand_opt:
        return _select(cand_opt), probes, "optimo"
    cand_ext = [p for p in valid if p.in_extended and _passes_spread(p)]
    if cand_ext:
        return _select(cand_ext), probes, "extended"
    return None, probes, ""


def replay_session(
    downloader: Downloader,
    ticker: str,
    date: str,
    premium_min: float,
    premium_max: float,
    time_start: time = DEFAULT_TIME_START,
    time_end: time = DEFAULT_TIME_END,
    max_strikes_to_probe: int = MAX_STRIKES_TO_PROBE,
) -> ReplayResult:
    if premium_min >= premium_max:
        raise ValueError(f"premium_min ({premium_min}) must be < premium_max ({premium_max})")
    if premium_min < 0:
        raise ValueError("premium_min cannot be negative")

    ticker = ticker.upper().strip()
    tz = "America/New_York"
    start_ts = pd.Timestamp.combine(pd.Timestamp(date).date(), time_start).tz_localize(tz)
    end_ts = pd.Timestamp.combine(pd.Timestamp(date).date(), time_end).tz_localize(tz)

    # Underlying
    under = downloader.underlying(ticker, date)
    if under.empty:
        raise ValueError(f"No underlying data for {ticker} on {date} (market closed?)")
    under = under[(under["timestamp"] >= start_ts) & (under["timestamp"] <= end_ts)].reset_index(drop=True)
    if under.empty:
        raise ValueError(f"No bars inside {time_start}-{time_end} for {ticker} on {date}")
    spot_at_start = float(under.iloc[0]["open"])

    # 0 DTE expiry must equal date
    nearest = downloader.nearest_expiry(ticker, date)
    if nearest is None:
        raise ValueError(f"No option expirations available for {ticker} on/after {date}")
    if nearest != date:
        raise ValueError(
            f"No 0 DTE option for {ticker} on {date}. Nearest available expiry is {nearest}. "
            f"This ticker likely does not have daily expirations on this date."
        )

    chain = downloader.chain(ticker, date)
    if chain.empty:
        raise ValueError(f"Empty 0 DTE chain for {ticker} on {date}")

    calls = chain[chain["contract_type"].str.lower() == "call"].copy()
    puts = chain[chain["contract_type"].str.lower() == "put"].copy()
    if calls.empty or puts.empty:
        raise ValueError(f"Chain missing CALL or PUT side for {ticker} on {date}")

    # Order by distance to ATM (closest first)
    calls = calls.assign(_d=(calls["strike_price"] - spot_at_start).abs()).sort_values(
        ["_d", "strike_price"]
    )
    puts = puts.assign(_d=(puts["strike_price"] - spot_at_start).abs()).sort_values(
        ["_d", "strike_price"]
    )

    call_pick, call_probes, _call_fb = _probe_premium_range(
        downloader, ticker, date, calls, "C", start_ts, end_ts,
        premium_min, premium_max, max_strikes_to_probe,
    )
    if call_pick is None:
        raise NoMatchError("CALL", call_probes, premium_min, premium_max)

    put_pick, put_probes, _put_fb = _probe_premium_range(
        downloader, ticker, date, puts, "P", start_ts, end_ts,
        premium_min, premium_max, max_strikes_to_probe,
    )
    if put_pick is None:
        raise NoMatchError("PUT", put_probes, premium_min, premium_max)

    df_c = downloader.option(call_pick.occ, date)
    df_p = downloader.option(put_pick.occ, date)

    merged = _merge_minute(under, df_c, df_p, start_ts, end_ts)
    if merged.empty:
        raise ValueError("No overlapping minute bars between underlying, CALL and PUT")

    merged["call_strike"] = call_pick.strike
    merged["put_strike"] = put_pick.strike
    initial_total = float(call_pick.opening_premium + put_pick.opening_premium)
    merged["pnl_acum"] = merged["total"] - initial_total

    idx_max = int(merged["total"].idxmax())
    idx_min = int(merged["total"].idxmin())

    return ReplayResult(
        ticker=ticker,
        date=date,
        expiry=date,
        premium_min=premium_min,
        premium_max=premium_max,
        time_start=time_start,
        time_end=time_end,
        spot_at_start=spot_at_start,
        call_strike=call_pick.strike,
        put_strike=put_pick.strike,
        call_occ=call_pick.occ,
        put_occ=put_pick.occ,
        call_entry_premium=float(call_pick.opening_premium),
        put_entry_premium=float(put_pick.opening_premium),
        initial_total=initial_total,
        final_total=float(merged.iloc[-1]["total"]),
        max_total=float(merged.loc[idx_max, "total"]),
        max_total_dt=merged.loc[idx_max, "timestamp"],
        min_total=float(merged.loc[idx_min, "total"]),
        min_total_dt=merged.loc[idx_min, "timestamp"],
        call_probes=call_probes,
        put_probes=put_probes,
        df=merged,
    )


def replay_session_loop(
    downloader: Downloader,
    ticker: str,
    date: str,
    premium_min: float,
    premium_max: float,
    invest_call: float,
    invest_put: float,
    time_start: time = DEFAULT_TIME_START,
    time_end: time = DEFAULT_TIME_END,
    exit_threshold_pct: float = 1.0,
    max_iterations: int = 30,
    max_strikes_to_probe: int = MAX_STRIKES_TO_PROBE,
    check_step_min: int = 1,
) -> MultiIterationResult:
    """Run an iterating replay: when combined % exceeds `exit_threshold_pct`,
    exit the position, re-enter the same (invest_call, invest_put) into a new
    CALL+PUT 0 DTE pair whose opening premium at that minute is in range and
    is closest to ATM. Repeat until session end or no matching contracts."""
    if premium_min >= premium_max:
        raise ValueError(f"premium_min ({premium_min}) must be < premium_max ({premium_max})")
    if premium_min < 0:
        raise ValueError("premium_min cannot be negative")

    ticker = ticker.upper().strip()
    tz = "America/New_York"
    day_start_ts = pd.Timestamp.combine(pd.Timestamp(date).date(), time_start).tz_localize(tz)
    day_end_ts = pd.Timestamp.combine(pd.Timestamp(date).date(), time_end).tz_localize(tz)

    under_full = downloader.underlying(ticker, date)
    if under_full.empty:
        raise ValueError(f"No underlying data for {ticker} on {date} (market closed?)")

    nearest = downloader.nearest_expiry(ticker, date)
    if nearest is None:
        raise ValueError(f"No option expirations available for {ticker} on/after {date}")
    if nearest != date:
        raise ValueError(
            f"No 0 DTE option for {ticker} on {date}. Nearest expiry is {nearest}. "
            f"This ticker likely does not have daily expirations on this date."
        )

    chain = downloader.chain(ticker, date)
    if chain.empty:
        raise ValueError(f"Empty 0 DTE chain for {ticker} on {date}")

    calls_chain = chain[chain["contract_type"].str.lower() == "call"].copy()
    puts_chain = chain[chain["contract_type"].str.lower() == "put"].copy()
    if calls_chain.empty or puts_chain.empty:
        raise ValueError(f"Chain missing CALL or PUT side for {ticker} on {date}")

    iterations: list[IterationResult] = []
    current_start_ts = day_start_ts

    while current_start_ts < day_end_ts and len(iterations) < max_iterations:
        try:
            it = _run_one_iteration(
                downloader=downloader,
                ticker=ticker,
                date=date,
                under_full=under_full,
                calls_chain=calls_chain,
                puts_chain=puts_chain,
                premium_min=premium_min,
                premium_max=premium_max,
                invest_call=invest_call,
                invest_put=invest_put,
                start_ts=current_start_ts,
                end_ts=day_end_ts,
                exit_threshold_pct=exit_threshold_pct,
                iteration_idx=len(iterations) + 1,
                max_strikes_to_probe=max_strikes_to_probe,
                check_step_min=check_step_min,
            )
        except (NoMatchError, ValueError):
            break

        iterations.append(it)

        if it.exit_reason != "100%_threshold":
            break
        current_start_ts = it.end_dt + pd.Timedelta(minutes=1)

    return MultiIterationResult(
        ticker=ticker,
        date=date,
        expiry=date,
        premium_min=premium_min,
        premium_max=premium_max,
        time_start=time_start,
        time_end=time_end,
        invest_call_per_iter=invest_call,
        invest_put_per_iter=invest_put,
        exit_threshold_pct=exit_threshold_pct,
        iterations=iterations,
    )


def validate_0dte_session(downloader: Downloader, ticker: str, date: str) -> str:
    """Verify 0 DTE exists for ticker/date. Returns expiry (== date) or raises."""
    ticker = ticker.upper().strip()
    # Fast path: si la chain ya está cacheada en Parquet, 0 DTE existe seguro.
    # Evitamos una llamada innecesaria a la API de Polygon.
    chain_path = downloader.data_dir / "chain" / f"{ticker}_{date}.parquet"
    if chain_path.exists():
        return date
    nearest = downloader.nearest_expiry(ticker, date)
    if nearest is None:
        raise ValueError(f"No option expirations available for {ticker} on/after {date}")
    if nearest != date:
        raise ValueError(
            f"No 0 DTE option for {ticker} on {date}. Nearest expiry is {nearest}. "
            f"This ticker likely does not have daily expirations on this date."
        )
    return nearest


def _simulate_refuerzo(call_px, put_px, call_entry, put_entry, invest_call, invest_put,
                       profit_target, loss_thr, max_refuerzos=2, stop_thr=-1.0,
                       call_buy_px=None, put_buy_px=None):
    """Martingala POR PIERNA del MISMO tipo. Abre CALL (invest_call) y PUT (invest_put).
    Disparador POR PIERNA: cada pierna mira SU PROPIO ROI. Cuando una (o ambas) cae a
    <= -loss_thr, se refuerza la pierna que MÁS pierde (ROI más negativo) comprando otro
    tranche de ESA misma pierna (mismo tipo, al precio del minuto, invirtiendo de nuevo su
    capital inicial). NUNCA compra la pierna contraria. El tope `max_refuerzos` es TOTAL
    (cuenta ambas piernas juntas).
    Salidas (chequeadas ANTES de reforzar, sobre el ROI TOTAL de ambas piernas): ROI >=
    profit_target ('100%_threshold'); ROI <= stop_thr ('stop_loss' — stop sobre el TOTAL, NO
    por pierna; default -1.0 = -100% ≈ sin stop); o al cierre del día ('session_end').
    Devuelve (exit_idx, exit_reason, roi[], value[], invested[], events[]), donde
    events = [{'idx': t, 'leg': 'CALL'|'PUT', 'price': px}, ...] (un evento por refuerzo)."""
    import numpy as np
    cpx = np.asarray(call_px, dtype=float)   # MARK por minuto (bid si Fase 2) → valuación/ROI
    ppx = np.asarray(put_px, dtype=float)
    # Precio de COMPRA de cada refuerzo: el ASK por minuto si se pasa (Fase 2: pagás la
    # oferta al agregar tranches), si no el propio mark (comportamiento previo).
    cbuy = np.asarray(call_buy_px if call_buy_px is not None else call_px, dtype=float)
    pbuy = np.asarray(put_buy_px if put_buy_px is not None else put_px, dtype=float)
    n = len(cpx)
    call_tr = [float(call_entry)]            # precios de entrada de cada tranche CALL
    put_tr = [float(put_entry)]              # idem PUT (cada pierna lleva su propia lista)
    roi = np.zeros(n); value = np.zeros(n); invested = np.zeros(n)
    events: list[dict] = []
    exit_idx, exit_reason = n - 1, "session_end"

    def _leg(px, tranches, unit):
        """(valor, invertido) de una pierna: cada tranche vale unit*(px/entry)."""
        v = sum((unit * (px / e)) if e > 0 else 0.0 for e in tranches)
        return v, len(tranches) * unit

    for t in range(n):
        c, p = cpx[t], ppx[t]
        cv, ci = _leg(c, call_tr, invest_call)
        pv, pi = _leg(p, put_tr, invest_put)
        v, inv = cv + pv, ci + pi
        r = (v - inv) / inv if inv > 0 else 0.0
        if r >= profit_target:                               # Umbral de ROI TOTAL → salir (gana)
            roi[t], value[t], invested[t] = r, v, inv
            exit_idx, exit_reason = t, "100%_threshold"
            break
        if r <= stop_thr:                                    # Stop loss sobre el ROI TOTAL (no por
            roi[t], value[t], invested[t] = r, v, inv        # pierna) → cortar ANTES de reforzar
            exit_idx, exit_reason = t, "stop_loss"
            break
        # REFUERZO POR PIERNA: cada pierna mira SU PROPIO ROI. Entre las que cayeron a
        # <= -loss_thr (activas y comprables), se refuerza la que MÁS pierde (ROI más
        # negativo) comprando más del MISMO tipo. Nunca la contraria. El tope `max_refuerzos`
        # es TOTAL: cuenta los refuerzos de AMBAS piernas juntas (no por pierna).
        croi = (cv - ci) / ci if ci > 0 else 0.0
        proi = (pv - pi) / pi if pi > 0 else 0.0
        cand = []
        if invest_call > 0 and croi <= -loss_thr and c > 0.01:
            cand.append(("CALL", croi, float(cbuy[t])))      # se PAGA el ask (cbuy)
        if invest_put > 0 and proi <= -loss_thr and p > 0.01:
            cand.append(("PUT", proi, float(pbuy[t])))
        if cand and len(events) < max_refuerzos:             # tope TOTAL (ambas piernas juntas)
            leg, _, px = min(cand, key=lambda x: x[1])       # la pierna que MÁS pierde
            (call_tr if leg == "CALL" else put_tr).append(px)
            events.append({"idx": t, "leg": leg, "price": px})
            cv, ci = _leg(c, call_tr, invest_call)           # recomputar con el tranche nuevo
            pv, pi = _leg(p, put_tr, invest_put)
            v, inv = cv + pv, ci + pi
            r = (v - inv) / inv if inv > 0 else 0.0
        roi[t], value[t], invested[t] = r, v, inv

    return (exit_idx, exit_reason, roi[: exit_idx + 1], value[: exit_idx + 1],
            invested[: exit_idx + 1], events)


def run_next_iteration(
    downloader: Downloader,
    ticker: str,
    date: str,
    premium_min: float,
    premium_max: float,
    invest_call: float,
    invest_put: float,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    exit_threshold_pct: float = 1.0,
    exit_metric: str = "total",
    stop_loss_pct: float = -1.0,
    iteration_idx: int = 1,
    max_strikes_to_probe: int = MAX_STRIKES_TO_PROBE,
    mode: str = "both",   # "both" | "call_only" | "put_only" | "call_or_put"
    ext_min: Optional[float] = None,
    ext_max: Optional[float] = None,
    check_step_min: int = 1,
    call_exit_threshold_pct: float = 0.10,
    call_stop_loss_pct: float = -1.0,
    put_exit_threshold_pct: float = 0.10,
    put_stop_loss_pct: float = -1.0,
    exit_plus_threshold_pct: float = 0.50,
    exit_plus_time: Optional[time] = None,
    refuerzo_loss_threshold_pct: float = 0.50,
    refuerzo_max_count: int = 2,
    selection_criterion: str = "itm",
    value_target: float = 2.0,
    dte: int = 0,
    overnight_exit_time: Optional[time] = None,
    spread_cfg: Optional[dict] = None,
    entry_at_ask: bool = False,
    exit_at_bid: bool = False,
    nbbo_timeline: bool = False,
) -> IterationResult:
    """Run a single iteration starting at `start_ts`. Public wrapper that loads
    underlying + chain from the downloader cache and then invokes the iteration
    engine. Use for manual mode (one iteration per UI click)."""
    if premium_min >= premium_max:
        raise ValueError(f"premium_min ({premium_min}) must be < premium_max ({premium_max})")
    if premium_min < 0:
        raise ValueError("premium_min cannot be negative")

    ticker = ticker.upper().strip()

    # DTE=1 (overnight): despacha al flujo que compra un contrato venciendo el día
    # hábil siguiente (1 DTE) en buy_date a la Horario de entrada y lo vende ese día
    # siguiente a la Horario de salida. Como son días distintos, la salida puede ser
    # una hora anterior a la entrada. Misma selección de contrato (selection_criterion);
    # sin Umbral/Stop (la venta del día siguiente es el único evento de salida).
    if int(dte) == 1:
        return run_overnight_1dte(
            downloader, ticker, date, premium_min, premium_max,
            invest_call, invest_put, start_ts,
            iteration_idx=iteration_idx, max_strikes_to_probe=max_strikes_to_probe,
            mode=mode, ext_min=ext_min, ext_max=ext_max,
            selection_criterion=selection_criterion, value_target=value_target,
            spread_cfg=spread_cfg, exit_time=overnight_exit_time, entry_at_ask=entry_at_ask,
            exit_at_bid=exit_at_bid,
        )

    under_full = downloader.underlying(ticker, date)
    if under_full.empty:
        raise ValueError(f"No underlying data for {ticker} on {date}")

    chain = downloader.chain(ticker, date)
    if chain.empty:
        raise ValueError(f"Empty 0 DTE chain for {ticker} on {date}")

    calls_chain = chain[chain["contract_type"].str.lower() == "call"].copy()
    puts_chain = chain[chain["contract_type"].str.lower() == "put"].copy()
    if calls_chain.empty or puts_chain.empty:
        raise ValueError(f"Chain missing CALL or PUT side for {ticker} on {date}")

    return _run_one_iteration(
        downloader=downloader,
        ticker=ticker,
        date=date,
        under_full=under_full,
        calls_chain=calls_chain,
        puts_chain=puts_chain,
        premium_min=premium_min,
        premium_max=premium_max,
        invest_call=invest_call,
        invest_put=invest_put,
        start_ts=start_ts,
        end_ts=end_ts,
        exit_threshold_pct=exit_threshold_pct,
        exit_metric=exit_metric,
        stop_loss_pct=stop_loss_pct,
        iteration_idx=iteration_idx,
        max_strikes_to_probe=max_strikes_to_probe,
        mode=mode,
        ext_min=ext_min,
        ext_max=ext_max,
        check_step_min=check_step_min,
        call_exit_threshold_pct=call_exit_threshold_pct,
        call_stop_loss_pct=call_stop_loss_pct,
        put_exit_threshold_pct=put_exit_threshold_pct,
        put_stop_loss_pct=put_stop_loss_pct,
        exit_plus_threshold_pct=exit_plus_threshold_pct,
        exit_plus_time=exit_plus_time,
        refuerzo_loss_threshold_pct=refuerzo_loss_threshold_pct,
        refuerzo_max_count=refuerzo_max_count,
        selection_criterion=selection_criterion,
        value_target=value_target,
        spread_cfg=spread_cfg,
        entry_at_ask=entry_at_ask,
        exit_at_bid=exit_at_bid,
        nbbo_timeline=nbbo_timeline,
    )


def next_trading_day(downloader: Downloader, ticker: str, date: str,
                     max_lookahead: int = 6) -> Optional[str]:
    """Primer día hábil posterior a `date` con barras de subyacente (salta findes
    y feriados). None si ninguno dentro de `max_lookahead` días (p.ej. fecha futura
    cuyo día siguiente aún no tiene datos)."""
    d = pd.Timestamp(date)
    for _ in range(max_lookahead):
        d = d + pd.Timedelta(days=1)
        ds = d.strftime("%Y-%m-%d")
        try:
            if not downloader.underlying(ticker, ds).empty:
                return ds
        except Exception:
            pass
    return None


def run_overnight_1dte(
    downloader: Downloader,
    ticker: str,
    date: str,                         # buy_date
    premium_min: float,
    premium_max: float,
    invest_call: float,
    invest_put: float,
    start_ts: pd.Timestamp,            # entrada en buy_date (date + Horario de entrada)
    iteration_idx: int = 0,
    max_strikes_to_probe: int = 25,
    mode: str = "both",
    ext_min: Optional[float] = None,
    ext_max: Optional[float] = None,
    value_target: float = 2.0,
    selection_criterion: str = "spread",
    spread_cfg: Optional[dict] = None,
    sell_date: Optional[str] = None,
    exit_time: Optional[time] = None,
    entry_at_ask: bool = False,
    exit_at_bid: bool = False,
) -> IterationResult:
    """DTE=1 (overnight): compra un contrato que VENCE el día hábil siguiente, en
    `date` a la hora de entrada, y lo vende el día hábil siguiente al `exit_time`
    (Horario de salida). Si exit_time es None, vende a la misma hora de entrada.
    Selección de contrato según `selection_criterion` (default 'spread' = Opción 1).
    Sin umbral ni stop: el único evento de salida es la venta del día siguiente.
    exit_reason='overnight_1dte'. Lanza ValueError si no hay día hábil siguiente."""
    ticker = ticker.upper().strip()
    buy_ts = pd.Timestamp(start_ts)
    entry_time = buy_ts.time()
    sell_time = exit_time or entry_time   # hora de venta del día siguiente (Horario de salida)

    if sell_date is None:
        sell_date = next_trading_day(downloader, ticker, date)
    if sell_date is None:
        raise ValueError(
            f"1DTE: no hay día hábil siguiente con datos para {ticker} tras "
            f"{date} (¿fecha demasiado reciente/futura?)."
        )
    sell_ts = pd.Timestamp.combine(pd.Timestamp(sell_date).date(), sell_time)
    if buy_ts.tz is not None:
        sell_ts = sell_ts.tz_localize(buy_ts.tz)

    # Spot en el día de compra a la hora de entrada.
    under_b = downloader.underlying(ticker, date)
    if under_b.empty:
        # Día de compra sin sesión (feriado, etc.) → error "1DTE:" para que el
        # backtest de rango lo SALTE limpio en vez de listarlo como error.
        raise ValueError(f"1DTE: sin datos del subyacente {ticker} en {date} (¿feriado/sin sesión?).")
    ub = under_b[under_b["timestamp"] >= buy_ts]
    if ub.empty:
        raise ValueError(f"1DTE: sin barras de {ticker} tras {buy_ts:%H:%M} en {date}.")
    spot_at_start = float(ub.iloc[0]["open"])

    # Cadena que VENCE el día hábil siguiente (= 1DTE el día de compra).
    chain = downloader.chain(ticker, sell_date)
    if chain.empty:
        raise ValueError(f"1DTE: sin cadena venciendo {sell_date} para {ticker}")
    calls_chain = chain[chain["contract_type"].str.lower() == "call"].copy()
    puts_chain = chain[chain["contract_type"].str.lower() == "put"].copy()

    if mode == "call_only":
        invest_put = 0.0
    elif mode == "put_only":
        invest_call = 0.0

    calls_sorted = calls_chain.assign(
        _d=(calls_chain["strike_price"] - spot_at_start).abs()
    ).sort_values(["_d", "strike_price"])
    puts_sorted = puts_chain.assign(
        _d=(puts_chain["strike_price"] - spot_at_start).abs()
    ).sort_values(["_d", "strike_price"])

    spread_cfg = spread_cfg or load_spread_config()
    probe_end = buy_ts + pd.Timedelta(minutes=1)

    def _pick(sorted_chain, right):
        # Probing en buy_date: lee la prima de COMPRA al minuto de entrada y elige
        # según selection_criterion (default 'spread' = Opción 1).
        return _probe_premium_range(
            downloader, ticker, date, sorted_chain, right, buy_ts, probe_end,
            premium_min, premium_max, max_strikes_to_probe,
            ext_min=ext_min, ext_max=ext_max, spot=spot_at_start, spread_cfg=spread_cfg,
            selection_criterion=selection_criterion, value_target=value_target,
        )

    _empty = StrikeProbe(strike=0.0, opening_premium=0.0, occ="", in_range=False)
    if mode != "put_only":
        call_pick, call_probes, call_tier = _pick(calls_sorted, "C")
        if call_pick is None:
            raise NoMatchError("CALL", call_probes, premium_min, premium_max)
    else:
        call_pick, call_probes, call_tier = _empty, [], ""
    if mode != "call_only":
        put_pick, put_probes, put_tier = _pick(puts_sorted, "P")
        if put_pick is None:
            raise NoMatchError("PUT", put_probes, premium_min, premium_max)
    else:
        put_pick, put_probes, put_tier = _empty, [], ""

    def _ovn_entry(pick):
        # entry_at_ask: pagar el ASK del NBBO al minuto de compra (fill realista).
        if entry_at_ask and (pick.opening_premium or 0) > 0:
            ask = pick.ask
            if ask is None and pick.occ:
                try:
                    ask = _robust_quote(downloader, pick.occ, date, buy_ts,
                                        ref_premium=pick.opening_premium).get("ask")
                except Exception:
                    ask = None
            if ask and ask > 0:
                return float(ask)
        return float(pick.opening_premium or 0.0)
    call_entry = _ovn_entry(call_pick)
    put_entry = _ovn_entry(put_pick)

    def _sell_premium(occ: str) -> Optional[float]:
        if not occ:
            return 0.0
        if exit_at_bid:   # venta al BID del NBBO al minuto de salida (lo que cobrás)
            try:
                _b = downloader.option_quote(occ, sell_date, sell_ts).get("bid")
            except Exception:
                _b = None
            if _b is not None and _b >= 0:
                return float(_b)
        sb = downloader.option(occ, sell_date)
        if sb.empty:
            return None
        sin_ = sb[sb["timestamp"] >= sell_ts]
        if sin_.empty:
            return float(sb.iloc[-1]["open"])   # sin barra a esa hora → última del día
        return float(sin_.iloc[0]["open"])

    call_exit = _sell_premium(call_pick.occ) if mode != "put_only" else 0.0
    put_exit = _sell_premium(put_pick.occ) if mode != "call_only" else 0.0
    if call_exit is None or put_exit is None:
        raise ValueError(
            f"1DTE: sin barras de venta el {sell_date} a las {sell_time:%H:%M} "
            f"(CALL={call_exit}, PUT={put_exit})."
        )

    initial_total = invest_call + invest_put
    fc = invest_call * (call_exit / call_entry) if call_entry else 0.0
    fp = invest_put * (put_exit / put_entry) if put_entry else 0.0
    final_total = fc + fp

    # Spot del día de venta + df mínimo de 2 puntos (compra → venta overnight) para
    # que build_chart / _build_display_df rendericen igual que en intradía.
    under_s = downloader.underlying(ticker, sell_date)
    us = under_s[under_s["timestamp"] >= sell_ts] if not under_s.empty else under_s
    spot_at_sell = float(us.iloc[0]["open"]) if not us.empty else spot_at_start
    _ce, _pe = (call_exit or 0.0), (put_exit or 0.0)
    overnight_df = pd.DataFrame({
        "timestamp": [buy_ts, sell_ts],
        "spot": [spot_at_start, spot_at_sell],
        "call_px": [call_entry, _ce],
        "put_px": [put_entry, _pe],
        "total": [call_entry + put_entry, _ce + _pe],
        "pnl_acum": [0.0, final_total - initial_total],
    })

    return IterationResult(
        iteration=iteration_idx,
        invest_call=invest_call, invest_put=invest_put,
        premium_min=premium_min, premium_max=premium_max,
        exit_threshold_pct=0.0, exit_metric="total", stop_loss_pct=-1.0,
        start_dt=buy_ts, end_dt=sell_ts, exit_reason="overnight_1dte",
        spot_at_start=spot_at_start,
        call_strike=call_pick.strike, put_strike=put_pick.strike,
        call_occ=call_pick.occ, put_occ=put_pick.occ,
        call_entry_premium=call_entry, put_entry_premium=put_entry,
        call_exit_premium=(call_exit or 0.0), put_exit_premium=(put_exit or 0.0),
        initial_total=initial_total, final_total=final_total,
        max_total=max(initial_total, final_total), max_total_dt=sell_ts,
        min_total=min(initial_total, final_total), min_total_dt=sell_ts,
        call_probes=call_probes, put_probes=put_probes, df=overnight_df,
        mode=mode,
        call_bid=call_pick.bid, call_ask=call_pick.ask, call_spread=call_pick.spread,
        call_range_tier=call_tier,
        put_bid=put_pick.bid, put_ask=put_pick.ask, put_spread=put_pick.spread,
        put_range_tier=put_tier,
        call_exit_reason="overnight_1dte", put_exit_reason="overnight_1dte",
    )


def _run_one_iteration(
    downloader: Downloader,
    ticker: str,
    date: str,
    under_full: pd.DataFrame,
    calls_chain: pd.DataFrame,
    puts_chain: pd.DataFrame,
    premium_min: float,
    premium_max: float,
    invest_call: float,
    invest_put: float,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    exit_threshold_pct: float,
    iteration_idx: int,
    max_strikes_to_probe: int,
    exit_metric: str = "total",
    stop_loss_pct: float = -1.0,
    mode: str = "both",
    ext_min: Optional[float] = None,
    ext_max: Optional[float] = None,
    check_step_min: int = 1,
    call_exit_threshold_pct: float = 0.10,
    call_stop_loss_pct: float = -1.0,
    put_exit_threshold_pct: float = 0.10,
    put_stop_loss_pct: float = -1.0,
    exit_plus_threshold_pct: float = 0.50,
    exit_plus_time: Optional[time] = None,
    refuerzo_loss_threshold_pct: float = 0.50,
    refuerzo_max_count: int = 2,
    selection_criterion: str = "itm",
    value_target: float = 2.0,
    spread_cfg: Optional[dict] = None,
    entry_at_ask: bool = False,
    exit_at_bid: bool = False,
    nbbo_timeline: bool = False,
) -> IterationResult:
    # En single-leg, la inversión del leg no usado debe ser 0 para que el ROI
    # ponderado refleje SOLO la pierna activa (de lo contrario el invest "fantasma"
    # diluiría el ROI: ej. call_only con invest_put=10k haría ROI = pct_call / 2).
    if mode == "call_only":
        invest_put = 0.0
    elif mode == "put_only":
        invest_call = 0.0

    under = under_full[
        (under_full["timestamp"] >= start_ts) & (under_full["timestamp"] <= end_ts)
    ].reset_index(drop=True)
    if under.empty:
        raise ValueError(f"No bars after {start_ts}")
    spot_at_start = float(under.iloc[0]["open"])

    calls_sorted = calls_chain.assign(
        _d=(calls_chain["strike_price"] - spot_at_start).abs()
    ).sort_values(["_d", "strike_price"])
    puts_sorted = puts_chain.assign(
        _d=(puts_chain["strike_price"] - spot_at_start).abs()
    ).sort_values(["_d", "strike_price"])

    spread_cfg = spread_cfg or load_spread_config()

    # Probing CALL (skip si mode == "put_only")
    if mode != "put_only":
        call_pick, call_probes, call_tier = _probe_premium_range(
            downloader, ticker, date, calls_sorted, "C", start_ts, end_ts,
            premium_min, premium_max, max_strikes_to_probe,
            ext_min=ext_min, ext_max=ext_max, spot=spot_at_start, spread_cfg=spread_cfg,
            selection_criterion=selection_criterion, value_target=value_target,
        )
        if call_pick is None:
            raise NoMatchError("CALL", call_probes, premium_min, premium_max)
        call_fallback = (call_tier == "fallback")
        df_c = downloader.option(call_pick.occ, date)
    else:
        call_pick = StrikeProbe(strike=0.0, opening_premium=0.0, occ="", in_range=False)
        call_probes = []
        call_fallback = False
        call_tier = ""
        df_c = pd.DataFrame({
            "timestamp": under["timestamp"],
            "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0,
        })

    # Probing PUT (skip si mode == "call_only")
    if mode != "call_only":
        put_pick, put_probes, put_tier = _probe_premium_range(
            downloader, ticker, date, puts_sorted, "P", start_ts, end_ts,
            premium_min, premium_max, max_strikes_to_probe,
            ext_min=ext_min, ext_max=ext_max, spot=spot_at_start, spread_cfg=spread_cfg,
            selection_criterion=selection_criterion, value_target=value_target,
        )
        if put_pick is None:
            raise NoMatchError("PUT", put_probes, premium_min, premium_max)
        put_fallback = (put_tier == "fallback")
        df_p = downloader.option(put_pick.occ, date)
    else:
        put_pick = StrikeProbe(strike=0.0, opening_premium=0.0, occ="", in_range=False)
        put_probes = []
        put_fallback = False
        put_tier = ""
        df_p = pd.DataFrame({
            "timestamp": under["timestamp"],
            "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0,
        })

    merged = _merge_minute(under, df_c, df_p, start_ts, end_ts)
    if merged.empty:
        raise ValueError("No overlapping minute bars between underlying, CALL and PUT")

    # "Sell verification (min)": el ROI se verifica cada `check_step_min` minutos
    # (no cada minuto), simulando que el API se consulta a ese período. Subsampleamos
    # el minuto-a-minuto a una grilla absoluta desde la entrada (offset 0, N, 2N, …).
    # La fila 0 (entrada) siempre se conserva. N=1 => comportamiento original.
    _step = int(check_step_min) if check_step_min else 1
    if _step > 1 and len(merged) > 1:
        _t0 = merged["timestamp"].iloc[0]
        _mins = ((merged["timestamp"] - _t0).dt.total_seconds() / 60.0).round().astype(int)
        merged = merged[_mins % _step == 0].reset_index(drop=True)

    # "Hora de salida" (solo modo "CALL o PUT (plus)"): ambas piernas se venden a
    # MÁS TARDAR a esta hora, en vez de al cierre (16:00). Se capea la ventana acá:
    # si la condición de la estrategia no se cumple antes, la última fila será esta
    # hora y ambas piernas se liquidan ahí.
    if mode == "call_or_put_plus" and exit_plus_time is not None and not merged.empty:
        _hs_ts = merged["timestamp"].iloc[0].normalize() + pd.Timedelta(
            hours=int(exit_plus_time.hour), minutes=int(exit_plus_time.minute))
        _capped = merged[merged["timestamp"] <= _hs_ts]
        # Si la Hora de salida queda ANTES de la entrada, el cap dejaría la ventana
        # vacía → se IGNORA el cap (se usa la ventana completa) en vez de fallar la
        # iteración. (La UI igual valida que sea posterior a la hora de orden.)
        if not _capped.empty:
            merged = _capped.reset_index(drop=True)

    # Fills NBBO POR BARRA (Fase 2): la VALUACIÓN y los triggers usan el BID por minuto (lo
    # que REALMENTE cobrás si vendés en ese bar), no el precio del bar. Reemplazamos call_px/
    # put_px por la línea de bid (mark) alineada al cierre de cada minuto; la entrada sigue al
    # ASK y los refuerzos se compran al ASK (series _ask_*_ser). Así TODOS los modos/triggers
    # disparan sobre el bid sin tocar su lógica. Si no hay timeline para una pierna, esa pierna
    # cae al precio del bar (degradación segura). bid=0 (worthless) es válido → no se rellena.
    _ask_c_ser = _ask_p_ser = None
    if nbbo_timeline:
        _idx = pd.DatetimeIndex(merged["timestamp"])

        def _nbbo_series(pick, bar_col):
            if not pick.occ or (pick.opening_premium or 0) <= 0:
                return None, None
            try:
                qs = downloader.option_quote_series(pick.occ, date)
            except Exception:
                qs = None
            if qs is None or qs.empty:
                return None, None
            qs = qs.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
            _bar = pd.Series(merged[bar_col].values, index=_idx)
            _bid = qs["bid"].reindex(_idx, method="ffill")
            _ask = qs["ask"].reindex(_idx, method="ffill")
            _bid = _bid.where(_bid.notna(), _bar)   # minutos sin quote → precio del bar
            _ask = _ask.where(_ask.notna(), _bar)
            return _bid, _ask

        _bid_c, _ask_c_ser = _nbbo_series(call_pick, "call_px")
        _bid_p, _ask_p_ser = _nbbo_series(put_pick, "put_px")
        if _bid_c is not None:
            merged["call_px"] = _bid_c.values
        if _bid_p is not None:
            merged["put_px"] = _bid_p.values
        merged["total"] = merged["call_px"] + merged["put_px"]

    # Base de costo de entrada: 'open' del bar (default), el ASK del NBBO al minuto de entrada
    # (entry_at_ask / Fase 1 → pagás la oferta), o el ASK de la línea NBBO (Fase 2, fila 0).
    # Si el ask no está cacheado, se pide al vuelo; si no hay NBBO, cae al 'open'.
    def _entry_prem(pick, ask_ser=None):
        # Comprás al INICIO del minuto de entrada → ask PUNTUAL en start_ts (igual que Fase 1).
        # nbbo_timeline también implica entrada al ask. El ask de la línea (fin del minuto) es
        # solo fallback si el point-quote falla.
        if (entry_at_ask or nbbo_timeline) and (pick.opening_premium or 0) > 0:
            ask = pick.ask
            if ask is None and pick.occ:
                try:
                    ask = _robust_quote(downloader, pick.occ, date, start_ts,
                                        ref_premium=pick.opening_premium).get("ask")
                except Exception:
                    ask = None
            if ask and ask > 0:
                return float(ask)
            if nbbo_timeline and ask_ser is not None:
                a0 = float(ask_ser.iloc[0])
                if a0 and a0 > 0:
                    return a0
        return float(pick.opening_premium or 0.0)
    call_entry = _entry_prem(call_pick, _ask_c_ser)
    put_entry = _entry_prem(put_pick, _ask_p_ser)

    # Cálculo de % por leg. Si la pierna fue "skip" (opening_premium == 0),
    # forzamos pct = 0 para evitar división por cero y para que no contamine
    # el total ni los triggers. La BASE es call_entry/put_entry (ask si entry_at_ask).
    if call_pick.opening_premium > 0:
        pct_call = (merged["call_px"] - call_entry) / call_entry
    else:
        pct_call = pd.Series(0.0, index=merged.index)
    if put_pick.opening_premium > 0:
        pct_put = (merged["put_px"] - put_entry) / put_entry
    else:
        pct_put = pd.Series(0.0, index=merged.index)
    # pct_total = ROI real sobre el capital invertido (ponderado por inversión por leg).
    _total_invest = invest_call + invest_put
    if _total_invest > 0:
        pct_total = (invest_call * pct_call + invest_put * pct_put) / _total_invest
    else:
        pct_total = pd.Series(0.0, index=merged.index)
    # Defaults de metadata de salida por pierna (solo se llenan en "call_or_put").
    call_exit_idx: Optional[int] = None
    put_exit_idx: Optional[int] = None
    call_exit_reason = ""
    put_exit_reason = ""
    _refuerzo = None   # solo se llena en mode == "both_refuerzo" (martingala)

    def _first_pos(mask) -> Optional[int]:
        return int(mask.values.argmax()) if bool(mask.any()) else None

    if mode == "call_or_put":
        # ----- Salida COMBINADA al +100% -----
        # Se compran ambas piernas y se VENDEN LAS DOS en la primera fila donde
        # CUALQUIERA de las dos alcanza +100% (la prima se duplica). NO usa Umbral
        # de ROI por pierna ni Stop loss. Si ninguna llega a +100%, ambas se cierran
        # al final del día (EOD). Los parámetros *_threshold/*_stop_loss se ignoran.
        TARGET = 1.0  # +100%
        trigger_pos = _first_pos((pct_call >= TARGET) | (pct_put >= TARGET))
        if trigger_pos is not None:
            exit_reason = "100%_threshold"
            # Marca verde la(s) celda(s) de la pierna que alcanzó +100% en la fila
            # de salida (puede ser una o ambas; la otra se vende igual pero sin marca).
            if bool(pct_call.iloc[trigger_pos] >= TARGET):
                call_exit_idx, call_exit_reason = trigger_pos, "100%_threshold"
            if bool(pct_put.iloc[trigger_pos] >= TARGET):
                put_exit_idx, put_exit_reason = trigger_pos, "100%_threshold"
            merged = merged.iloc[: trigger_pos + 1].reset_index(drop=True)
        else:
            # Ninguna llegó a +100% → ambas se cierran a EOD (sin marca verde).
            exit_reason = "session_end"
            call_exit_reason = put_exit_reason = "session_end"
            merged = merged.reset_index(drop=True)
    elif mode == "call_or_put_plus":
        # ----- CALL o PUT (plus): umbral de salida + recuperar inversión total -----
        # 1) La PRIMERA pierna (A) que alcanza `exit_plus_threshold_pct` se vende y
        #    banca su valor: proceeds_A = invest_A × (1 + pct_A_al_salir).
        # 2) La otra (B) se vende cuando proceeds_A + valor_B >= inversión TOTAL
        #    (invest_call + invest_put): entre lo bancado de A y el valor actual de B
        #    se recupera la inversión inicial.
        # 3) Si ninguna alcanza el umbral, o B nunca recupera → cierran a EOD.
        TARGET_A = float(exit_plus_threshold_pct)
        T_total = invest_call + invest_put
        call_hit = _first_pos(pct_call >= TARGET_A)
        put_hit = _first_pos(pct_put >= TARGET_A)

        if call_hit is None and put_hit is None:
            # Ninguna pierna llegó al umbral → ambas cierran a EOD.
            exit_reason = "session_end"
            call_exit_reason = put_exit_reason = "session_end"
            merged = merged.reset_index(drop=True)
        else:
            # A = primera pierna en alcanzar el umbral; B = la otra.
            a_is_call = call_hit is not None and (put_hit is None or call_hit <= put_hit)
            a_idx = call_hit if a_is_call else put_hit
            if a_is_call:
                invest_A, invest_B, pct_A, pct_B = invest_call, invest_put, pct_call, pct_put
                a_col = "call_px"
            else:
                invest_A, invest_B, pct_A, pct_B = invest_put, invest_call, pct_put, pct_call
                a_col = "put_px"

            proceeds_A = invest_A * (1.0 + float(pct_A.iloc[a_idx]))   # valor de A al vender
            need_B = T_total - proceeds_A                              # lo que falta recuperar
            value_B = invest_B * (1.0 + pct_B)                         # serie de valor de B

            b_recover = (value_B >= need_B)
            if a_idx > 0:
                b_recover.iloc[:a_idx] = False   # B sólo puede salir desde que A salió
            b_idx = _first_pos(b_recover)
            if b_idx is None:
                # B nunca recupera la inversión → cierra a EOD (condición no cumplida).
                b_idx = len(merged) - 1
                b_reason = "session_end"
                exit_reason = "session_end"
            else:
                b_reason = "100%_threshold"
                exit_reason = "100%_threshold"

            end_pos = b_idx  # b_idx >= a_idx siempre
            # Truncar a end_pos y CONGELAR la pierna A desde su salida (ya vendida).
            merged = merged.iloc[: end_pos + 1].copy()
            if a_idx < end_pos:
                merged.loc[a_idx + 1:, a_col] = float(merged.loc[a_idx, a_col])
            merged["total"] = merged["call_px"] + merged["put_px"]
            merged = merged.reset_index(drop=True)

            # Marcas verdes: A salió por umbral en a_idx; B recuperó la inversión en b_idx.
            if a_is_call:
                call_exit_idx, call_exit_reason = a_idx, "100%_threshold"
                put_exit_idx, put_exit_reason = b_idx, b_reason
            else:
                put_exit_idx, put_exit_reason = a_idx, "100%_threshold"
                call_exit_idx, call_exit_reason = b_idx, b_reason
    elif mode == "both_plus":
        # ----- CALL y PUT (plus): se compran ambas piernas y se venden las DOS SOLO
        # al llegar a la Hora de salida (end_ts). NO usa Umbral de ROI ni Stop loss. -----
        trigger_pos = None
        exit_reason = "session_end"
        merged = merged.reset_index(drop=True)
    elif mode == "both_refuerzo":
        # ----- CALL y PUT (Refuerzo): MARTINGALA POR PIERNA del MISMO tipo. Cada pierna mira SU
        # propio ROI y, cuando cae a <= -refuerzo_loss_threshold_pct, compra MÁS de la MISMA
        # pierna (nunca la contraria). Sale por: ROI TOTAL >= Umbral de ROI (gana); ROI TOTAL
        # <= stop_loss_pct (stop sobre el TOTAL, no por pierna); o cierre. ROI/valor/invertido
        # TOTAL por minuto quedan en merged (ref_roi/ref_value/ref_invested) para el display. ---
        _exi, exit_reason, _rroi, _rval, _rinv, _events = _simulate_refuerzo(
            merged["call_px"].values, merged["put_px"].values, call_entry, put_entry,
            invest_call, invest_put, exit_threshold_pct, float(refuerzo_loss_threshold_pct),
            int(refuerzo_max_count), stop_thr=float(stop_loss_pct),
            call_buy_px=(_ask_c_ser.values if _ask_c_ser is not None else None),
            put_buy_px=(_ask_p_ser.values if _ask_p_ser is not None else None))
        merged = merged.iloc[: _exi + 1].reset_index(drop=True)
        merged["total"] = merged["call_px"] + merged["put_px"]
        merged["ref_roi"] = _rroi
        merged["ref_value"] = _rval
        merged["ref_invested"] = _rinv
        _ridx = sorted({int(_e["idx"]) for _e in _events})   # minutos únicos para el marcador ➕
        _refuerzo = {"gain": float(_rval[-1] - _rinv[-1]), "invest": float(_rinv[-1]),
                     "n": len(_events), "idxs": _ridx, "events": _events,
                     "n_call": sum(1 for _e in _events if _e["leg"] == "CALL"),
                     "n_put": sum(1 for _e in _events if _e["leg"] == "PUT")}
    else:
        # ----- Salida combinada / single-leg (modos existentes) -----
        if exit_metric == "call":
            profit_mask = pct_call >= exit_threshold_pct
        elif exit_metric == "put":
            profit_mask = pct_put >= exit_threshold_pct
        else:
            profit_mask = pct_total >= exit_threshold_pct

        loss_mask = pct_total <= stop_loss_pct
        profit_pos = _first_pos(profit_mask)
        loss_pos = _first_pos(loss_mask)

        if profit_pos is not None and (loss_pos is None or profit_pos <= loss_pos):
            trigger_pos = profit_pos
            exit_reason = "100%_threshold"
        elif loss_pos is not None:
            trigger_pos = loss_pos
            exit_reason = "stop_loss"
        else:
            trigger_pos = None
            exit_reason = "session_end"

        if trigger_pos is not None:
            merged = merged.iloc[: trigger_pos + 1].reset_index(drop=True)
        else:
            merged = merged.reset_index(drop=True)

    # exit_at_bid (Fase 1): la venta se realiza al BID del NBBO al minuto de salida (lo que
    # REALMENTE cobrás), no al precio del bar. Ajusta SOLO la última fila (la salida); el resto
    # de la serie queda en precio de bar (el trigger ya se detectó arriba). Bajo nbbo_timeline
    # (Fase 2) NO se aplica: toda la serie YA es el bid → la última fila ya es el bid.
    if exit_at_bid and not nbbo_timeline and not merged.empty:
        _exit_ts = merged["timestamp"].iloc[-1]
        _last = merged.index[-1]
        for _col, _pick in (("call_px", call_pick), ("put_px", put_pick)):
            if (_pick.opening_premium or 0) > 0 and _pick.occ:
                try:
                    _bid = downloader.option_quote(_pick.occ, date, _exit_ts).get("bid")
                except Exception:
                    _bid = None
                if _bid is not None and _bid >= 0:
                    merged.loc[_last, _col] = float(_bid)
        merged.loc[_last, "total"] = float(merged.loc[_last, "call_px"] + merged.loc[_last, "put_px"])

    merged["call_strike"] = call_pick.strike
    merged["put_strike"] = put_pick.strike
    initial_total = float(call_entry + put_entry)
    merged["pnl_acum"] = merged["total"] - initial_total

    idx_max = int(merged["total"].idxmax())
    idx_min = int(merged["total"].idxmin())

    return IterationResult(
        iteration=iteration_idx,
        invest_call=invest_call,
        invest_put=invest_put,
        premium_min=premium_min,
        premium_max=premium_max,
        exit_threshold_pct=exit_threshold_pct,
        exit_metric=exit_metric,
        stop_loss_pct=stop_loss_pct,
        start_dt=merged.iloc[0]["timestamp"],
        end_dt=merged.iloc[-1]["timestamp"],
        exit_reason=exit_reason,
        spot_at_start=spot_at_start,
        call_strike=call_pick.strike,
        put_strike=put_pick.strike,
        call_occ=call_pick.occ,
        put_occ=put_pick.occ,
        call_entry_premium=float(call_entry),
        put_entry_premium=float(put_entry),
        call_exit_premium=float(merged.iloc[-1]["call_px"]),
        put_exit_premium=float(merged.iloc[-1]["put_px"]),
        initial_total=initial_total,
        final_total=float(merged.iloc[-1]["total"]),
        max_total=float(merged.loc[idx_max, "total"]),
        max_total_dt=merged.loc[idx_max, "timestamp"],
        min_total=float(merged.loc[idx_min, "total"]),
        min_total_dt=merged.loc[idx_min, "timestamp"],
        call_probes=call_probes,
        put_probes=put_probes,
        df=merged,
        mode=mode,
        call_fallback=call_fallback,
        put_fallback=put_fallback,
        call_bid=getattr(call_pick, "bid", None),
        call_ask=getattr(call_pick, "ask", None),
        call_spread=getattr(call_pick, "spread", None),
        call_range_tier=call_tier,
        put_bid=getattr(put_pick, "bid", None),
        put_ask=getattr(put_pick, "ask", None),
        put_spread=getattr(put_pick, "spread", None),
        put_range_tier=put_tier,
        call_exit_idx=call_exit_idx,
        put_exit_idx=put_exit_idx,
        call_exit_reason=call_exit_reason,
        put_exit_reason=put_exit_reason,
        refuerzo=_refuerzo,
    )


def _merge_minute(
    under: pd.DataFrame,
    df_c: pd.DataFrame,
    df_p: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    u = under[["timestamp", "close"]].rename(columns={"close": "spot"})
    c = df_c[["timestamp", "close"]].rename(columns={"close": "call_px"})
    p = df_p[["timestamp", "close"]].rename(columns={"close": "put_px"})
    df = u.merge(c, on="timestamp", how="left").merge(p, on="timestamp", how="left")
    df = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)].copy()
    df["call_px"] = df["call_px"].ffill().bfill()
    df["put_px"] = df["put_px"].ffill().bfill()
    df = df.dropna(subset=["call_px", "put_px", "spot"]).reset_index(drop=True)
    df["total"] = df["call_px"] + df["put_px"]
    return df

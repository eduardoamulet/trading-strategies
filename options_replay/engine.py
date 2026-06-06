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

DEFAULT_TIME_START = time(9, 30)
DEFAULT_TIME_END = time(16, 0)
MAX_STRIKES_TO_PROBE = 25  # per side; safety cap


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
    """Máximo spread aceptable según el bucket donde cae el precio del subyacente."""
    for b in cfg.get("buckets", []):
        if b["price_min"] <= spot < b["price_max"]:
            return float(b["max_spread"])
    return float("inf")  # fuera de todos los buckets → sin filtro


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
        return self.gain_call + self.gain_put

    @property
    def invest_total(self) -> float:
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
    selection_criterion: str = "itm",   # "itm" (cercano a ITM) | "spread" (menor spread) | "value" (prima ≈ value_target, IGNORA spread)
    value_target: float = 2.0,          # objetivo de prima ($) para selection_criterion="value"
) -> tuple[Optional[StrikeProbe], list[StrikeProbe], str]:
    """Selector de contrato: filtro de spread (compuerta dura) + Óptimo/Extendido.

    Lógica (confirmada por el usuario — SIN fallback):
    1. Candidatos: hasta max_probe strikes ordenados por cercanía a ATM; se lee
       su premium de apertura en el minuto de entrada.
    2. Filtro de spread (COMPUERTA): trae el NBBO y descarta todo contrato cuyo
       spread (ask-bid) supere el máximo del bucket de precio del subyacente
       (spread_config.json). Si NINGÚN contrato pasa el filtro → NO se compra.
    3. Entre los que pasan spread, dentro del Rango Óptimo → el MÁS CERCANO A ITM.
       tier="optimo".
    4. Si ninguno en óptimo: dentro del Rango Extendido → el MÁS CERCANO A ITM.
       tier="extended".
    5. Si tampoco hay en extendido → NO se compra (None). NO hay fallback ni
       mecanismo alternativo de selección.
    Devuelve (probe_elegido, todos_los_probes, tier). tier="" si no hubo compra.

    Retro-compat: ext_min/ext_max default al rango óptimo; si spread_cfg es None
    o el filtro está deshabilitado, se omite el filtro de spread.
    """
    if ext_min is None:
        ext_min = premium_min
    if ext_max is None:
        ext_max = premium_max
    if spread_cfg is None:
        spread_cfg = load_spread_config()

    spread_enabled = bool(spread_cfg.get("enable_spread_filter", True))
    max_spread = _max_spread_for_price(spot, spread_cfg) if spot is not None else float("inf")
    # Rango de fetch de quotes = unión de óptimo y extendido (eficiencia: solo
    # pedimos NBBO de candidatos con premium plausible).
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
        in_opt = premium_min <= opening <= premium_max
        in_ext = ext_min <= opening <= ext_max
        itm_depth = (spot - strike) if (spot is not None and right == "C") else \
                    ((strike - spot) if (spot is not None and right == "P") else 0.0)

        bid = ask = spread = None
        spread_ok = True
        # Solo pedimos quote si el premium está en el rango de fetch (óptimo∪ext)
        # y el filtro de spread está activo.
        if spread_enabled and (fetch_lo <= opening <= fetch_hi):
            q = downloader.option_quote(occ, date, start_ts)
            bid, ask, spread = q.get("bid"), q.get("ask"), q.get("spread")
            spread_ok = (spread is not None) and (spread <= max_spread)

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
        #   "spread" (opción 1): el de MENOR spread (desempate: cercano a ITM, volumen).
        #   "value"  (opción 2): IGNORA el spread; el de prima de entrada MÁS CERCANA a
        #            value_target ($2 default), igual para CALL y PUT. Desempate: ITM.
        #   "itm"    (legado): de los 2 de menor spread, el más cercano a ITM.
        if selection_criterion == "spread":
            return min(cands, key=lambda p: (_sp(p), _itm_rank(p), -(p.volume or 0.0)))
        if selection_criterion == "value":
            return min(cands, key=lambda p: (round(abs((p.opening_premium or 0.0) - value_target), 4),
                                             _itm_rank(p)))
        top2 = sorted(cands, key=lambda p: (_sp(p), _itm_rank(p)))[:2]
        return min(top2, key=lambda p: (_itm_rank(p), _sp(p)))

    def _passes_spread(p: StrikeProbe) -> bool:
        # Opción 2 ("value") NO considera el spread → la compuerta no aplica.
        if selection_criterion == "value":
            return True
        if not spread_enabled:
            return True
        return p.spread_ok

    # a. Óptimo + pasa spread
    cand_opt = [p for p in valid if p.in_range and _passes_spread(p)]
    if cand_opt:
        return _select(cand_opt), probes, "optimo"

    # b. Extendido + pasa spread
    cand_ext = [p for p in valid if p.in_extended and _passes_spread(p)]
    if cand_ext:
        return _select(cand_ext), probes, "extended"

    # c. SIN FALLBACK: si nadie quedó dentro del Óptimo/Extendido pasando el filtro
    #    de spread, NO se compra. No hay mecanismo alternativo de selección.
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
    selection_criterion: str = "itm",
    value_target: float = 2.0,
    salto_exit_time: Optional[time] = None,
) -> IterationResult:
    """Run a single iteration starting at `start_ts`. Public wrapper that loads
    underlying + chain from the downloader cache and then invokes the iteration
    engine. Use for manual mode (one iteration per UI click)."""
    if premium_min >= premium_max:
        raise ValueError(f"premium_min ({premium_min}) must be < premium_max ({premium_max})")
    if premium_min < 0:
        raise ValueError("premium_min cannot be negative")

    ticker = ticker.upper().strip()

    # Opción 3 "Salto 1 DTE": despacha al flujo OVERNIGHT (compra en buy_date a la
    # hora de entrada, vende el día hábil siguiente a la MISMA hora). Selección por
    # value (prima ≈ value_target); ignora Umbral/Stop/Horario de salida.
    if selection_criterion == "salto_1dte":
        return run_overnight_1dte(
            downloader, ticker, date, premium_min, premium_max,
            invest_call, invest_put, start_ts,
            iteration_idx=iteration_idx, max_strikes_to_probe=max_strikes_to_probe,
            mode=mode, ext_min=ext_min, ext_max=ext_max, value_target=value_target,
            exit_time=salto_exit_time,
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
        selection_criterion=selection_criterion,
        value_target=value_target,
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
    sell_date: Optional[str] = None,
    exit_time: Optional[time] = None,
) -> IterationResult:
    """Opción 3 'Salto 1 DTE': compra un contrato que VENCE el día hábil siguiente,
    en `date` a la hora de entrada, y lo vende el día hábil siguiente al `exit_time`
    (Horario de salida). Si exit_time es None, vende a la misma hora de entrada.
    Selección por value (prima de compra ≈ value_target). Sin umbral ni stop: el
    único evento de salida es la venta del día siguiente. exit_reason='overnight_1dte'.
    Lanza ValueError si no hay día hábil siguiente con datos."""
    ticker = ticker.upper().strip()
    buy_ts = pd.Timestamp(start_ts)
    entry_time = buy_ts.time()
    sell_time = exit_time or entry_time   # hora de venta del día siguiente (Horario de salida)

    if sell_date is None:
        sell_date = next_trading_day(downloader, ticker, date)
    if sell_date is None:
        raise ValueError(
            f"Salto 1DTE: no hay día hábil siguiente con datos para {ticker} tras "
            f"{date} (¿fecha demasiado reciente/futura?)."
        )
    sell_ts = pd.Timestamp.combine(pd.Timestamp(sell_date).date(), sell_time)
    if buy_ts.tz is not None:
        sell_ts = sell_ts.tz_localize(buy_ts.tz)

    # Spot en el día de compra a la hora de entrada.
    under_b = downloader.underlying(ticker, date)
    if under_b.empty:
        # Día de compra sin sesión (feriado, etc.) → error "Salto 1DTE:" para que el
        # backtest de rango lo SALTE limpio en vez de listarlo como error.
        raise ValueError(f"Salto 1DTE: sin datos del subyacente {ticker} en {date} (¿feriado/sin sesión?).")
    ub = under_b[under_b["timestamp"] >= buy_ts]
    if ub.empty:
        raise ValueError(f"Salto 1DTE: sin barras de {ticker} tras {buy_ts:%H:%M} en {date}.")
    spot_at_start = float(ub.iloc[0]["open"])

    # Cadena que VENCE el día hábil siguiente (= 1DTE el día de compra).
    chain = downloader.chain(ticker, sell_date)
    if chain.empty:
        raise ValueError(f"Salto 1DTE: sin cadena venciendo {sell_date} para {ticker}")
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

    spread_cfg = load_spread_config()
    probe_end = buy_ts + pd.Timedelta(minutes=1)

    def _pick(sorted_chain, right):
        # Probing en buy_date: lee la prima de COMPRA al minuto de entrada y elige
        # por value (más cercano a value_target). El criterio "value" ignora spread.
        return _probe_premium_range(
            downloader, ticker, date, sorted_chain, right, buy_ts, probe_end,
            premium_min, premium_max, max_strikes_to_probe,
            ext_min=ext_min, ext_max=ext_max, spot=spot_at_start, spread_cfg=spread_cfg,
            selection_criterion="value", value_target=value_target,
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

    call_entry = float(call_pick.opening_premium or 0.0)
    put_entry = float(put_pick.opening_premium or 0.0)

    def _sell_premium(occ: str) -> Optional[float]:
        if not occ:
            return 0.0
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
            f"Salto 1DTE: sin barras de venta el {sell_date} a las {sell_time:%H:%M} "
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
    selection_criterion: str = "itm",
    value_target: float = 2.0,
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

    spread_cfg = load_spread_config()

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

    # Cálculo de % por leg. Si la pierna fue "skip" (opening_premium == 0),
    # forzamos pct = 0 para evitar división por cero y para que no contamine
    # el total ni los triggers.
    if call_pick.opening_premium > 0:
        pct_call = (merged["call_px"] - call_pick.opening_premium) / call_pick.opening_premium
    else:
        pct_call = pd.Series(0.0, index=merged.index)
    if put_pick.opening_premium > 0:
        pct_put = (merged["put_px"] - put_pick.opening_premium) / put_pick.opening_premium
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

    merged["call_strike"] = call_pick.strike
    merged["put_strike"] = put_pick.strike
    initial_total = float(call_pick.opening_premium + put_pick.opening_premium)
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
        call_entry_premium=float(call_pick.opening_premium),
        put_entry_premium=float(put_pick.opening_premium),
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

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
    in_range: bool


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
    """No strike found whose opening premium falls inside the user range."""

    def __init__(self, side: str, probes: list[StrikeProbe], premium_min: float, premium_max: float):
        self.side = side
        self.probes = probes
        self.premium_min = premium_min
        self.premium_max = premium_max
        info = [
            f"strike={p.strike:g} open={p.opening_premium:.2f}" if p.opening_premium is not None
            else f"strike={p.strike:g} open=N/A"
            for p in probes[:8]
        ]
        super().__init__(
            f"No {side} 0 DTE contract has opening premium inside "
            f"[{premium_min:.2f}, {premium_max:.2f}] USD. "
            f"Probed {len(probes)} strikes; sample (USD): {'; '.join(info)}"
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
) -> tuple[Optional[StrikeProbe], list[StrikeProbe]]:
    """Iterate strikes (already sorted by distance to ATM) and return the first whose
    entry premium falls in [premium_min, premium_max]. Also return all probed strikes."""
    probes: list[StrikeProbe] = []
    for i, (_, row) in enumerate(candidates.iterrows()):
        if i >= max_probe:
            break
        strike = float(row["strike_price"])
        occ = PolygonAdapter.build_occ(ticker, date, right, strike)
        bars = downloader.option(occ, date)
        bars_in = bars[(bars["timestamp"] >= start_ts) & (bars["timestamp"] <= end_ts)] if not bars.empty else bars
        if bars_in.empty:
            probes.append(StrikeProbe(strike=strike, opening_premium=None, occ=occ, in_range=False))
            continue
        opening = float(bars_in.iloc[0]["open"])
        in_range = premium_min <= opening <= premium_max
        probe = StrikeProbe(strike=strike, opening_premium=opening, occ=occ, in_range=in_range)
        probes.append(probe)
        if in_range:
            return probe, probes
    return None, probes


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

    call_pick, call_probes = _probe_premium_range(
        downloader, ticker, date, calls, "C", start_ts, end_ts,
        premium_min, premium_max, max_strikes_to_probe,
    )
    if call_pick is None:
        raise NoMatchError("CALL", call_probes, premium_min, premium_max)

    put_pick, put_probes = _probe_premium_range(
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

"""Python port of Strategy_Trend_Reversal BB - 1H BACKTEST.pine

Mirrors the Pine Script indicator logic as faithfully as possible:
- 1H execution with SMA20 / BB(20, 2) + confirmation candle
- 15m confirmation: close > 15m SMA20 AND 15m slope > 0
- Macro trend filter: daily SMA slope
- Cooldown + SMA20 reset with distance %
- Long-only (SHORT disabled by default)
- Trailing chandelier stop

DEFAULT BACKTEST WINDOW: last 3 years (rolling, from current date).
Use trim_to_lookback_years(df, years) before run_backtest to apply the cut.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

# Standard lookback for backtests going forward.
DEFAULT_LOOKBACK_YEARS = 3


def trim_to_lookback_years(df: pd.DataFrame, years: int = DEFAULT_LOOKBACK_YEARS) -> pd.DataFrame:
    """Return only the rows in `df` whose index is within the last `years`."""
    if years is None or years <= 0:
        return df
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(365 * years))
    # Pandas tz-aware comparison — the data index is UTC-tz from Alpaca.
    return df[df.index >= cutoff]


# ---------- Parameters ----------
@dataclass
class StrategyParams:
    """Mirrors the new Pine Script logic (cross-based reset + validity window)."""
    # BB + SMA
    bb_len: int = 20
    bb_mult: float = 2.0

    # 15m confirmation
    ltf_bb_len: int = 20
    ltf_slope_lookback: int = 3

    # Filters
    cooldown_bars: int = 8
    require_reset: bool = True
    cross_validity_window: int = 15   # bars a cross stays VALID before expiring

    # Macro trend filter (daily)
    use_macro_trend: bool = True
    macro_sma_len: int = 50
    macro_slope_bars: int = 5

    # Trailing chandelier stop
    use_trailing_stop: bool = True
    trail_atr_len: int = 22
    trail_atr_mult: float = 2.5

    # Risk: SL/TP in ATR multiples (only used if trailing is OFF)
    atr_len: int = 14
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 4.0

    # Direction
    enable_long: bool = True
    enable_short: bool = False
    close_on_opposite: bool = False


# ---------- Technical indicators (vectorized) ----------
def sma(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(window=length, min_periods=length).mean()


def rolling_std_pine(s: pd.Series, length: int) -> pd.Series:
    """Pine ta.stdev uses population std (ddof=0)."""
    return s.rolling(window=length, min_periods=length).std(ddof=0)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """RMA/Wilder ATR like Pine ta.atr."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    # Pine ta.atr uses RMA (Wilder's). RMA ≈ EMA with alpha = 1/length.
    rma = tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    return rma


# ---------- Multi-timeframe alignment ----------
def align_to_base(base_idx: pd.DatetimeIndex, other: pd.Series) -> pd.Series:
    """Align a series from a higher/lower timeframe to the base 1H index
    using the last closed value at or before each base timestamp
    (equivalent to Pine `request.security(..., lookahead=off)`)."""
    left = pd.DataFrame({"ts": base_idx, "_pos": range(len(base_idx))})
    right = pd.DataFrame({"ts": other.index, "val": other.values})
    left_sorted = left.sort_values("ts")
    right_sorted = right.sort_values("ts")
    merged = pd.merge_asof(left_sorted, right_sorted, on="ts", direction="backward")
    merged = merged.sort_values("_pos")
    return pd.Series(merged["val"].to_numpy(), index=base_idx)


# ---------- Core signal generation ----------
def compute_signals(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_1d: pd.DataFrame,
    p: StrategyParams,
    lookback_years: int | None = DEFAULT_LOOKBACK_YEARS,
) -> pd.DataFrame:
    """Compute all columns needed for backtest on the 1H base index.

    `lookback_years`: when set, all three input dataframes are trimmed to the
    last N years before computing indicators. Pass None to disable trimming.
    Default is 3 years (matches the BACKTEST Pine Script window).
    """
    if lookback_years is not None and lookback_years > 0:
        df_1h  = trim_to_lookback_years(df_1h,  lookback_years)
        df_15m = trim_to_lookback_years(df_15m, lookback_years)
        df_1d  = trim_to_lookback_years(df_1d,  lookback_years)
    out = df_1h.copy()

    # BB / SMA20 on 1H
    out["basis"] = sma(out["close"], p.bb_len)
    dev = p.bb_mult * rolling_std_pine(out["close"], p.bb_len)
    out["upper"] = out["basis"] + dev
    out["lower"] = out["basis"] - dev

    # Cross detection
    prev_close = out["close"].shift(1)
    prev_basis = out["basis"].shift(1)
    out["cross_above"] = (out["close"] > out["basis"]) & (prev_close <= prev_basis)
    out["cross_below"] = (out["close"] < out["basis"]) & (prev_close >= prev_basis)

    # Confirmation candle
    out["bull_candle"] = (out["close"] > out["open"]) & (out["close"] > out["basis"])
    out["bear_candle"] = (out["close"] < out["open"]) & (out["close"] < out["basis"])

    # 15m confirmation
    ltf_basis_raw = sma(df_15m["close"], p.ltf_bb_len)
    ltf_slope_raw = ltf_basis_raw - ltf_basis_raw.shift(p.ltf_slope_lookback)
    out["ltf_close"] = align_to_base(out.index, df_15m["close"])
    out["ltf_basis"] = align_to_base(out.index, ltf_basis_raw)
    out["ltf_slope"] = align_to_base(out.index, ltf_slope_raw)
    out["ltf_bullish"] = (out["ltf_close"] > out["ltf_basis"]) & (out["ltf_slope"] > 0)
    out["ltf_bearish"] = (out["ltf_close"] < out["ltf_basis"]) & (out["ltf_slope"] < 0)

    # Macro trend filter (daily)
    macro_sma_raw = sma(df_1d["close"], p.macro_sma_len)
    macro_sma_prev_raw = macro_sma_raw.shift(p.macro_slope_bars)
    out["macro_sma"] = align_to_base(out.index, macro_sma_raw)
    out["macro_sma_prev"] = align_to_base(out.index, macro_sma_prev_raw)
    out["macro_rising"] = out["macro_sma"] > out["macro_sma_prev"]
    out["macro_falling"] = out["macro_sma"] < out["macro_sma_prev"]

    # ---- Cross validity window ----
    # Track the bar index of the most recent cross-above / cross-below.
    # The signal can fire within `cross_validity_window` bars of that cross
    # (matches the Pine Script behavior).
    n = len(out)
    bar_idx = np.arange(n)
    last_above = np.full(n, -10**9, dtype=np.int64)
    last_below = np.full(n, -10**9, dtype=np.int64)
    cross_above_mask = out["cross_above"].fillna(False).to_numpy()
    cross_below_mask = out["cross_below"].fillna(False).to_numpy()
    cur_above = -10**9
    cur_below = -10**9
    for i in range(n):
        if cross_above_mask[i]:
            cur_above = i
        if cross_below_mask[i]:
            cur_below = i
        last_above[i] = cur_above
        last_below[i] = cur_below
    recent_above = (bar_idx - last_above) <= p.cross_validity_window
    recent_below = (bar_idx - last_below) <= p.cross_validity_window
    out["recent_cross_above"] = recent_above & (last_above > -10**8)
    out["recent_cross_below"] = recent_below & (last_below > -10**8)

    # Raw signals: fire within the validity window when filters align AND
    # the close is still on the correct side of the SMA20.
    out["raw_call"] = out["recent_cross_above"] & (out["close"] > out["basis"]) & out["bull_candle"] & out["ltf_bullish"]
    out["raw_put"]  = out["recent_cross_below"] & (out["close"] < out["basis"]) & out["bear_candle"] & out["ltf_bearish"]

    # ATR for SL/TP / trailing
    out["atr"] = atr(out, p.atr_len)
    out["atr_trail"] = atr(out, p.trail_atr_len)
    out["chandelier_long"] = (
        out["high"].rolling(window=p.trail_atr_len, min_periods=1).max()
        - out["atr_trail"] * p.trail_atr_mult
    )
    out["chandelier_short"] = (
        out["low"].rolling(window=p.trail_atr_len, min_periods=1).min()
        + out["atr_trail"] * p.trail_atr_mult
    )
    return out


# ---------- Backtest ----------
@dataclass
class Trade:
    entry_bar: int
    entry_time: pd.Timestamp
    entry_price: float
    direction: str  # "LONG" / "SHORT"
    qty: float
    exit_bar: int | None = None
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    pnl: float = 0.0
    commission: float = 0.0
    reason: str = ""


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    initial_capital: float = 10_000.0

    def metrics(self) -> dict:
        closed = [t for t in self.trades if t.exit_price is not None]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        net_profit = sum(t.pnl for t in closed)
        total_commission = sum(t.commission for t in closed)
        equity_final = self.initial_capital + net_profit

        max_dd = 0.0
        sharpe = 0.0
        sortino = 0.0
        cagr = 0.0
        if not self.equity_curve.empty:
            peak = self.equity_curve.cummax()
            dd = (peak - self.equity_curve) / peak
            max_dd = float(dd.max()) if not dd.empty else 0.0

            # Daily-resampled equity for risk-adjusted metrics. Using daily
            # returns gives a more standard Sharpe than per-bar 1H returns.
            equity_daily = self.equity_curve.resample("D").last().dropna()
            if len(equity_daily) > 5:
                rets = equity_daily.pct_change().dropna()
                if len(rets) > 0 and rets.std() > 0:
                    sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
                # Sortino: same idea but only downside std.
                downside = rets[rets < 0]
                if len(downside) > 0 and downside.std() > 0:
                    sortino = float(rets.mean() / downside.std() * np.sqrt(252))
                # CAGR
                years = (equity_daily.index[-1] - equity_daily.index[0]).days / 365.25
                if years > 0 and self.initial_capital > 0:
                    cagr = float((equity_final / self.initial_capital) ** (1 / years) - 1)

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(closed)) if closed else 0.0,
            "avg_win": (gross_profit / len(wins)) if wins else 0.0,
            "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
            "win_loss_ratio": (gross_profit / len(wins) / (gross_loss / len(losses)))
                if wins and losses else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "net_profit": net_profit,
            "commission_paid": total_commission,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else 0.0,
            "max_drawdown_pct": max_dd,
            "equity_final": equity_final,
            "sharpe": sharpe,
            "sortino": sortino,
            "cagr": cagr,
        }


def run_backtest(
    signals: pd.DataFrame,
    p: StrategyParams,
    *,
    initial_capital: float = 10_000.0,
    position_size_pct: float = 0.10,
    commission_pct: float = 0.0005,  # 0.05% per side
) -> BacktestResult:
    """Walk through signals bar by bar and simulate trades."""
    trades: list[Trade] = []
    equity = initial_capital
    equity_curve = []

    in_long = False
    long_trade: Trade | None = None

    call_armed = True
    put_armed = True
    last_signal_bar = -10_000

    timestamps = signals.index.to_list()
    close_arr = signals["close"].to_numpy()
    open_arr = signals["open"].to_numpy()
    high_arr = signals["high"].to_numpy()
    low_arr = signals["low"].to_numpy()
    basis_arr = signals["basis"].to_numpy()
    bull_candle_arr = signals["bull_candle"].fillna(False).to_numpy()
    bear_candle_arr = signals["bear_candle"].fillna(False).to_numpy()
    ltf_bull_arr    = signals["ltf_bullish"].fillna(False).to_numpy()
    ltf_bear_arr    = signals["ltf_bearish"].fillna(False).to_numpy()
    macro_rising_arr  = signals["macro_rising"].fillna(False).to_numpy()
    macro_falling_arr = signals["macro_falling"].fillna(False).to_numpy()
    cross_above_arr = signals["cross_above"].fillna(False).to_numpy()
    cross_below_arr = signals["cross_below"].fillna(False).to_numpy()
    chand_long_arr = signals["chandelier_long"].to_numpy()
    atr_arr = signals["atr"].to_numpy()
    n = len(signals)

    # Stateful "last cross" trackers — set to -∞ when consumed by a signal.
    last_cross_above = -10**9
    last_cross_below = -10**9

    for i in range(n):
        close = close_arr[i]
        high = high_arr[i]
        low = low_arr[i]
        basis = basis_arr[i]
        if np.isnan(basis):
            equity_curve.append(equity)
            continue

        # Update the "most recent cross" trackers (resettable via signal fire).
        if bool(cross_above_arr[i]):
            last_cross_above = i
        if bool(cross_below_arr[i]):
            last_cross_below = i

        recent_above = (i - last_cross_above) <= p.cross_validity_window and last_cross_above > -10**8
        recent_below = (i - last_cross_below) <= p.cross_validity_window and last_cross_below > -10**8

        # ---- Cross-based reset (symmetric, mirrors Pine) ----
        if p.require_reset:
            if not call_armed and bool(cross_below_arr[i]):
                call_armed = True
            if not put_armed and bool(cross_above_arr[i]):
                put_armed = True

        cooldown_ok = (i - last_signal_bar) > p.cooldown_bars

        macro_call_ok = (not p.use_macro_trend) or bool(macro_rising_arr[i])
        macro_put_ok  = (not p.use_macro_trend) or bool(macro_falling_arr[i])

        # Re-evaluate raw signals in-loop using the stateful tracker (matches Pine
        # behavior where the pending cross is consumed on signal fire).
        raw_call = recent_above and (close > basis) and bool(bull_candle_arr[i]) and bool(ltf_bull_arr[i])
        raw_put  = recent_below and (close < basis) and bool(bear_candle_arr[i]) and bool(ltf_bear_arr[i])

        call_signal = (raw_call and cooldown_ok and macro_call_ok
                       and (not p.require_reset or call_armed))
        put_signal = (raw_put and cooldown_ok and macro_put_ok
                      and (not p.require_reset or put_armed))

        # ---- Handle exit first (trailing) before potential re-entry this bar ----
        if in_long and long_trade is not None and i > long_trade.entry_bar:
            # Use the stop that was VALID at the start of this bar, i.e. the
            # chandelier value from the PREVIOUS bar's close. This mirrors Pine
            # Script: strategy.exit sets the stop each bar for the NEXT bar.
            raw_stop = chand_long_arr[i - 1] if p.use_trailing_stop else (
                long_trade.entry_price - p.sl_atr_mult * atr_arr[long_trade.entry_bar]
                if p.sl_atr_mult > 0 else np.nan
            )
            tp_level = np.nan
            if not p.use_trailing_stop and p.tp_atr_mult > 0:
                tp_level = long_trade.entry_price + p.tp_atr_mult * atr_arr[long_trade.entry_bar]

            # Trailing stop: update the position's stop to the MAX of previous
            # and new chandelier (stops for longs only move up).
            if p.use_trailing_stop and not np.isnan(raw_stop):
                prev_stop = getattr(long_trade, "_stop", -np.inf)
                trail_stop = max(prev_stop, raw_stop)
                long_trade._stop = trail_stop  # type: ignore[attr-defined]
                stop_level = trail_stop
            else:
                stop_level = raw_stop

            # Sanity: a valid long stop must be BELOW the previous close.
            # If the stop has risen above market (chandelier > prev close),
            # Pine converts it to a market order -> exit at current open/close.
            prev_close = close_arr[i - 1]
            stop_invalid_above_market = (
                not np.isnan(stop_level) and stop_level >= prev_close
            )

            exited = False
            if stop_invalid_above_market:
                # Trailing stop crossed above market -> market exit at open
                exit_price = open_arr[i]
                pnl = (exit_price - long_trade.entry_price) * long_trade.qty
                comm_exit = commission_pct * exit_price * long_trade.qty
                long_trade.exit_bar = i
                long_trade.exit_time = timestamps[i]
                long_trade.exit_price = exit_price
                long_trade.commission = long_trade.commission + comm_exit
                long_trade.pnl = pnl - long_trade.commission
                long_trade.reason = "Trail-Market"
                equity += long_trade.pnl
                in_long = False
                long_trade = None
                exited = True
            elif not np.isnan(stop_level) and low <= stop_level:
                # Normal stop: price touched the stop level during the bar.
                # If the bar gapped DOWN through the stop, fill at open.
                exit_price = min(stop_level, open_arr[i]) if open_arr[i] < stop_level else stop_level
                pnl = (exit_price - long_trade.entry_price) * long_trade.qty
                comm_exit = commission_pct * exit_price * long_trade.qty
                long_trade.exit_bar = i
                long_trade.exit_time = timestamps[i]
                long_trade.exit_price = exit_price
                long_trade.commission = long_trade.commission + comm_exit
                long_trade.pnl = pnl - long_trade.commission
                long_trade.reason = "SL/Trail"
                equity += long_trade.pnl
                in_long = False
                long_trade = None
                exited = True
            elif not np.isnan(tp_level) and high >= tp_level:
                exit_price = tp_level
                pnl = (exit_price - long_trade.entry_price) * long_trade.qty
                comm_exit = commission_pct * exit_price * long_trade.qty
                long_trade.exit_bar = i
                long_trade.exit_time = timestamps[i]
                long_trade.exit_price = exit_price
                long_trade.commission = long_trade.commission + comm_exit
                long_trade.pnl = pnl - long_trade.commission
                long_trade.reason = "TP"
                equity += long_trade.pnl
                in_long = False
                long_trade = None
                exited = True

        # ---- Handle entries ----
        if p.enable_long and call_signal and not in_long:
            qty = (equity * position_size_pct) / close
            if qty > 0:
                comm_entry = commission_pct * close * qty
                long_trade = Trade(
                    entry_bar=i,
                    entry_time=timestamps[i],
                    entry_price=close,
                    direction="LONG",
                    qty=qty,
                    commission=comm_entry,
                )
                trades.append(long_trade)
                in_long = True

        # ---- Bookkeeping after a signal (consume cross + arm/cooldown) ----
        if call_signal:
            last_signal_bar = i
            call_armed = False
            last_cross_above = -10**9   # consume the pending cross
        if put_signal:
            last_signal_bar = i
            put_armed = False
            last_cross_below = -10**9

        # Update equity curve (mark-to-market)
        if in_long and long_trade is not None:
            unreal = (close - long_trade.entry_price) * long_trade.qty - long_trade.commission
            equity_curve.append(equity + unreal)
        else:
            equity_curve.append(equity)

    # Close any remaining open position at the last close
    if in_long and long_trade is not None:
        exit_price = close_arr[-1]
        pnl = (exit_price - long_trade.entry_price) * long_trade.qty
        comm_exit = commission_pct * exit_price * long_trade.qty
        long_trade.exit_bar = n - 1
        long_trade.exit_time = timestamps[-1]
        long_trade.exit_price = exit_price
        long_trade.pnl = pnl - long_trade.commission - comm_exit
        long_trade.commission = long_trade.commission + comm_exit
        long_trade.reason = "EOD"
        equity += long_trade.pnl

    return BacktestResult(
        trades=trades,
        equity_curve=pd.Series(equity_curve, index=signals.index, dtype=float),
        initial_capital=initial_capital,
    )

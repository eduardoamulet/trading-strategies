# Trading Strategies

Pine Script strategies (TradingView) plus a Python optimizer to search for
the best parameters per ticker and validate with walk-forward.

## Repository layout

```
pine_indicators/                   Pine Script files for TradingView
  Strategy_Trend_Reversal BB - 1H.pine             (v1, 20 tickers preset)
  Strategy_Trend_Reversal BB - 1H BACKTEST.pine    (v1 backtest)
  Trend Reversal QQQ & SPY.pine                    (v2, QQQ+SPY hardcoded)
  Trend Reversal QQQ & SPY - BACKTEST.pine         (v2 backtest, 3-year window)
  Strategy_Magnet_Effect.pine                       (Yoel strategy 7+8)
  YC_02_ReboteMM20.pine                             (Yoel strategy 3+4)
  YC_03_GapLateral.pine                             (Yoel strategy 5+6)

optimizer/                         Python grid-search optimizer
  data.py                          Alpaca data loader (with parquet cache)
  strategy.py                      Python port of the Pine logic
  grid.py                          Multi-ticker grid search
  optimize_qqq_spy.py              Exhaustive grid + walk-forward (QQQ+SPY)
  report.py                        Generates Strategy_Optimization_Report.xlsx
  validate.py                      Cross-checks Python vs TradingView numbers

build_*.py                         Excel template builders
```

## Strategy 1+2 — Trend Reversal (QQQ & SPY focus)

The strategy fires LONG (CALL) on a SMA20 cross-up with confirmation candle
and 15-min trend alignment, gated by:
- Macro daily SMA filter (rising → CALL allowed, falling → PUT allowed)
- Cooldown bars after each signal
- Cross-based armed/disarmed state (avoids duplicates without a real reversal)
- Cross-validity window: a cross stays valid for N bars so the signal can
  fire after the strict cross bar if filters align later.

Trailing chandelier stop manages exits.

Validated on TradingView (12-year span) for `QQQ` and `SPY`. The Python
port re-creates the same metrics within ~0.5% PF tolerance.

### Default values (hardcoded in v2)

|              | QQQ | SPY |
|--------------|-----|-----|
| Chandelier × ATR | 2.5 | 2.0 |
| Chandelier ATR length | 14 | 14 |
| Macro SMA length | 50 | 100 |
| Cooldown bars | 8 | 8 |
| Cross-validity window | 15 | 15 |

Backtests use a **rolling 3-year** window from the chart's current bar.

## Setup (Python optimizer)

```bash
# 1. Clone the repo
git clone https://github.com/eduardoamulet/trading-strategies.git
cd trading-strategies

# 2. Create config.py with your Alpaca paper-trading credentials
cp config.example.py config.py
# then edit config.py and paste your keys

# 3. Install dependencies
python -m pip install pandas numpy openpyxl pyarrow alpaca-py tqdm

# 4. Run the QQQ/SPY exhaustive optimization
python -m optimizer.optimize_qqq_spy
```

Free Alpaca paper-trading keys: https://app.alpaca.markets/signup

## Setup (Pine Script in TradingView)

1. Open TradingView, then `Pine Editor` panel.
2. Paste the contents of `pine_indicators/Trend Reversal QQQ & SPY.pine`.
3. Save → `Add to chart`. Switch to a 1-hour chart of QQQ or SPY.
4. For the backtest version, do the same with the `... - BACKTEST.pine` file.
   The backtest will only consider the last 3 years.

## Notes

- `config.py` is **never** committed (it holds API keys). The `config.example.py`
  template shows what fields are required.
- Backtest XLSX exports and Alpaca data caches are git-ignored to keep the
  repo small.
- The optimizer runs against the local Alpaca cache (`data_cache/`); the
  first call to a new ticker downloads ~10k 1H bars and saves them.

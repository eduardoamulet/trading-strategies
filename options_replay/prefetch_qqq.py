"""Pre-fetch 0 DTE options data into the Parquet cache.

Pre-fetches:
- Underlying 1-min bars per trading day
- 0 DTE chain per trading day
- Option 1-min bars for the (2N+1) strikes closest to ATM (per side) per day

Run from options_replay/:
    py prefetch_qqq.py <ticker> <start YYYY-MM-DD> <end YYYY-MM-DD> [strikes_around_atm]

Examples:
    py prefetch_qqq.py QQQ 2025-12-01 2026-05-28 10
    py prefetch_qqq.py SPY 2025-12-01 2026-05-28 10
"""
from __future__ import annotations

import sys
import time as time_mod
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import pandas as pd  # noqa: E402

import config  # type: ignore  # noqa: E402
from adapter_polygon import PolygonAdapter  # noqa: E402
from downloader import Downloader  # noqa: E402

def _print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def main() -> int:
    if len(sys.argv) < 4:
        _print("Usage: py prefetch_qqq.py <ticker> <start YYYY-MM-DD> <end YYYY-MM-DD> [strikes_around_atm]")
        return 1

    ticker = sys.argv[1].upper().strip()
    start_str = sys.argv[2]
    end_str = sys.argv[3]
    strikes_window = int(sys.argv[4]) if len(sys.argv) > 4 else 10

    start = pd.Timestamp(start_str).date()
    end = pd.Timestamp(end_str).date()

    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY), HERE / "data")

    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)

    n_to_fetch = 2 * strikes_window + 1

    _print(f"Pre-fetch {ticker} from {start} to {end} ({len(days)} weekdays)")
    _print(f"Strikes around ATM per side: {n_to_fetch} (ATM + ±{strikes_window})")
    _print(f"Expected API calls/day: ~{2 + 2 * n_to_fetch} = ~{(2 + 2 * n_to_fetch) * len(days)} total")
    _print(f"Rate limit: 100 req/min -> est. time: ~{(2 + 2 * n_to_fetch) * len(days) / 100:.0f} min")
    _print("=" * 78)

    t0 = time_mod.time()
    n_processed = 0
    n_skipped = 0
    n_option_calls = 0
    n_cache_hits = 0

    for i, date_str in enumerate(days):
        # Underlying
        u_path = dl.data_dir / "underlying" / f"{ticker}_{date_str}.parquet"
        u_cached = u_path.exists()
        under = dl.underlying(ticker, date_str)
        if under.empty:
            n_skipped += 1
            elapsed_min = (time_mod.time() - t0) / 60
            _print(f"[{i+1:3d}/{len(days)}] {date_str}: market closed/no data | elapsed={elapsed_min:.1f}min")
            continue

        spot_open = float(under.iloc[0]["open"])

        # Chain
        c_path = dl.data_dir / "chain" / f"{ticker}_{date_str}.parquet"
        c_cached = c_path.exists()
        chain = dl.chain(ticker, date_str)
        if chain.empty:
            n_skipped += 1
            _print(f"[{i+1:3d}/{len(days)}] {date_str}: empty 0 DTE chain")
            continue

        calls = chain[chain["contract_type"].str.lower() == "call"]
        puts = chain[chain["contract_type"].str.lower() == "put"]

        calls_sorted = calls.assign(_d=(calls["strike_price"] - spot_open).abs()).sort_values("_d")
        puts_sorted = puts.assign(_d=(puts["strike_price"] - spot_open).abs()).sort_values("_d")

        call_strikes = calls_sorted.head(n_to_fetch)
        put_strikes = puts_sorted.head(n_to_fetch)

        day_new_fetches = 0
        day_cache_hits = 0

        for _, row in call_strikes.iterrows():
            # Usar el ticker real del chain — necesario para índices que tienen
            # OCC root distinto al underlying (ej. NDX → O:NDXP..., SPX → O:SPXW...).
            occ = row.get("ticker") or PolygonAdapter.build_occ(ticker, date_str, "C", row["strike_price"])
            safe = occ.replace(":", "_")
            op_path = dl.data_dir / "options" / f"{safe}_{date_str}.parquet"
            was_cached = op_path.exists()
            dl.option(occ, date_str)
            if was_cached:
                day_cache_hits += 1
            else:
                day_new_fetches += 1
            n_option_calls += 1

        for _, row in put_strikes.iterrows():
            occ = row.get("ticker") or PolygonAdapter.build_occ(ticker, date_str, "P", row["strike_price"])
            safe = occ.replace(":", "_")
            op_path = dl.data_dir / "options" / f"{safe}_{date_str}.parquet"
            was_cached = op_path.exists()
            dl.option(occ, date_str)
            if was_cached:
                day_cache_hits += 1
            else:
                day_new_fetches += 1
            n_option_calls += 1

        n_cache_hits += day_cache_hits
        n_processed += 1

        elapsed_min = (time_mod.time() - t0) / 60
        days_left = len(days) - (i + 1)
        eta_min = elapsed_min / (i + 1) * days_left if (i + 1) > 0 else 0

        _print(f"[{i+1:3d}/{len(days)}] {date_str} spot=${spot_open:.2f} "
              f"u_cached={u_cached} c_cached={c_cached} "
              f"opt_new={day_new_fetches} opt_cached={day_cache_hits} "
              f"| elapsed={elapsed_min:.1f}m eta={eta_min:.1f}m")

    elapsed_min = (time_mod.time() - t0) / 60
    _print("=" * 78)
    _print(f"Done in {elapsed_min:.1f} min")
    _print(f"Processed: {n_processed} | Skipped: {n_skipped}")
    _print(f"Option calls total: {n_option_calls} (cache hits: {n_cache_hits}, new fetches: {n_option_calls - n_cache_hits})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

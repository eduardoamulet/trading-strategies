"""Quick end-to-end Polygon validation.

Runs: list expirations -> chain -> underlying -> select 1-ITM CALL+PUT -> option 1-min bars.
Prints what works and what doesn't.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from adapter_polygon import PolygonAdapter
from downloader import Downloader
from engine import select_itm_strikes

import config  # type: ignore


TEST_TICKER = "SPY"
TEST_DATE = "2026-05-27"  # known closed session


def main() -> int:
    print(f"Polygon smoke test — {TEST_TICKER} on {TEST_DATE}")
    print("=" * 60)

    adapter = PolygonAdapter(config.POLYGON_API_KEY)
    dl = Downloader(adapter, HERE / "data")

    # 1. Underlying
    print("[1/5] Underlying minute bars...", end=" ")
    under = dl.underlying(TEST_TICKER, TEST_DATE)
    if under.empty:
        print("FAIL — empty")
        return 1
    print(f"OK ({len(under)} bars, first close=${under.iloc[0]['close']:.2f}, last=${under.iloc[-1]['close']:.2f})")

    # 2. Expirations
    print("[2/5] List expirations >= test date...", end=" ")
    exps = adapter.list_expirations(TEST_TICKER, TEST_DATE)
    if not exps:
        print("FAIL — empty")
        return 1
    print(f"OK ({len(exps)} expirations, first={exps[0]})")

    if exps[0] != TEST_DATE:
        print(f"  ! 0 DTE not available on {TEST_DATE}; using nearest expiry {exps[0]}")
    expiry = exps[0]

    # 3. Chain
    print(f"[3/5] Chain for expiry={expiry}...", end=" ")
    chain = dl.chain(TEST_TICKER, expiry)
    if chain.empty:
        print("FAIL — empty")
        return 1
    print(f"OK ({len(chain)} contracts, "
          f"{(chain['contract_type'].str.lower()=='call').sum()} calls / "
          f"{(chain['contract_type'].str.lower()=='put').sum()} puts)")

    # 4. Strike selection
    spot = float(under.iloc[0]["close"])
    print(f"[4/5] Selecting 1-ITM strikes for spot=${spot:.2f}...", end=" ")
    call_row, put_row = select_itm_strikes(chain, spot)
    if call_row is None or put_row is None:
        print("FAIL — no strikes")
        return 1
    print(f"OK (CALL strike={call_row['strike_price']}, PUT strike={put_row['strike_price']})")

    call_occ = PolygonAdapter.build_occ(TEST_TICKER, expiry, "C", call_row["strike_price"])
    put_occ = PolygonAdapter.build_occ(TEST_TICKER, expiry, "P", put_row["strike_price"])
    print(f"     CALL OCC: {call_occ}")
    print(f"     PUT  OCC: {put_occ}")

    # 5. Option bars
    print("[5/5] Option 1-min bars...")
    df_c = dl.option(call_occ, TEST_DATE)
    print(f"     CALL: {len(df_c)} bars" + (f" (first ${df_c.iloc[0]['close']:.2f})" if len(df_c) else " EMPTY"))
    df_p = dl.option(put_occ, TEST_DATE)
    print(f"     PUT : {len(df_p)} bars" + (f" (first ${df_p.iloc[0]['close']:.2f})" if len(df_p) else " EMPTY"))

    if df_c.empty or df_p.empty:
        print("\nFAIL — at least one option series empty. Likely a plan/tier limitation.")
        return 2

    print("\nAll OK — Polygon tier supports the full pipeline.")
    print(f"Cache written under: {(HERE / 'data').resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

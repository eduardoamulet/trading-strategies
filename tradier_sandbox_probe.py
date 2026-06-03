"""Probe Tradier sandbox to verify what's needed for the 0 DTE options analysis.

Tests, in order:
  1. Auth + basic quote (SPY)
  2. Options expirations for SPY (do we see today / weekly?)
  3. Options chain for nearest expiration (can we read greeks/strikes?)
  4. Pick one ATM CALL → fetch its quote (live snapshot)
  5. Timesales 1-min for that contract (THE critical one — needed for max_value)

Stops at first failure to keep output focused.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, date, timedelta
from typing import Any

import requests

import config

HEADERS = {
    "Authorization": f"Bearer {config.TRADIER_ACCESS_TOKEN}",
    "Accept": "application/json",
}
BASE = config.TRADIER_BASE_URL.rstrip("/")


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def get(path: str, params: dict[str, Any] | None = None) -> dict:
    url = f"{BASE}{path}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    print(f"  GET {url}")
    if params:
        print(f"      params={params}")
    print(f"      status={r.status_code}")
    if r.status_code != 200:
        print(f"      body={r.text[:400]}")
        sys.exit(1)
    return r.json()


# ---------------------------------------------------------------------------
banner("1. AUTH + QUOTE (SPY)")
data = get("/v1/markets/quotes", {"symbols": "SPY"})
q = data.get("quotes", {}).get("quote") or {}
spy_last = q.get("last")
print(f"  SPY last={spy_last}  bid={q.get('bid')}  ask={q.get('ask')}  ts={q.get('trade_date')}")
if spy_last is None:
    print("  ! No price returned — sandbox may be returning stale/empty data.")
    sys.exit(1)

# ---------------------------------------------------------------------------
banner("2. OPTIONS EXPIRATIONS (SPY)")
data = get("/v1/markets/options/expirations", {"symbol": "SPY", "includeAllRoots": "true"})
exps = data.get("expirations", {}).get("date") or []
if isinstance(exps, str):
    exps = [exps]
print(f"  Got {len(exps)} expirations. First 8: {exps[:8]}")
if not exps:
    print("  ! No expirations returned.")
    sys.exit(1)

today_str = date.today().isoformat()
has_today = today_str in exps
print(f"  Today ({today_str}) in expirations? {has_today}  -- need True for 0 DTE")
target_exp = today_str if has_today else exps[0]
print(f"  Using target expiration: {target_exp}")

# ---------------------------------------------------------------------------
banner("3. OPTIONS CHAIN (SPY, target_exp)")
data = get("/v1/markets/options/chains", {"symbol": "SPY", "expiration": target_exp, "greeks": "true"})
opts = data.get("options", {}).get("option") or []
calls = [o for o in opts if o.get("option_type") == "call"]
puts  = [o for o in opts if o.get("option_type") == "put"]
print(f"  Total contracts: {len(opts)}  (calls={len(calls)}, puts={len(puts)})")
if not calls:
    print("  ! No calls returned.")
    sys.exit(1)

# ITM CALL = highest strike strictly below spot
itm_calls = sorted([c for c in calls if c.get("strike") and c["strike"] < spy_last], key=lambda x: -x["strike"])
itm_puts  = sorted([p for p in puts  if p.get("strike") and p["strike"] > spy_last], key=lambda x:  x["strike"])
if not itm_calls or not itm_puts:
    print("  ! Could not find both an ITM call and ITM put (strikes may be coarse).")
    print(f"     spy_last={spy_last}, sample call strikes={[c['strike'] for c in calls[:5]]}")
    sys.exit(1)

best_call = itm_calls[0]
best_put  = itm_puts[0]
print(f"  Closest-ITM CALL: {best_call['symbol']}  strike={best_call['strike']}  "
      f"bid={best_call.get('bid')} ask={best_call.get('ask')} delta={best_call.get('greeks',{}).get('delta')}")
print(f"  Closest-ITM PUT : {best_put['symbol']}  strike={best_put['strike']}  "
      f"bid={best_put.get('bid')} ask={best_put.get('ask')} delta={best_put.get('greeks',{}).get('delta')}")

# ---------------------------------------------------------------------------
banner("4. LIVE QUOTE for chosen CALL")
sym = best_call["symbol"]
data = get("/v1/markets/quotes", {"symbols": sym})
q = data.get("quotes", {}).get("quote") or {}
print(f"  {sym}  bid={q.get('bid')}  ask={q.get('ask')}  last={q.get('last')}  vol={q.get('volume')}  oi={q.get('open_interest')}")

# ---------------------------------------------------------------------------
banner("5. TIMESALES 1-min  (THE CRITICAL TEST)")
# Try yesterday (if today is closed expiration that already happened, also OK)
y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
start = f"{y} 09:30"
end   = f"{y} 16:00"
data = get("/v1/markets/timesales", {
    "symbol": sym,
    "interval": "1min",
    "start": start,
    "end":   end,
    "session_filter": "open",
})
ts = data.get("series", {}).get("data") or []
if isinstance(ts, dict):
    ts = [ts]
print(f"  Bars returned for {y}: {len(ts)}")
if ts:
    first = ts[0]; last = ts[-1]
    print(f"  First: {first.get('time')}  o={first.get('open')} h={first.get('high')} l={first.get('low')} c={first.get('close')}")
    print(f"  Last : {last.get('time')}  o={last.get('open')} h={last.get('high')} l={last.get('low')} c={last.get('close')}")
    print()
    print("  [OK] Sandbox returns intraday minute data for options. Plan B is feasible.")
else:
    print()
    print("  [FAIL] Sandbox returned 0 bars. Two likely causes:")
    print("     a) Sandbox does not provide options minute history (common limitation).")
    print("     b) Yesterday was non-trading or the contract wasn't traded.")
    print("  -> Will need a Tradier production token with market data plan.")

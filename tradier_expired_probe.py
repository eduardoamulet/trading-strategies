"""Probe whether sandbox has minute-history for ALREADY-EXPIRED 0 DTE contracts.

For the analysis we need contracts that expired on each of the past 5 trading days.
Test: pick a recent past trading day, build a 0DTE OCC symbol that expired that day,
query timesales 1-min. If we get bars -> plan is feasible. If 0 bars -> sandbox lacks
expired contract history and we need production data.
"""
from __future__ import annotations
import requests, sys
from datetime import datetime, timedelta
import config

H = {"Authorization": f"Bearer {config.TRADIER_ACCESS_TOKEN}", "Accept": "application/json"}
B = config.TRADIER_BASE_URL.rstrip("/")


def occ(symbol: str, expiry: datetime, opt_type: str, strike: float) -> str:
    """Build OCC option symbol: SPY260506C00731000"""
    yymmdd = expiry.strftime("%y%m%d")
    cp = "C" if opt_type.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{symbol}{yymmdd}{cp}{strike_int:08d}"


def get(path: str, params: dict) -> dict:
    r = requests.get(f"{B}{path}", headers=H, params=params, timeout=20)
    print(f"  GET {path} params={params} -> {r.status_code}")
    if r.status_code != 200:
        print(f"      body={r.text[:300]}")
    return r.json() if r.status_code == 200 else {}


# Past 5 weekdays (skip weekends)
def past_weekdays(n: int) -> list[datetime]:
    out, d = [], datetime.now().date() - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:  # Mon-Fri
            out.append(datetime.combine(d, datetime.min.time()))
        d -= timedelta(days=1)
    return out


past = past_weekdays(5)
print("Past 5 weekdays to test (each as a 0 DTE expiry):")
for p in past:
    print(f"  {p.strftime('%Y-%m-%d (%a)')}")
print()

# We need spot near each past day. Quick approach: use today's spot as a rough strike,
# then query expirations to find the actual expirations available, and only test ones
# that are in the available list.
print("Available SPY expirations (sandbox):")
data = get("/v1/markets/options/expirations", {"symbol": "SPY"})
exps = data.get("expirations", {}).get("date") or []
if isinstance(exps, str):
    exps = [exps]
print(f"  {len(exps)} expirations, first 12: {exps[:12]}")
print(f"  Any past expirations in the list? "
      f"{[e for e in exps if e < datetime.now().strftime('%Y-%m-%d')]}")
print()

# Use today's SPY ATM strike as a proxy
data = get("/v1/markets/quotes", {"symbols": "SPY"})
spy_last = data.get("quotes", {}).get("quote", {}).get("last")
print(f"SPY last = {spy_last}, using strike {round(spy_last)} for tests")
print()

results = []
for past_day in past:
    sym = occ("SPY", past_day, "call", round(spy_last))
    print(f"Testing {sym} (0 DTE that would have expired {past_day.strftime('%Y-%m-%d')})")
    data = get("/v1/markets/timesales", {
        "symbol": sym,
        "interval": "1min",
        "start": past_day.strftime("%Y-%m-%d") + " 09:30",
        "end":   past_day.strftime("%Y-%m-%d") + " 16:00",
        "session_filter": "open",
    })
    series = data.get("series", {}).get("data") or []
    if isinstance(series, dict):
        series = [series]
    bars = len(series)
    print(f"  bars returned: {bars}")
    if series:
        print(f"  first: {series[0].get('time')} c={series[0].get('close')}")
        print(f"  last : {series[-1].get('time')} c={series[-1].get('close')}")
    results.append((past_day, sym, bars))
    print()

print("=" * 60)
print("SUMMARY")
print("=" * 60)
ok = sum(1 for _, _, b in results if b > 0)
for d, s, b in results:
    flag = "[OK]" if b > 0 else "[FAIL]"
    print(f"  {flag} {d.strftime('%Y-%m-%d')}  {s}  bars={b}")
print()
if ok == 5:
    print("[OK] Sandbox HAS minute history for all 5 past expired contracts.")
    print("     Plan B is fully feasible with current sandbox token.")
elif ok > 0:
    print(f"[PARTIAL] Only {ok}/5 days returned bars. Sandbox may have a rolling")
    print("     window. Check which days returned 0 bars.")
else:
    print("[FAIL] Sandbox returns NO minute history for already-expired 0DTE contracts.")
    print("     Need Tradier production token + market data plan.")

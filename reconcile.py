"""Reconciliación BACKTEST (Polygon) vs LIVE (Tradier) para un ticker, para el
vencimiento 0DTE más cercano. Valida los SUPUESTOS del backtest contra el mercado real:

  - ¿mismo strike elige cada fuente?
  - 'open' (lo que el backtest ASUME como precio de entrada) vs 'ask' (lo que pagás
    REAL al comprar) → el "gap de optimismo" del backtest.
  - spread Polygon vs spread Tradier (¿coinciden? ¿ambos pasan el gate?).

Pensado para correr con MERCADO ABIERTO (ambas fuentes ~ahora). Con mercado cerrado
igual corre: Tradier da el último quote conocido y Polygon el bar del horario pedido.

Correr DESDE LA RAÍZ del repo (evita la colisión secrets.py/numpy):
    py reconcile.py [TICKER] [CALL|PUT] [HH:MM] [YYYY-MM-DD]
    py reconcile.py SPY CALL 15:55
"""
import pandas as pd            # ANTES de tocar el path (evita la colisión numpy/secrets.py)
import sys
import pathlib
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "options_replay"))   # adapter_polygon, downloader
sys.path.insert(0, str(ROOT))                       # config, strategy_core

import config                                        # type: ignore  # noqa: E402
import strategy_core                                 # noqa: E402
from adapter_polygon import PolygonAdapter           # noqa: E402
from downloader import Downloader                     # noqa: E402

sys.path.append(str(ROOT / "live_trader"))           # al final: no tapa nada
import settings                                       # noqa: E402
from brokers.tradier import TradierAdapter            # noqa: E402
from core.selector import ContractSelector            # noqa: E402


def _bar_open_at(df: pd.DataFrame, ts: pd.Timestamp):
    """Devuelve (precio, bar_ts, nota). 'open' del PRIMER bar >= ts (igual que el engine:
    primer bar de la ventana). Si NO hay bar en/después de ts, el engine SALTEA el strike
    → devolvemos (None, último_bar_ts, 'skip'). nota='lejano' si el bar quedó a >5 min."""
    if df is None or df.empty:
        return None, None, "sin bars"
    after = df[df["timestamp"] >= ts]
    if after.empty:
        return None, df.iloc[-1]["timestamp"], "skip"
    row = after.iloc[0]
    nota = "lejano" if (row["timestamp"] - ts) > pd.Timedelta(minutes=5) else ""
    return float(row["open"]), row["timestamp"], nota


def main() -> int:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper().strip()
    side = (sys.argv[2] if len(sys.argv) > 2 else "CALL").upper().strip()
    hhmm = sys.argv[3] if len(sys.argv) > 3 else "10:00"
    right = "C" if side == "CALL" else "P"

    # ---------- LIVE (Tradier) ----------
    broker = TradierAdapter()
    spot_t = broker.get_underlying_price(ticker)
    expiry = broker.nearest_expiry(ticker)
    chain = broker.get_option_chain(ticker, expiry)
    contract = ContractSelector().select(chain, side, spot_t)   # mismo selector que live
    strike = contract.strike
    ask_t, bid_t, spread_t = contract.ask, contract.bid, contract.spread

    # fecha del backtest = el vencimiento que eligió Tradier (0DTE si hoy es hábil).
    date = sys.argv[4] if len(sys.argv) > 4 else expiry
    entry_ts = pd.Timestamp(f"{date} {hhmm}", tz="America/New_York")

    # ---------- BACKTEST (Polygon), MISMO strike/fecha ----------
    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY), ROOT / "options_replay" / "data")
    occ_p = PolygonAdapter.build_occ(ticker, date, right, strike)
    spot_p = open_p = bar_ts = bid_p = ask_p = sp_p = None
    open_note = ""
    try:
        spot_p, _, _ = _bar_open_at(dl.underlying(ticker, date), entry_ts)
    except Exception as e:
        print(f"[polygon] sin underlying para {date}: {e}")
    try:
        open_p, bar_ts, open_note = _bar_open_at(dl.option(occ_p, date), entry_ts)
        q = dl.option_quote(occ_p, date, entry_ts)   # NBBO al ts (precio tradeable, no el trade)
        bid_p, ask_p, sp_p = q.get("bid"), q.get("ask"), q.get("spread")
    except Exception as e:
        print(f"[polygon] sin data de opción {occ_p} para {date}: {e}")

    def _gate(spot, spread):
        if spot is None or spread is None:
            return "—"
        mx = strategy_core.max_spread_for_price(spot, settings.SPREAD_BUCKETS)
        return f"✓ (≤${mx:.2f})" if spread <= mx else f"✗ (>${mx:.2f})"

    def _f(x, p="$%.2f"):
        return (p % x) if x is not None else "—"

    print("\n" + "=" * 68)
    print(f" Reconciliación {ticker} {side} · {date} {hhmm} ET · strike {strike:g}")
    print("=" * 68)
    print(f"{'':20}{'Polygon (backtest)':<26}{'Tradier (live)'}")
    print(f"{'spot':20}{_f(spot_p):<26}{_f(spot_t)}")
    print(f"{'NBBO bid/ask':20}{(_f(bid_p)+' / '+_f(ask_p)):<26}{_f(bid_t)+' / '+_f(ask_t)}")
    print(f"{'spread':20}{(_f(sp_p)+'  '+_gate(spot_p, sp_p)):<26}{_f(spread_t)+'  '+_gate(spot_t, spread_t)}")
    _bts = bar_ts.strftime('%H:%M') if bar_ts is not None else '—'
    if open_p is not None:
        _bar_lbl = f"{_f(open_p)}  (bar {_bts}{', LEJANO' if open_note == 'lejano' else ''})"
    else:
        _bar_lbl = f"— (sin trade a las {hhmm}; último bar {_bts} → el backtest saltearía)"
    print(f"{'backtest entra@':20}{_bar_lbl}")
    print("-" * 68)

    # ---------- validaciones ----------
    print(" Validaciones:")
    if ask_p is not None and ask_t is not None:
        d = abs(ask_p - ask_t)
        ok = "✓ coinciden" if d <= 0.03 else "⚠ difieren (Tradier sandbox tiene delay 15min)"
        print(f" • fuentes de precio: ask Polygon {_f(ask_p)} vs ask Tradier {_f(ask_t)} "
              f"→ {ok} (Δ {_f(d)})")
    if spot_p is not None and spot_t is not None and abs(spot_p - spot_t) > 0.5:
        print(f" • spot: Polygon {_f(spot_p)} vs Tradier {_f(spot_t)} (Δ {_f(abs(spot_p-spot_t))}) "
              f"→ ⚠ el sandbox de Tradier viene desfasado.")
    if open_p is None and ask_p is not None:
        print(f" • el backtest NO entraría este strike a las {hhmm} (no hubo trade); el NBBO")
        print(f"   ask tradeable era {_f(ask_p)}. Lección: para 0DTE illiquid, entrar al ASK")
        print(f"   (no al 'open' del último trade) y validar liquidez en el minuto de entrada.")
    elif open_p is not None and ask_p is not None and ask_p > 0:
        gpct = (open_p - ask_p) / ask_p * 100
        print(f" • optimismo del backtest: entra al 'open' {_f(open_p)} vs ask {_f(ask_p)} "
              f"→ {gpct:+.0f}% ({'optimista' if open_p < ask_p else 'conservador'}).")
    if sp_p is not None and spread_t is not None:
        print(f" • spread: Polygon {_f(sp_p)} vs Tradier {_f(spread_t)} (Δ {_f(abs(sp_p-spread_t))})")
    print("=" * 68)
    print(" (Solo lectura — no se colocó ninguna orden. Más fiel con MERCADO ABIERTO.)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

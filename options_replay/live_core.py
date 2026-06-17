"""Lógica PURA del dashboard de operar en vivo (sin Streamlit) — testeable de forma aislada.

Construye candidatos, la tabla de cadena, compra (vía el puerto Broker) y marca la posición.
Todo sobre los puertos `MarketData`/`Broker` de trading_core → corre igual con datos de
Replay (Polygon) o de Tradier sandbox. La UI (live_app.py) solo orquesta + renderiza.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from trading_core.domain import Contract, OrderRequest, OrderSide, Right
from trading_core.selection import SelectionParams, _best_leg_at, make_range_gate

# Tipo de operación → piernas necesarias (v1: las que ya soporta el dominio).
RIGHTS: Dict[str, tuple] = {
    "CALL y PUT": (Right.CALL, Right.PUT),
    "CALL y PUT (Refuerzo)": (Right.CALL, Right.PUT),
    "Sólo CALL": (Right.CALL,),
    "Sólo PUT": (Right.PUT,),
}


def gate_and_sel(p: dict):
    params = SelectionParams(premium_min=p["pmin"], premium_max=p["pmax"], window_min=0)
    return params, make_range_gate(p["pmin"], p["pmax"], p["max_spread"])


def candidates(market, ticker: str, expiry: str, now: Any, p: dict) -> Dict[Right, Optional[Contract]]:
    """Contrato candidato por pierna (lo que el sistema compraría AHORA) vía la selección
    de trading_core. {Right: Contract|None}."""
    params, gate = gate_and_sel(p)
    return {r: _best_leg_at(market, ticker, expiry, r, now, params, gate)
            for r in RIGHTS.get(p["tipo"], (Right.CALL, Right.PUT))}


def chain_df(market, ticker: str, expiry: str, now: Any, cand: dict, n: int = 8):
    """Tabla de la cadena cerca de ATM: strike | CALL bid/ask | PUT bid/ask + marca candidato.
    Devuelve (DataFrame, spot)."""
    spot = market.underlying_price(ticker, now)
    chain = market.chain(ticker, expiry, now)
    if not chain or spot is None:
        return pd.DataFrame(), spot
    strikes = sorted(sorted({c.strike for c in chain}, key=lambda k: abs(k - spot))[:2 * n + 1])
    by = {(c.right, c.strike): c for c in chain}
    cand_occ = {cand[r].occ for r in cand if cand.get(r)}
    nearest = min(strikes, key=lambda s: abs(s - spot)) if strikes else None
    rows = []
    for k in strikes:
        cc, pc = by.get((Right.CALL, k)), by.get((Right.PUT, k))
        cq = market.quote(cc.occ, now) if cc else None
        pq = market.quote(pc.occ, now) if pc else None
        mark = ("✅C" if (cc and cc.occ in cand_occ) else "") + ("✅P" if (pc and pc.occ in cand_occ) else "")
        rows.append({"✓": mark,
                     "C bid": cq.bid if cq else None, "C ask": cq.ask if cq else None,
                     "Strike": k,
                     "P bid": pq.bid if pq else None, "P ask": pq.ask if pq else None,
                     "ATM": "◀" if k == nearest else ""})
    return pd.DataFrame(rows), spot


def invest_split(p: dict) -> Dict[Right, float]:
    """Inversión por pierna según el Tipo (single-leg = 100% a esa pierna; dual = call_pct)."""
    rights = RIGHTS.get(p["tipo"], (Right.CALL, Right.PUT))
    if len(rights) == 1:
        return {rights[0]: p["inversion"]}
    return {Right.CALL: p["inversion"] * p["call_pct"] / 100.0,
            Right.PUT: p["inversion"] * (1 - p["call_pct"] / 100.0)}


def buy_proposal(broker, market, now: Any, ticker: str, expiry: str, p: dict, cand: dict) -> dict:
    """Compra (paper) los candidatos por el puerto Broker. Devuelve el dict de posición."""
    invest = invest_split(p)
    legs = []
    for r in RIGHTS.get(p["tipo"], (Right.CALL, Right.PUT)):
        c = cand.get(r)
        if c is None or c.quote is None or not c.quote.ask:
            raise RuntimeError(f"sin candidato {r.value} válido para comprar")
        qty = max(1, int(invest[r] // (c.quote.ask * 100)))
        f = broker.execute(OrderRequest(c.occ, OrderSide.BUY, qty, ts=now), now)
        legs.append({"occ": c.occ, "right": r.value, "strike": c.strike,
                     "qty": qty, "entry_price": f.price, "entry_ts": str(now)})
    return {"ticker": ticker, "expiry": expiry, "tipo": p["tipo"],
            "umbral": p["umbral"], "stop": p["stop"], "entry_ts": str(now), "legs": legs,
            "invest": {r.value: v for r, v in invest.items()},        # para re-invertir al reforzar
            "refuerzo_loss": float(p.get("refuerzo_loss", 50.0)),
            "refuerzo_max": int(p.get("refuerzo_max", 0)),
            "reinforcements": []}


def reinforce_candidate(pos: dict, m: dict) -> Optional[Right]:
    """¿Qué pierna reforzar AHORA (martingala)? La que MÁS pierde entre las elegibles:
    ROI propio <= -refuerzo_loss, mark > penny, y bajo el tope total. None si ninguna."""
    if len(pos.get("reinforcements", [])) >= int(pos.get("refuerzo_max", 0)):
        return None
    loss = float(pos.get("refuerzo_loss", 50.0))
    cands = []
    for r in (Right.CALL, Right.PUT):
        d = m["per"].get(r)
        if not d or d["cost"] <= 0 or d["bid"] <= 0.01:
            continue
        if d["pct"] <= -loss:
            cands.append((r, d["pct"]))
    return min(cands, key=lambda x: x[1])[0] if cands else None


def reinforce(broker, market, now: Any, pos: dict, right: Right) -> dict:
    """Compra (paper) otra tranche de la pierna `right` (MISMO contrato) al ask, re-invirtiendo
    el capital original de esa pierna. Muta `pos` (agrega la tranche + el evento). Devuelve el
    evento de refuerzo para el log de operaciones."""
    base = next(l for l in pos["legs"] if l["right"] == right.value)
    ask = market.quote(base["occ"], now).ask
    if not ask or ask <= 0:
        raise RuntimeError(f"sin ask para reforzar {right.value}")
    qty = max(1, int(float(pos.get("invest", {}).get(right.value, 0.0)) // (ask * 100)))
    f = broker.execute(OrderRequest(base["occ"], OrderSide.BUY, qty, ts=now), now)
    pos["legs"].append({"occ": base["occ"], "right": right.value, "strike": base["strike"],
                        "qty": qty, "entry_price": f.price, "entry_ts": str(now)})
    ev = {"ts": str(now), "right": right.value, "price": f.price, "qty": qty}
    pos.setdefault("reinforcements", []).append(ev)
    return ev


def mark(market, pos: dict, now: Any) -> dict:
    """Marca la posición al BID. Devuelve $/% por pierna, total, combined (CALL%+PUT%), capital."""
    per = {}
    for r in (Right.CALL, Right.PUT):
        rlegs = [l for l in pos["legs"] if l["right"] == r.value]
        if not rlegs:
            continue
        q = market.quote(rlegs[0]["occ"], now)
        bid = q.bid if (q and q.bid is not None) else rlegs[0]["entry_price"]
        cost = sum(l["qty"] * l["entry_price"] * 100 for l in rlegs)
        value = sum(l["qty"] * bid * 100 for l in rlegs)
        per[r] = {"cost": cost, "value": value, "bid": bid,
                  "pnl": value - cost, "pct": ((value - cost) / cost * 100) if cost else 0.0}
    tot_cost = sum(d["cost"] for d in per.values())
    pnl = sum(d["value"] for d in per.values()) - tot_cost
    return {"per": per, "cost": tot_cost, "pnl": pnl,
            "roi": (pnl / tot_cost * 100) if tot_cost else 0.0,
            "combined": sum(d["pct"] for d in per.values()),
            "capital": tot_cost + pnl}

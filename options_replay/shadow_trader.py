"""Shadow trader (Fase 1 del plan de integración con broker) — decide SIN operar.

Cada día hábil a las 09:31 ET este script recorre el ciclo de decisión y REGISTRA el
resultado en data/shadow_trader.db sin mandar ninguna orden: selección de contrato con
datos EN VIVO de Alpaca (menor spread en rango óptimo, misma `trading_core.selection`
del paper) → tamaño de la posición → precios de entrada reales. Riesgo cero.

POR DEFECTO entra TODOS los días hábiles (máxima recolección de datos de calibración).
Con el checkbox «Usar el playbook» (Operar → 🕶, persiste en data/shadow_config.json)
replica la política del «(playbook automático)»: solo días OPERAR + estado operable
del veredicto vigente; el resto se saltea con su motivo. A las 15:50 ET (--eod) captura el bid de cierre de los contratos
elegidos → P&L hipotético de aguantar al cierre.

Comparación (--reporte): las decisiones registradas + el P&L hipotético. La
comparación fina contra lo que el BACKTEST dice del mismo día (¿mismo contrato?,
¿mismo ask?) llega en Fase 1.5, cruzando con el almacén del playbook.

Modos:
    py shadow_trader.py             → decidir y registrar (09:31, tarea programada)
    py shadow_trader.py --eod       → completar bids de cierre del día (15:50)
    py shadow_trader.py --reporte 10 → últimas decisiones con P&L hipotético

Sin Streamlit. Seguro de correr a mano las veces que quieras: PK (fecha, ticker)
con INSERT OR REPLACE en la decisión del día (la última corrida del día manda).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent          # options_replay/
ROOT = HERE.parent                              # Traiding/
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DB_PATH = HERE / "data" / "shadow_trader.db"
CONFIG_PATH = HERE / "data" / "shadow_config.json"
TICKERS = ["QQQ", "SPY", "IWM"]
INVERSION = 1000.0                              # $ por ticker (50/50 CALL/PUT)
WINDOW_MIN = 4.0                                # ventana de búsqueda de contrato (min)
MAX_SPREAD = 0.10                               # tope de spread de la compuerta
_WD_ES = {0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue", 4: "Vie", 5: "Sáb", 6: "Dom"}


# ── Almacén ───────────────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS shadow_decisions (
        fecha TEXT NOT NULL, ticker TEXT NOT NULL, hora TEXT, weekday TEXT,
        decision TEXT NOT NULL,          -- 'entrar' | 'saltear' | 'error'
        motivo TEXT,
        scenario TEXT, combination TEXT,
        spot REAL,
        call_occ TEXT, call_bid REAL, call_ask REAL, call_qty INTEGER,
        put_occ TEXT,  put_bid REAL,  put_ask REAL,  put_qty INTEGER,
        costo_estimado REAL,
        eod_call_bid REAL, eod_put_bid REAL, eod_ts TEXT,
        creado_en TEXT,
        PRIMARY KEY (fecha, ticker))""")
    return con


def _grabar(con: sqlite3.Connection, fila: dict) -> None:
    cols = ",".join(fila)
    con.execute(f"INSERT OR REPLACE INTO shadow_decisions ({cols}) VALUES "
                f"({','.join('?' for _ in fila)})", list(fila.values()))
    con.commit()


def _usar_playbook() -> bool:
    """Config del shadow (data/shadow_config.json — checkbox en Operar → 🕶). Default
    FALSE: entra TODOS los días hábiles (máxima recolección de datos de calibración).
    True: replica la política del «(playbook automático)» (solo OPERAR + operable)."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return bool(cfg.get("usar_playbook", False))
    except Exception:  # noqa: BLE001 — sin config = default
        return False


# ── Decisión desde el playbook vigente ────────────────────────────────────────
def _leer_playbook() -> dict | None:
    try:
        return json.loads((HERE / "data" / "playbook.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — sin playbook: el shadow registra el porqué
        return None


def decidir_dia(pb: dict | None, weekday_es: str) -> tuple[str, str, str, str]:
    """(decision, motivo, scenario, combination) según el veredicto vigente. El shadow
    replica la política del «(playbook automático)»: OPERAR + estado operable (o sin
    estado, playbooks viejos) — todo lo demás se saltea con su motivo."""
    if not pb:
        return "saltear", "sin playbook vigente (data/playbook.json)", "", ""
    comb = str(pb.get("combination") or "")
    dia = (pb.get("per_day") or {}).get(weekday_es) or {}
    if not dia:
        return "saltear", f"el playbook no cubre el día {weekday_es}", "", comb
    rec = str(dia.get("recommendation") or "").upper()
    est = dia.get("estado")
    esc = str(dia.get("scenario") or "")
    if rec != "OPERAR":
        return "saltear", f"{weekday_es} = NO OPERAR ({dia.get('reason') or 'veredicto'})", esc, comb
    if est not in (None, "operable"):
        return "saltear", f"{weekday_es} pasa el gate pero estado={est}", esc, comb
    return "entrar", f"{weekday_es} OPERAR + operable (esc {esc})", esc, comb


def _rango_prima(ticker: str) -> tuple[float, float]:
    """Rango óptimo de prima del ticker (ticker_info.json, en centavos → ÷100).
    Mismo fallback que el engine: $0.30–$0.50."""
    try:
        info = json.loads((HERE / "ticker_info.json").read_text(encoding="utf-8"))
        t = info.get(ticker.upper()) or {}
        lo = float(t.get("rango_optimo_lo") or t.get("min") or 30.0) / 100.0
        hi = float(t.get("rango_optimo_hi") or t.get("max") or 50.0) / 100.0
        return lo, hi
    except Exception:  # noqa: BLE001
        return 0.30, 0.50


def _qty(usd: float, ask: float) -> int:
    return int(usd // (ask * 100.0)) if ask and ask > 0 else 0


def _es_dia_habil(fecha_iso: str) -> bool:
    from ucbatch.runner import trading_days
    return fecha_iso in trading_days(fecha_iso, fecha_iso)


# ── Modo principal: decidir y registrar ───────────────────────────────────────
def correr_decision() -> int:
    import pandas as pd
    now = pd.Timestamp.now(tz="America/New_York")
    fecha, hora = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    wd = _WD_ES.get(now.weekday(), "?")
    if not _es_dia_habil(fecha):
        print(f"[shadow] {fecha} no es día hábil NYSE — nada que decidir.")
        return 0

    if _usar_playbook():
        pb = _leer_playbook()
        decision, motivo, esc, comb = decidir_dia(pb, wd)
    else:
        # Default: SIN playbook — se entra todos los días hábiles (el gate estadístico se
        # aplica después, al ANALIZAR los datos; acá se recolecta todos los días).
        decision, motivo, esc, comb = ("entrar", "modo sin playbook: todos los días hábiles",
                                       "", "")
    con = _connect()
    base = {"fecha": fecha, "hora": hora, "weekday": wd, "scenario": esc,
            "combination": comb, "creado_en": now.isoformat()}

    if decision != "entrar":
        for tk in TICKERS:
            _grabar(con, {**base, "ticker": tk, "decision": "saltear", "motivo": motivo})
        print(f"[shadow] {fecha} ({wd}): SALTEAR los {len(TICKERS)} tickers — {motivo}")
        return 0

    # Día operable → seleccionar contratos con datos EN VIVO de Alpaca (paper).
    try:
        from trading_core.adapters.clocks import LiveClock
        from trading_core.live_runner import build_alpaca_ports
        from trading_core.selection import SelectionParams, make_range_gate, select_straddle
        market, _broker = build_alpaca_ports()
    except Exception as e:  # noqa: BLE001
        for tk in TICKERS:
            _grabar(con, {**base, "ticker": tk, "decision": "error",
                          "motivo": f"Alpaca no disponible: {type(e).__name__}: {e}"})
        print(f"[shadow] ERROR conectando a Alpaca: {e}")
        return 1

    rc = 0
    for tk in TICKERS:
        try:
            expiry = market.nearest_expiry(tk, fecha) or fecha
            lo, hi = _rango_prima(tk)
            params = SelectionParams(premium_min=lo, premium_max=hi,
                                     window_min=WINDOW_MIN)
            gate = make_range_gate(lo, hi, MAX_SPREAD)
            clock = LiveClock(poll_sec=3.0, tz="America/New_York")
            call, put, t_sel = select_straddle(market, clock, tk, expiry,
                                               pd.Timestamp.now(tz="America/New_York"),
                                               params, gate)
            spot = market.underlying_price(tk, None)
            if call is None or put is None:
                _grabar(con, {**base, "ticker": tk, "decision": "error", "spot": spot,
                              "motivo": f"sin contrato en rango ${lo:.2f}-${hi:.2f} "
                                        f"(ventana {WINDOW_MIN:.0f} min)"})
                print(f"[shadow] {tk}: sin contrato en rango — registrado.")
                continue
            cq, pq = call.quote, put.quote
            c_qty, p_qty = _qty(INVERSION / 2, cq.ask), _qty(INVERSION / 2, pq.ask)
            costo = (c_qty * cq.ask + p_qty * pq.ask) * 100.0
            _grabar(con, {**base, "ticker": tk, "decision": "entrar", "motivo": motivo,
                          "spot": spot,
                          "call_occ": call.occ, "call_bid": cq.bid, "call_ask": cq.ask,
                          "call_qty": c_qty,
                          "put_occ": put.occ, "put_bid": pq.bid, "put_ask": pq.ask,
                          "put_qty": p_qty, "costo_estimado": costo})
            print(f"[shadow] {tk}: CALL {call.occ} ask {cq.ask:.2f} ×{c_qty} | "
                  f"PUT {put.occ} ask {pq.ask:.2f} ×{p_qty} | costo ~${costo:,.0f}")
        except Exception as e:  # noqa: BLE001 — un ticker no tumba a los demás
            rc = 1
            _grabar(con, {**base, "ticker": tk, "decision": "error",
                          "motivo": f"{type(e).__name__}: {e}"})
            print(f"[shadow] {tk}: ERROR {e}")
    return rc


# ── Modo EOD: bid de cierre de los contratos elegidos ────────────────────────
def correr_eod() -> int:
    import pandas as pd
    now = pd.Timestamp.now(tz="America/New_York")
    fecha = now.strftime("%Y-%m-%d")
    con = _connect()
    filas = con.execute(
        "SELECT ticker, call_occ, put_occ FROM shadow_decisions "
        "WHERE fecha=? AND decision='entrar'", (fecha,)).fetchall()
    if not filas:
        print(f"[shadow] {fecha}: sin entradas que cerrar.")
        return 0
    try:
        from trading_core.live_runner import build_alpaca_ports
        market, _ = build_alpaca_ports()
    except Exception as e:  # noqa: BLE001
        print(f"[shadow] ERROR conectando a Alpaca para EOD: {e}")
        return 1
    for tk, c_occ, p_occ in filas:
        try:
            cb = market.quote(c_occ, None).bid if c_occ else None
            pb_ = market.quote(p_occ, None).bid if p_occ else None
            con.execute("UPDATE shadow_decisions SET eod_call_bid=?, eod_put_bid=?, eod_ts=? "
                        "WHERE fecha=? AND ticker=?",
                        (cb, pb_, now.isoformat(), fecha, tk))
            con.commit()
            print(f"[shadow] EOD {tk}: call bid {cb} | put bid {pb_}")
        except Exception as e:  # noqa: BLE001
            print(f"[shadow] EOD {tk}: ERROR {e}")
    return 0


# ── Reporte ───────────────────────────────────────────────────────────────────
def reporte(n_dias: int = 10) -> int:
    con = _connect()
    filas = con.execute(
        "SELECT fecha, weekday, ticker, decision, motivo, scenario, call_occ, call_ask, "
        "call_qty, put_occ, put_ask, put_qty, costo_estimado, eod_call_bid, eod_put_bid "
        "FROM shadow_decisions WHERE fecha >= (SELECT MIN(f) FROM (SELECT DISTINCT fecha AS f "
        "FROM shadow_decisions ORDER BY fecha DESC LIMIT ?)) ORDER BY fecha DESC, ticker",
        (n_dias,)).fetchall()
    if not filas:
        print("[shadow] sin decisiones registradas todavía.")
        return 0
    print(f"{'fecha':<11} {'día':<4} {'tk':<4} {'decisión':<9} "
          f"{'costo':>9} {'P&L cierre':>11}  detalle")
    for (f, wd, tk, dec, mot, esc, cocc, cask, cqty, pocc, pask, pqty,
         costo, ecb, epb) in filas:
        pnl = ""
        if dec == "entrar" and ecb is not None and epb is not None:
            _pnl = ((ecb - (cask or 0)) * (cqty or 0) + (epb - (pask or 0)) * (pqty or 0)) * 100.0
            pnl = f"{_pnl:+,.0f}"
        det = (f"esc {esc} · C {cocc} @{cask} ×{cqty} · P {pocc} @{pask} ×{pqty}"
               if dec == "entrar" else (mot or ""))
        print(f"{f:<11} {wd:<4} {tk:<4} {dec:<9} "
              f"{('$' + format(costo, ',.0f')) if costo else '':>9} {pnl:>11}  {det[:90]}")
    return 0


def _norm_occ(occ) -> str:
    """OCC comparable entre mundos: el backtest guarda estilo Polygon («O:QQQ…»),
    el shadow guarda el OCC plano de Alpaca."""
    s = str(occ or "").strip().upper()
    return s[2:] if s.startswith("O:") else s


# ── Fase 1.5: comparar el shadow (vivo) contra el backtest del MISMO día ─────
def comparar(n_dias: int = 10) -> int:
    """Para cada entrada shadow: ¿el backtest eligió el MISMO contrato ese día/ticker?
    ¿cuánto difiere el ask VIVO del ask cacheado (slippage de datos)? Todas las filas de
    una combinación comparten el contrato por (fecha, ticker) — la selección depende del
    seed, no de las condiciones de salida — así que basta una fila (MIN id). Preferencia:
    la combinación registrada en la decisión; si no hay (modo sin playbook), la primera
    con datos de ese día."""
    import sqlite3
    con = _connect()
    filas = con.execute(
        "SELECT fecha, hora, ticker, combination, call_occ, call_ask, put_occ, put_ask "
        "FROM shadow_decisions WHERE decision='entrar' AND fecha >= (SELECT MIN(f) FROM ("
        "SELECT DISTINCT fecha AS f FROM shadow_decisions ORDER BY fecha DESC LIMIT ?)) "
        "ORDER BY fecha DESC, ticker", (n_dias,)).fetchall()
    if not filas:
        print("[shadow] sin entradas registradas para comparar (decision='entrar').")
        return 0
    bt = sqlite3.connect(str(HERE / "data" / "bt_results.db"))
    bt.execute("PRAGMA busy_timeout=60000")
    print(f"{'fecha':<11} {'hora':<6} {'tk':<4} {'CALL':<5} {'Δask C':>8} "
          f"{'PUT':<5} {'Δask P':>8}  referencia")
    d_c, d_p, m_c, m_p, n_cmp = [], [], 0, 0, 0
    for f, hora, tk, comb, cocc, cask, pocc, pask in filas:
        row = bt.execute(
            "SELECT call_occ, call_entry_prem, put_occ, put_entry_prem, combination "
            "FROM bt_results WHERE fecha=? AND ticker=? "
            "ORDER BY CASE WHEN combination=? THEN 0 ELSE 1 END, combination, id LIMIT 1",
            (f, tk, comb or "")).fetchone()
        if not row:
            print(f"{f:<11} {hora:<6} {tk:<4} backtest pendiente (llega con el job de las 05:00)")
            continue
        b_cocc, b_cprem, b_pocc, b_pprem, b_comb = row
        ok_c = _norm_occ(cocc) == _norm_occ(b_cocc)
        ok_p = _norm_occ(pocc) == _norm_occ(b_pocc)
        n_cmp += 1
        m_c += ok_c
        m_p += ok_p
        dc = ((cask - b_cprem) / b_cprem * 100.0) if (cask and b_cprem) else None
        dp = ((pask - b_pprem) / b_pprem * 100.0) if (pask and b_pprem) else None
        if ok_c and dc is not None:
            d_c.append(dc)
        if ok_p and dp is not None:
            d_p.append(dp)
        _w = "" if str(hora).startswith("09:3") else " ⚠hora≠apertura"
        print(f"{f:<11} {hora:<6} {tk:<4} {'✓' if ok_c else '✗':<5} "
              f"{(f'{dc:+.1f}%' if dc is not None else '—'):>8} "
              f"{'✓' if ok_p else '✗':<5} "
              f"{(f'{dp:+.1f}%' if dp is not None else '—'):>8}  "
              f"{(b_comb or '')[-18:]}{_w}")
    if n_cmp:
        print(f"\ncontratos iguales: CALL {m_c}/{n_cmp} · PUT {m_p}/{n_cmp}"
              + (f" · Δask medio mismo contrato: CALL {sum(d_c) / len(d_c):+.1f}%" if d_c else "")
              + (f" · PUT {sum(d_p) / len(d_p):+.1f}%" if d_p else ""))
        print("Δask = (ask vivo del shadow − prima de entrada del backtest) ÷ backtest. "
              "✗ = eligieron contratos distintos (mirar hora del shadow vs 09:30).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Shadow trader — decide sin operar (Fase 1)")
    ap.add_argument("--eod", action="store_true", help="capturar bids de cierre del día")
    ap.add_argument("--reporte", type=int, nargs="?", const=10, default=None,
                    metavar="N", help="mostrar las decisiones de los últimos N días")
    ap.add_argument("--comparar", type=int, nargs="?", const=10, default=None,
                    metavar="N", help="Fase 1.5: shadow vs backtest de los últimos N días")
    args = ap.parse_args()
    if args.reporte is not None:
        return reporte(args.reporte)
    if args.comparar is not None:
        return comparar(args.comparar)
    if args.eod:
        return correr_eod()
    return correr_decision()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())

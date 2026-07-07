"""estudio_refuerzo_vs_contra.py — Estudio CONTRAFACTUAL: ¿qué conviene hacer cuando la
posición 0DTE va perdiendo más de X%?

Pregunta: en el PRIMER minuto t* donde el drawdown cruza −X% (X ∈ 20/30/40/50/60), comparar
CUATRO ramas corriendo cada una hasta el cierre del estudio (15:55):
  1. hold      — no hacer nada (la posición base de la casa no tiene salida antes del cierre).
  2. reforzar  — comprar $500 MÁS de la pierna perdedora al ASK de t* (patrón martingala del
                 engine: reinvierte el capital inicial de ESA pierna, compra al ask del mismo
                 minuto de la detección — ver _simulate_refuerzo en engine.py).
  3. contra    — comprar $500 de la pierna OPUESTA al ASK de t* (mismo monto que el refuerzo).
  4. cortar    — vender al BID de t* y aceptar la pérdida. Con disparador combinado vende
                 AMBAS piernas; con disparador por pierna vende SOLO la perdedora (la otra
                 sigue hasta el cierre — así las 4 ramas difieren únicamente en la acción
                 sobre la pierna que disparó, y los deltas vs cortar equivalen EXACTOS al
                 caso de pierna única).

Dos niveles de disparador (por separado, un evento por día×ticker×umbral×nivel):
  - combined: el ROI de la posición combinada (50/50, marcada al bid) cruza −X%.
  - leg:      el ROI de UNA pierna cruza −X% (la que más pierde si cruzan juntas — mismo
              criterio de la martingala del engine).

Posición base y fills = los de la casa (NO se toca engine.py; se REUSA su selección y sus
series): CALL y PUT 0DTE, $1,000 50/50, entrada 09:30 (ventana de búsqueda 4 min, como el
walk-forward), contrato «Menor spread en rango óptimo» (selection_criterion="spread" +
rango de prima por ticker de ticker_info.json), fills Fase 2 (nbbo_timeline: se COMPRA al
ask y se VALÚA/VENDE al bid por minuto). Corre 100% del cache local (offline +
entry_from_timeline) — nunca pega a Polygon.

Convenciones heredadas del engine (documentadas, no re-decididas acá):
  - unidades fraccionales (invest·(px/entry)); sin redondeo a contratos enteros;
  - el refuerzo/compra requiere px > $0.01 (mismo guard de _simulate_refuerzo); si el ask
    de CUALQUIERA de las dos compras no es usable en t*, el evento se EXCLUYE ENTERO de la
    matriz (mantiene las 4 ramas pareadas) y se cuenta aparte;
  - sin comisiones (igual que el resto del backtest).

Salidas (en resultados/): eventos (detalle), matriz de decisión (CSV + markdown legible con
buckets de hora × profundidad, y día de la semana), con EV ($ y %), win-rate de cada rama vs
«cortar», n y significancia (bt_analysis/significance.py — t-test PAREADO sobre las
diferencias rama−cortar; con su fallback sin scipy).

Uso:
  python estudio_refuerzo_vs_contra.py [--desde 2026-01-02] [--hasta 2026-07-02]
        [--tickers QQQ,SPY,IWM] [--out ../resultados] [--workers 8]
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import time as _time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))            # options_replay (engine, downloader, …)
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))     # raíz del repo (config, strategy_core)

import config  # noqa: E402
from adapter_polygon import PolygonAdapter  # noqa: E402
from bt_analysis.significance import tests as sig_tests  # noqa: E402
from downloader import Downloader  # noqa: E402
from engine import (NoMatchError, _prepare_iteration_context,  # noqa: E402
                    load_spread_config)
from signals_backtest import premium_range, to_ts  # noqa: E402
from ucbatch.runner import trading_days  # noqa: E402

# ── Parámetros de la casa (mismos del walk-forward / seed del batch) ──────────
TICKERS_DEF = ["QQQ", "SPY", "IWM"]
ENTRY_T = _time(9, 30)
EXIT_T = _time(15, 55)          # las bifurcaciones corren hasta acá (cierre del estudio)
SEARCH_WIN = 4.0                # ventana de búsqueda de entrada (min) — mismo valor del walk-forward
INVERSION = 1000.0              # $1,000 base
INV_LEG = INVERSION / 2.0       # 50/50 → $500 por pierna
ADD_AMT = INV_LEG               # refuerzo/contra = capital inicial de la pierna (patrón engine)
THRESHOLDS = [0.20, 0.30, 0.40, 0.50, 0.60]
MIN_PX = 0.01                   # px mínimo comprable (mismo guard de _simulate_refuerzo)
LEVELS = ["combined", "leg"]
BRANCHES = ["hold", "reforzar", "contra", "cortar"]
CAPITAL = {"hold": INVERSION, "cortar": INVERSION,
           "reforzar": INVERSION + ADD_AMT, "contra": INVERSION + ADD_AMT}
BUCKETS = [("09:30-10:30", _time(9, 30), _time(10, 30)),
           ("10:30-12:00", _time(10, 30), _time(12, 0)),
           ("12:00-13:30", _time(12, 0), _time(13, 30)),
           ("13:30-15:55", _time(13, 30), _time(16, 0))]
_DOW = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _bucket(t: _time) -> str:
    for lbl, lo, hi in BUCKETS:
        if lo <= t < hi:
            return lbl
    return BUCKETS[-1][0]


# ── 1) Posición base por (ticker, día): series minuto a minuto de la casa ─────
def build_position(dl: Downloader, ticker: str, day: str) -> dict:
    """Reusa _prepare_iteration_context (la parte INVARIANTE de run_one) para obtener la
    posición base con la MISMA selección de contrato y los MISMOS fills Fase 2 del engine:
    merged con call_px/put_px = BID por minuto, series de ASK para las compras y primas de
    entrada al ask. Devuelve dict con status='ok'|'skip' (+motivo)."""
    base = {"ticker": ticker, "fecha": day}
    try:
        under_full = dl.underlying(ticker, day)
        if under_full.empty:
            return {**base, "status": "skip", "motivo": "sin subyacente en cache"}
        chain = dl.chain(ticker, day)
        if chain.empty:
            return {**base, "status": "skip", "motivo": "sin chain 0DTE en cache"}
        calls = chain[chain["contract_type"].str.lower() == "call"].copy()
        puts = chain[chain["contract_type"].str.lower() == "put"].copy()
        if calls.empty or puts.empty:
            return {**base, "status": "skip", "motivo": "chain sin CALL o PUT"}
        lo, hi = premium_range(ticker)
        start_ts = to_ts(day, ENTRY_T)
        end_ts = to_ts(day, EXIT_T)
        ctx = _prepare_iteration_context(
            dl, ticker, day, under_full, calls, puts, lo, hi, start_ts, end_ts,
            25, "both", lo, hi, 1, None, "spread", 2.0, load_spread_config(),
            True, True, SEARCH_WIN)
    except NoMatchError as e:
        return {**base, "status": "skip", "motivo": f"sin contrato (rango/spread): {e}"}
    except Exception as e:  # noqa: BLE001 — aislar el fallo de un día
        return {**base, "status": "skip", "motivo": str(e)}

    merged = ctx["merged"]
    if merged.empty or len(merged) < 2:
        return {**base, "status": "skip", "motivo": "serie minuto a minuto vacía"}
    entry_c, entry_p = float(ctx["call_entry"]), float(ctx["put_entry"])
    if entry_c <= 0 or entry_p <= 0:
        return {**base, "status": "skip", "motivo": "prima de entrada inválida"}
    # Series de compra (ASK). Si la pierna no tiene timeline NBBO, el engine degrada al
    # precio del bar (call_px/put_px YA es el bar en ese caso) → misma degradación acá.
    ask_c = ctx["ask_c"].values if ctx["ask_c"] is not None else merged["call_px"].values
    ask_p = ctx["ask_p"].values if ctx["ask_p"] is not None else merged["put_px"].values
    return {**base, "status": "ok",
            "ts": merged["timestamp"].reset_index(drop=True),
            "bid_c": merged["call_px"].to_numpy(dtype=float),
            "bid_p": merged["put_px"].to_numpy(dtype=float),
            "ask_c": np.asarray(ask_c, dtype=float),
            "ask_p": np.asarray(ask_p, dtype=float),
            "entry_c": entry_c, "entry_p": entry_p,
            "call_occ": ctx["call_pick"].occ, "put_occ": ctx["put_pick"].occ,
            "nbbo_ok": (ctx["ask_c"] is not None) and (ctx["ask_p"] is not None),
            "hora_entrada": merged["timestamp"].iloc[0].strftime("%H:%M")}


# ── 2) Bifurcación de las 4 ramas en el primer cruce de cada umbral ───────────
def _first_cross(mask: np.ndarray):
    return int(mask.argmax()) if mask.any() else None


def simulate_position(pos: dict) -> list[dict]:
    """Detecta los primeros cruces de −X% (combinado y por pierna) y computa el P&L final
    ($, al cierre 15:55, ventas al bid) de las 4 ramas para cada evento."""
    ts = pos["ts"]
    bid_c, bid_p = pos["bid_c"], pos["bid_p"]
    ask_c, ask_p = pos["ask_c"], pos["ask_p"]
    ec, ep = pos["entry_c"], pos["entry_p"]
    last = len(ts) - 1
    u_c, u_p = bid_c / ec, bid_p / ep                 # valor (al bid) por $1 invertido
    roi_c, roi_p = u_c - 1.0, u_p - 1.0
    roi_comb = 0.5 * (u_c + u_p) - 1.0                # 50/50
    hold_final = INV_LEG * (u_c[last] + u_p[last])    # valor $ de la base al cierre
    dow = _DOW[pd.Timestamp(pos["fecha"]).dayofweek]
    out = []
    for level in LEVELS:
        for thr in THRESHOLDS:
            if level == "combined":
                idx = _first_cross(roi_comb <= -thr)
            else:
                idx = _first_cross((roi_c <= -thr) | (roi_p <= -thr))
            if idx is None:
                continue
            if level == "leg":
                c_hit, p_hit = roi_c[idx] <= -thr, roi_p[idx] <= -thr
                if c_hit and p_hit:                    # cruce simultáneo → la que MÁS pierde
                    loser = "CALL" if roi_c[idx] <= roi_p[idx] else "PUT"
                else:
                    loser = "CALL" if c_hit else "PUT"
            else:                                      # combinado → pierna más perdedora
                loser = "CALL" if roi_c[idx] <= roi_p[idx] else "PUT"
            if loser == "CALL":
                bid_L, ask_L, u_L, ask_O, bid_O, u_O = bid_c, ask_c, u_c, ask_p, bid_p, u_p
            else:
                bid_L, ask_L, u_L, ask_O, bid_O, u_O = bid_p, ask_p, u_p, ask_c, bid_c, u_c
            askL, askO = float(ask_L[idx]), float(ask_O[idx])
            # Ejecutabilidad: AMBAS compras (refuerzo y contra) necesitan un ask usable en t*
            # (> $0.01, guard del engine). Si alguna no lo es → el evento entero se excluye
            # de la matriz (comparaciones pareadas) y se cuenta en 'ejecutable'=False.
            feasible = (np.isfinite(askL) and askL > MIN_PX
                        and np.isfinite(askO) and askO > MIN_PX)
            pnl_hold = hold_final - INVERSION
            pnl_ref = (hold_final + ADD_AMT * (bid_L[last] / askL)
                       - (INVERSION + ADD_AMT)) if feasible else np.nan
            pnl_ctr = (hold_final + ADD_AMT * (bid_O[last] / askO)
                       - (INVERSION + ADD_AMT)) if feasible else np.nan
            if level == "combined":                    # cortar TODO al bid de t*
                pnl_cut = INV_LEG * (u_c[idx] + u_p[idx]) - INVERSION
            else:                                      # cortar SOLO la pierna perdedora
                pnl_cut = INV_LEG * u_L[idx] + INV_LEG * u_O[last] - INVERSION
            t_cross = ts.iloc[idx]
            out.append({
                "ticker": pos["ticker"], "fecha": pos["fecha"], "dow": dow,
                "nivel": level, "umbral_pct": int(round(thr * 100)),
                "hora_cruce": t_cross.strftime("%H:%M"), "bucket": _bucket(t_cross.time()),
                "min_a_cierre": int(last - idx),
                "pierna_perdedora": loser,
                "roi_comb_t": round(float(roi_comb[idx]) * 100, 2),
                "roi_call_t": round(float(roi_c[idx]) * 100, 2),
                "roi_put_t": round(float(roi_p[idx]) * 100, 2),
                "ask_perdedora_t": round(askL, 4), "ask_opuesta_t": round(askO, 4),
                "ejecutable": bool(feasible),
                "pnl_hold": round(float(pnl_hold), 2),
                "pnl_reforzar": round(float(pnl_ref), 2) if feasible else np.nan,
                "pnl_contra": round(float(pnl_ctr), 2) if feasible else np.nan,
                "pnl_cortar": round(float(pnl_cut), 2),
                "pct_hold": round(float(pnl_hold) / CAPITAL["hold"] * 100, 2),
                "pct_reforzar": (round(float(pnl_ref) / CAPITAL["reforzar"] * 100, 2)
                                 if feasible else np.nan),
                "pct_contra": (round(float(pnl_ctr) / CAPITAL["contra"] * 100, 2)
                               if feasible else np.nan),
                "pct_cortar": round(float(pnl_cut) / CAPITAL["cortar"] * 100, 2),
                "call_occ": pos["call_occ"], "put_occ": pos["put_occ"],
                "nbbo_ok": pos["nbbo_ok"], "hora_entrada": pos["hora_entrada"],
            })
    return out


# ── 3) Agregación por celda + significancia ───────────────────────────────────
def _cell_stats(sub: pd.DataFrame) -> dict:
    """Métricas de una celda: EV ($ y %) por rama, win-rate de cada rama vs 'cortar' y
    t-test PAREADO (significance.tests sobre las diferencias rama−cortar, $ por evento).
    n<8 → el módulo no computa el test (queda vacío)."""
    out = {"n": int(len(sub))}
    for b in BRANCHES:
        out[f"ev_{b}"] = round(float(sub[f"pnl_{b}"].mean()), 2)
        out[f"evpct_{b}"] = round(float(sub[f"pct_{b}"].mean()), 2)
    for b in ("hold", "reforzar", "contra"):
        d = (sub[f"pnl_{b}"] - sub["pnl_cortar"]).dropna()
        out[f"delta_{b}_vs_cortar"] = round(float(d.mean()), 2) if len(d) else np.nan
        out[f"win_{b}_vs_cortar"] = round(float((d > 0).mean() * 100), 1) if len(d) else np.nan
        rv = sig_tests(pd.DataFrame({"roi": d})).get("roi_vs_zero")
        out[f"p_{b}_vs_cortar"] = rv["p_value"] if rv else np.nan
        out[f"sig_{b}_vs_cortar"] = bool(rv["sig"]) if rv else False
    out["mejor_rama"] = max(BRANCHES, key=lambda b: out[f"ev_{b}"])
    return out


def aggregate(ev: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Matrices: (nivel × umbral × bucket_hora [+ 'TODAS']) y (nivel × umbral × dow).
    Solo eventos ejecutables (las 4 ramas pareadas)."""
    rows_h, rows_d = [], []
    for level in LEVELS:
        for thr in THRESHOLDS:
            thr_pct = int(round(thr * 100))
            base = ev[(ev["nivel"] == level) & (ev["umbral_pct"] == thr_pct)]
            if base.empty:
                continue
            rows_h.append({"nivel": level, "umbral_pct": thr_pct, "bucket": "TODAS",
                           **_cell_stats(base)})
            for lbl, _, _ in BUCKETS:
                sub = base[base["bucket"] == lbl]
                if not sub.empty:
                    rows_h.append({"nivel": level, "umbral_pct": thr_pct, "bucket": lbl,
                                   **_cell_stats(sub)})
            for dw in _DOW[:5]:
                sub = base[base["dow"] == dw]
                if not sub.empty:
                    rows_d.append({"nivel": level, "umbral_pct": thr_pct, "dow": dw,
                                   **_cell_stats(sub)})
    return pd.DataFrame(rows_h), pd.DataFrame(rows_d)


# ── 4) Reporte markdown ────────────────────────────────────────────────────────
_BR_LBL = {"hold": "Aguantar", "reforzar": "Reforzar", "contra": "Contra", "cortar": "Cortar"}


def _fmt_cell(r: pd.Series) -> str:
    best = r["mejor_rama"]
    txt = f"**{_BR_LBL[best]}**"
    if best != "cortar":
        d, p, sig = r[f"delta_{best}_vs_cortar"], r[f"p_{best}_vs_cortar"], r[f"sig_{best}_vs_cortar"]
        star = "✱" if sig else ""
        ptxt = "n/d" if pd.isna(p) else f"{p:.3f}"
        txt += f" {d:+.0f}$ vs cortar{star} (p={ptxt})"
    else:
        deltas = [r[f"delta_{b}_vs_cortar"] for b in ("hold", "reforzar", "contra")]
        worst = max(x for x in deltas if pd.notna(x)) if any(pd.notna(x) for x in deltas) else np.nan
        if pd.notna(worst):
            txt += f" (mejor alt. {worst:+.0f}$)"
    return txt + f" · n={int(r['n'])}"


def _tabla_ev(df: pd.DataFrame, nivel: str) -> list[str]:
    L = [f"| X% | n | EV hold | EV reforzar | EV contra | EV cortar | "
         f"win hold | win ref | win contra |",
         "|---|---|---|---|---|---|---|---|---|"]
    for _, r in df[(df["nivel"] == nivel) & (df["bucket"] == "TODAS")].iterrows():
        def s(b):
            star = "✱" if r[f"sig_{b}_vs_cortar"] else ""
            return f"{r[f'win_{b}_vs_cortar']:.0f}%{star}"

        def e(b):   # EV$ (EV% sobre el capital de la rama)
            return f"{r[f'ev_{b}']:+.0f}$ ({r[f'evpct_{b}']:+.1f}%)"
        L.append(f"| −{r['umbral_pct']}% | {int(r['n'])} | {e('hold')} | "
                 f"{e('reforzar')} | {e('contra')} | {e('cortar')} | "
                 f"{s('hold')} | {s('reforzar')} | {s('contra')} |")
    return L


def write_markdown(path: Path, ev: pd.DataFrame, mh: pd.DataFrame, md_dow: pd.DataFrame,
                   meta: dict) -> None:
    L: list[str] = []
    L.append("# Estudio contrafactual — ¿reforzar, comprar en contra o cortar cuando la 0DTE pierde X%?")
    L.append("")
    L.append(f"_Corrido el {meta['corrida']} · {meta['universo']} · {meta['dias_ok']} días-ticker "
             f"con posición (de {meta['dias_tot']}; {meta['dias_skip']} sin posición) · "
             f"eventos: {meta['n_eventos']} ({meta['n_no_ejec']} excluidos por ask no usable en t*)._")
    L.append("")
    L.append("## Método (resumen)")
    L.append("- Posición base de la casa: CALL y PUT 0DTE, $1,000 50/50, entrada 09:30 (ventana de "
             "búsqueda 4 min), contrato «Menor spread en rango óptimo», fills **Fase 2** "
             "(compra al ask, valuación/venta al bid por minuto). 100% cache local, 1 min.")
    L.append("- En el **primer cruce** de −X% (X ∈ 20/30/40/50/60) se bifurcan 4 ramas y corren "
             "hasta 15:55: **aguantar** · **reforzar** (+$500 de la pierna perdedora al ask de t*, "
             "patrón martingala del engine) · **contra** (+$500 de la pierna opuesta al ask de t*) · "
             "**cortar** (vender al bid de t*).")
    L.append("- Dos disparadores por separado: **combined** (la posición 50/50 cruza −X%; cortar "
             "vende ambas piernas) y **leg** (una pierna cruza −X% de SU capital; cortar vende solo "
             "esa pierna). En *leg*, los deltas vs cortar equivalen exactamente al caso de "
             "**pierna única** (la otra pierna es idéntica en las 4 ramas y se cancela).")
    L.append("- EV$ = P&L medio al cierre; EV% sobre el capital comprometido de cada rama "
             "($1,000 hold/cortar; $1,500 reforzar/contra). win = % de eventos donde la rama "
             "termina con MÁS dinero que cortar. ✱ = t-test pareado (rama−cortar) p<0.05 "
             "(significance.py). En la matriz por hora, «mejor alt. −N$» = cuánto pierde vs "
             "cortar la mejor de las otras ramas (cortar domina por N$). Sin comisiones; "
             "unidades fraccionales.")
    L.append(f"- {meta['n_cruce_min0']} eventos cruzan en el MISMO minuto de la entrada: ahí el "
             "drawdown es en gran parte el costo del spread inicial (se compra al ask y se marca "
             "al bid), no un movimiento adverso — pesan sobre todo en leg −20/−30%.")
    L.append("")
    for level, lbl in (("combined", "Disparador COMBINADO (la posición completa pierde X%)"),
                       ("leg", "Disparador POR PIERNA (una pierna pierde X% de su capital)")):
        L.append(f"## {lbl}")
        L.append("")
        L += _tabla_ev(mh, level)
        L.append("")
        L.append("### Matriz de decisión por hora del cruce (mejor rama por EV$)")
        L.append("")
        cols = [b[0] for b in BUCKETS]
        L.append("| X% | " + " | ".join(cols) + " |")
        L.append("|---" * (len(cols) + 1) + "|")
        for thr in THRESHOLDS:
            tp = int(round(thr * 100))
            cells = []
            for c in cols:
                r = mh[(mh["nivel"] == level) & (mh["umbral_pct"] == tp) & (mh["bucket"] == c)]
                cells.append(_fmt_cell(r.iloc[0]) if not r.empty else "—")
            L.append(f"| −{tp}% | " + " | ".join(cells) + " |")
        L.append("")
    L.append("## Día de la semana (todas las horas)")
    L.append("")
    L.append("| Nivel | X% | Lun | Mar | Mié | Jue | Vie |")
    L.append("|---|---|---|---|---|---|---|")
    for level in LEVELS:
        for thr in THRESHOLDS:
            tp = int(round(thr * 100))
            cells = []
            for dw in _DOW[:5]:
                r = md_dow[(md_dow["nivel"] == level) & (md_dow["umbral_pct"] == tp)
                           & (md_dow["dow"] == dw)]
                if r.empty:
                    cells.append("—")
                else:
                    rr = r.iloc[0]
                    star = ("✱" if rr["mejor_rama"] != "cortar"
                            and rr[f"sig_{rr['mejor_rama']}_vs_cortar"] else "")
                    cells.append(f"{_BR_LBL[rr['mejor_rama']]}{star} (n={int(rr['n'])})")
            L.append(f"| {level} | −{tp}% | " + " | ".join(cells) + " |")
    L.append("")
    L.append(meta["resumen_ejecutivo"])
    path.write_text("\n".join(L), encoding="utf-8")


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    # Consola Windows cp1252: forzar UTF-8 (mismo patrón del walk-forward) para →/·/✱.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Estudio contrafactual refuerzo vs contra vs cortar")
    ap.add_argument("--desde", default="2026-01-02")
    ap.add_argument("--hasta", default="2026-07-02")
    ap.add_argument("--tickers", default=",".join(TICKERS_DEF))
    ap.add_argument("--out", default=str(HERE.parent / "resultados"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    days = trading_days(args.desde, args.hasta)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    dl = Downloader(PolygonAdapter(config.POLYGON_API_KEY, rate_limit_per_min=600), HERE / "data")
    dl.resolution = "1min"
    dl.offline = True                 # estrictamente cache-only (nunca pega a Polygon)
    dl.entry_from_timeline = True     # NBBO de entrada desde quotes_minute (cacheado)

    specs = [(tk, d) for d in days for tk in tickers]
    print(f"== ESTUDIO REFUERZO vs CONTRA vs CORTAR · {','.join(tickers)} · "
          f"{args.desde}→{args.hasta} · {len(days)} días háb · Fase 2 · umbrales "
          f"{[int(t*100) for t in THRESHOLDS]}% · salida {EXIT_T:%H:%M} ==")

    positions, skips, done = [], [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for pos in ex.map(lambda s: build_position(dl, *s), specs):
            done += 1
            if pos["status"] == "ok":
                positions.append(pos)
            else:
                skips.append(pos)
            if done % 50 == 0 or done == len(specs):
                print(f"   posiciones {done}/{len(specs)} (ok={len(positions)} skip={len(skips)})")

    events: list[dict] = []
    for pos in positions:
        events.extend(simulate_position(pos))
    ev = pd.DataFrame(events)
    if ev.empty:
        print("Sin eventos de drawdown — nada que analizar.")
        return
    n_no_exec = int((~ev["ejecutable"]).sum())
    ev_ok = ev[ev["ejecutable"]].copy()
    n_min0 = int((ev_ok["hora_cruce"] == ev_ok["hora_entrada"]).sum())

    mh, md_dow = aggregate(ev_ok)

    # ── salidas ──
    p_ev = out_dir / "estudio_refuerzo_vs_contra_eventos.csv"
    p_mx = out_dir / "estudio_refuerzo_vs_contra_matriz.csv"
    p_dw = out_dir / "estudio_refuerzo_vs_contra_dow.csv"
    p_md = out_dir / "estudio_refuerzo_vs_contra.md"
    ev.to_csv(p_ev, index=False, encoding="utf-8-sig")
    mh.to_csv(p_mx, index=False, encoding="utf-8-sig")
    md_dow.to_csv(p_dw, index=False, encoding="utf-8-sig")

    resumen = ["## Resumen ejecutivo", ""]
    for level in LEVELS:
        top = mh[(mh["nivel"] == level) & (mh["bucket"] == "TODAS")]
        resumen.append(f"**{level}** — mejor rama por umbral (todas las horas): " + "; ".join(
            f"−{int(r['umbral_pct'])}%→{_BR_LBL[r['mejor_rama']]}"
            f" (EV {r['ev_' + r['mejor_rama']]:+.0f}$ vs cortar {r['ev_cortar']:+.0f}$, n={int(r['n'])})"
            for _, r in top.iterrows()) + ".")
        resumen.append("")
    resumen.append("### Advertencias de la casa")
    resumen.append("- **Una sola ventana temporal (ene→jul 2026) = in-sample**: no hay validación "
                   "fuera de muestra; los resultados describen ESTE régimen de mercado.")
    resumen.append("- **Muestras chicas por celda** (sobre todo buckets tardíos y umbrales "
                   "profundos): celdas con n<8 no tienen test; n<20 es indicativo, no concluyente.")
    resumen.append("- **Sesgo de selección de umbral**: los eventos de distintos X del mismo día "
                   "están anidados (un día muy malo dispara 20→60) y los 3 tickers están "
                   "correlacionados → los p-values son OPTIMISTAS (caveat estándar de "
                   "significance.py). Úsalos como guía, no como prueba.")
    resumen.append("- Unidades fraccionales y sin comisiones (convención del engine); montos "
                   "fijos $500 por acción de refuerzo/contra.")
    meta = {
        "corrida": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "universo": f"{','.join(tickers)} {args.desde}→{args.hasta}",
        "dias_tot": len(specs), "dias_ok": len(positions), "dias_skip": len(skips),
        "n_eventos": len(ev), "n_no_ejec": n_no_exec, "n_cruce_min0": n_min0,
        "resumen_ejecutivo": "\n".join(resumen),
    }
    write_markdown(p_md, ev_ok, mh, md_dow, meta)

    print(f"\nPosiciones: {len(positions)} ok · {len(skips)} skip · eventos {len(ev)} "
          f"({n_no_exec} no ejecutables excluidos; {n_min0} cruzan en el minuto de entrada)")
    if skips:
        for lbl, n in pd.Series([s['motivo'][:60] for s in skips]).value_counts().head(5).items():
            print(f"   skip: {n}× {lbl}")
    print("\n-- Mejor rama por (nivel, umbral), todas las horas --")
    for _, r in mh[mh["bucket"] == "TODAS"].iterrows():
        print(f"   {r['nivel']:<8} −{int(r['umbral_pct'])}%  n={int(r['n']):>3}  "
              f"mejor={_BR_LBL[r['mejor_rama']]:<9} "
              f"EV$ hold/ref/contra/cortar = {r['ev_hold']:+7.1f} / {r['ev_reforzar']:+7.1f} / "
              f"{r['ev_contra']:+7.1f} / {r['ev_cortar']:+7.1f}")
    print(f"\nSalidas:\n  {p_ev}\n  {p_mx}\n  {p_dw}\n  {p_md}")


if __name__ == "__main__":
    main()

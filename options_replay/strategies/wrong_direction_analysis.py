"""¿Cuándo la señal apunta a la dirección EQUIVOCADA?

Clasifica cada señal de apertura (CALL/PUT) en DIRECCIÓN CORRECTA vs INCORRECTA según
hacia dónde se movió REALMENTE el subyacente (open 9:30 → close 16:00), y compara
las características de mercado entre las que aciertan y las que fallan. Solo barras 15m
(sin NBBO) → rápido, 4 años. Features ORIENTADAS al sentido de la señal (positivo =
más fuerte/alineado con la dirección propuesta).
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np
import pandas as pd
from strategies.run_detect import load_15m
from strategies.trend_reversal_bb_15m import TrendReversalBB15m
from strategies import indicators as ind

TK = ["QQQ", "SPY", "IWM"]
START, END = "2022-06-01", "2026-06-23"
recs = []
for tk in TK:
    b = load_15m(tk, START, END).sort_values("timestamp").reset_index(drop=True)
    bs, up, lo = ind.bollinger(b["close"])
    b = b.assign(basis=bs, upper=up, lower=lo)
    b["atr"] = ind.atr(b["high"], b["low"], b["close"], 14)
    eod = b.groupby("session_date")["close"].last()
    sess = list(eod.index)
    em = eod.to_dict()
    pc = {sess[i]: em[sess[i - 1]] for i in range(1, len(sess))}
    ppc = {sess[i]: em[sess[i - 2]] for i in range(2, len(sess))}
    has_v = "volume" in b.columns
    if has_v:
        opv = b[b["is_opening"]].drop_duplicates("session_date").set_index("session_date")["volume"]
        opv_avg = opv.rolling(20, min_periods=5).mean()
    sigs = TrendReversalBB15m({"intraday": False}).detect_signals(b, tk)
    opb = b[b["is_opening"]].drop_duplicates("session_date", keep="first").set_index("session_date")
    for _, s in sigs.iterrows():
        d = s["fecha"]
        if d not in opb.index:
            continue
        row = opb.loc[d]
        o, oc = float(row["open"]), float(row["close"])
        U, L, ba, at = row["upper"], row["lower"], row["basis"], row["atr"]
        if pd.isna(U) or pd.isna(L) or pd.isna(ba) or not at:
            continue
        ses = b[b["session_date"] == d]
        if ses.empty:
            continue
        eodpx = float(ses["close"].iloc[-1])
        hi, loo = float(ses["high"].max()), float(ses["low"].min())
        sgn = 1.0 if s["direccion"] == "CALL" else -1.0
        net = (eodpx - o) / o * 100 * sgn          # >0 = se movió EN la dirección de la señal
        if s["direccion"] == "CALL":
            adverse, fav, firstbar = (o - loo) / o * 100, (hi - o) / o * 100, (oc - o) / o * 100
            bb_room = (U - o) / (U - L) * 100 if U > L else np.nan
        else:
            adverse, fav, firstbar = (hi - o) / o * 100, (o - loo) / o * 100, (o - oc) / o * 100
            bb_room = (o - L) / (U - L) * 100 if U > L else np.nan
        gp = (o - pc[d]) / pc[d] * 100 if d in pc else np.nan
        rec = dict(
            tk=tk, fecha=str(d), dir=s["direccion"], correct=bool(net > 0),
            net=net, adverse=adverse, fav=fav,
            firstbar_confirm=firstbar,                                   # >0 = vela 9:30 cierra A FAVOR
            gap_dir=gp * sgn if pd.notna(gp) else np.nan,                # >0 = gap en la dir. de la señal
            bb_room=bb_room,                                             # % de banda libre en la dir. señal
            bb_width=(U - L) / ba * 100 if ba else np.nan,
            dist_prevmid=((o - s["prevMidpoint"]) * sgn / o * 100) if pd.notna(s.get("prevMidpoint")) else np.nan,
            breakout_atr=(((o - s["trendline"]) * sgn) / at) if pd.notna(s.get("trendline")) else np.nan,
            prior_trend_opp=(-s["basisChangePct"] * sgn) if pd.notna(s.get("basisChangePct")) else np.nan,
            atr_pct=at / o * 100,
            prevday_ret_opp=((pc[d] - ppc[d]) / ppc[d] * 100 * (-sgn)) if (d in pc and d in ppc) else np.nan,
            dow=pd.Timestamp(d).day_name()[:3],
            rel_vol=(float(opv.loc[d] / opv_avg.loc[d]) if has_v and d in opv_avg.index
                     and pd.notna(opv_avg.loc[d]) and opv_avg.loc[d] > 0 else np.nan),
        )
        recs.append(rec)

df = pd.DataFrame(recs)
df.to_csv(HERE.parent / "data" / "wrong_direction_signals.csv", index=False)
FEATS = ["firstbar_confirm", "gap_dir", "prior_trend_opp", "bb_room", "bb_width",
         "dist_prevmid", "breakout_atr", "atr_pct", "prevday_ret_opp", "rel_vol"]

print(f"=== {len(df)} señales de apertura (4 años, 3 tickers) ===")
for dr in ["CALL", "PUT", "TODAS"]:
    sub = df if dr == "TODAS" else df[df.dir == dr]
    print(f"{dr:5s}: N={len(sub):4d}  dir-acierto={sub.correct.mean()*100:4.1f}%  "
          f"avg_net={sub.net.mean():+.2f}%  adverso_medio(MAE)={sub.adverse.mean():.2f}%")

print("\n=== Media del feature en ACERTADAS vs FALLADAS (orientado, ordenado por separación) ===")
rows = []
for f in FEATS:
    c, w = df[df.correct][f].mean(), df[~df.correct][f].mean()
    rows.append((f, c, w, c - w))
for f, c, w, d in sorted(rows, key=lambda x: -abs(x[3])):
    print(f"  {f:18s} acierta={c:8.2f}  falla={w:8.2f}  Δ={d:+8.2f}")

print("\n=== Acierto direccional por TERCIL de cada feature ===")
for f in FEATS:
    s = df[[f, "correct", "net"]].dropna()
    if len(s) < 45:
        continue
    try:
        s = s.assign(b=pd.qcut(s[f], 3, labels=["bajo", "medio", "alto"], duplicates="drop"))
    except Exception:
        continue
    g = s.groupby("b", observed=True).agg(N=("correct", "size"), acc=("correct", "mean"), net=("net", "mean"))
    cells = "  ".join(f"{i}:{r.acc*100:4.1f}%(n{int(r.N)},net{r.net:+.1f})" for i, r in g.iterrows())
    print(f"  {f:18s} {cells}")

print("\n=== Acierto por DÍA de la semana ===")
g = df.groupby("dow").agg(N=("correct", "size"), acc=("correct", "mean"), net=("net", "mean"))
for d in ["Mon", "Tue", "Wed", "Thu", "Fri"]:
    if d in g.index:
        r = g.loc[d]
        print(f"  {d}: N={int(r.N):4d}  acc={r.acc*100:4.1f}%  net={r.net:+.2f}%")

print("\n=== EARLY WARNING — ¿la vela 9:30 cierra EN CONTRA de la señal? ===")
df["fb_against"] = df.firstbar_confirm < 0
g = df.groupby("fb_against").agg(N=("correct", "size"), acc=("correct", "mean"),
                                 net=("net", "mean"), mae=("adverse", "mean"))
for k, r in g.iterrows():
    lab = "9:30 cierra EN CONTRA" if k else "9:30 cierra A FAVOR "
    print(f"  {lab}: N={int(r.N):4d}  acc={r.acc*100:4.1f}%  net={r.net:+.2f}%  adverso={r.mae:.2f}%")
print("FIN", flush=True)

"""Trend Reversal Up & Down BB 15m — réplica EXACTA del Pine (lógica del documento).

Ver SPEC_criterios_TR-UD-15m.md. Loop bar-a-bar igual al modelo de ejecución de Pine:
pivots confirmados R barras después, ventana de lookback, trendline hull, y los 4 criterios.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import SIGNAL_COLS, Strategy
from . import indicators as ind


class TrendReversalBB15m(Strategy):
    name = "trend_reversal_bb_15m"

    @staticmethod
    def default_params() -> dict:
        return dict(
            bbLen=20, bbMult=2.0, midpointPct=0.5,
            lateralLookback=20, lateralThreshold=0.5,
            pivotLen=3, trendlineLookback=40, maxBarsForward=20,
            useVolFilter=True, volMultATR=1.0,
            enableUp=True, enableDown=True,
            # --- modo INTRADÍA (default OFF = comportamiento original de apertura) ---
            intraday=False,          # True = detecta cruces en CUALQUIER vela de la sesión
            cooldown_bars=8,         # velas de espera tras una señal (evita clusters)
            require_in_bands=True,   # filtro de sobre-extensión (close dentro de las BB)
            use_r3=True,             # (sweep) False = ignora R3 (ruptura del midpoint)
            use_r4=True,             # (sweep) False = ignora R4 (dentro de bandas)
            r3_mode="prevmid",       # (sweep) "prevmid" | "basis" — nivel de R3 intradía
        )

    def detect_signals(self, bars15m: pd.DataFrame, ticker: str) -> pd.DataFrame:
        p = self.params
        df = bars15m.sort_values("timestamp").reset_index(drop=True).copy()

        basis, upper, lower = ind.bollinger(df["close"], p["bbLen"], p["bbMult"])
        df["basis"] = basis
        df["upper"] = upper
        df["lower"] = lower
        df["atr"] = ind.atr(df["high"], df["low"], df["close"], 14)
        df["rango"] = df["high"] - df["low"]
        prev = basis.shift(p["lateralLookback"])
        df["basisChangePct"] = (basis - prev) / prev * 100.0

        pdl = ind.prev_day_levels(df, p["midpointPct"])
        df = df.merge(pdl, left_on="session_date", right_index=True, how="left")

        phb = ind.pivot_high_bars(df["high"], p["pivotLen"], p["pivotLen"])
        plb = ind.pivot_low_bars(df["low"], p["pivotLen"], p["pivotLen"])
        H = df["high"].to_numpy(dtype=float)
        L = df["low"].to_numpy(dtype=float)

        R, LB, MF = p["pivotLen"], p["trendlineLookback"], p["maxBarsForward"]
        intraday = bool(p.get("intraday", False))
        cd_bars = int(p.get("cooldown_bars", 8))
        req_bands = bool(p.get("require_in_bands", True))
        use_r3 = bool(p.get("use_r3", True))
        use_r4 = bool(p.get("use_r4", True))
        r3_mode = p.get("r3_mode", "prevmid")
        C = df["close"].to_numpy(dtype=float)
        phP: list[float] = []; phB: list[int] = []
        plP: list[float] = []; plB: list[int] = []
        prev_up = prev_dn = False
        up_prev = dn_prev = None        # valor de la trendline en la barra anterior (para el cruce)
        last_up = last_dn = -10 ** 9    # última barra con señal (cooldown intradía)
        out: list[dict] = []
        opens: list[dict] = []

        for b in range(len(df)):
            j = b - R                                    # pivot se confirma R barras después
            if j >= 0:
                if phb[j]:
                    phP.append(H[j]); phB.append(j)
                if plb[j]:
                    plP.append(L[j]); plB.append(j)
            while phB and phB[0] < b - LB:
                phB.pop(0); phP.pop(0)
            while plB and plB[0] < b - LB:
                plB.pop(0); plP.pop(0)

            up_now = dn_now = None
            up_active = dn_active = False
            hu = ind.hull_line(list(zip(phB, phP)), "res")
            if hu is not None:
                a_bar, a_price, slope = hu
                up_active = b <= (phB[-1] + MF)
                if up_active:
                    up_now = a_price + slope * (b - a_bar)
            hd = ind.hull_line(list(zip(plB, plP)), "sup")
            if hd is not None:
                a_bar, a_price, slope = hd
                dn_active = b <= (plB[-1] + MF)
                if dn_active:
                    dn_now = a_price + slope * (b - a_bar)

            row = df.iloc[b]
            opening = bool(row["is_opening"])
            bcp = row["basisChangePct"]
            pmid = row["prevMidpoint"]
            opn = float(row["open"])

            r1u = (not pd.isna(bcp)) and bcp <= p["lateralThreshold"]
            r1d = (not pd.isna(bcp)) and bcp >= -p["lateralThreshold"]
            cls = float(C[b])
            up, lo = row["upper"], row["lower"]
            cx_u = up_now is not None and up_prev is not None and cls > up_now and C[b - 1] <= up_prev
            cx_d = dn_now is not None and dn_prev is not None and cls < dn_now and C[b - 1] >= dn_prev

            if not intraday:
                # APERTURA: gap rompe la trendline (R2) y el midpoint DÍA-PREVIO (R3); open dentro de bandas (R4)
                r2u = up_active and up_now is not None and opn > up_now
                r2d = dn_active and dn_now is not None and opn < dn_now
                r3u = opening and (not pd.isna(pmid)) and opn > pmid
                r3d = opening and (not pd.isna(pmid)) and opn < pmid
                p_r4 = opn
                gate_u = gate_d = True
            else:
                # INTRADÍA: el CIERRE cruza la trendline (R2) y rompe el midpoint DÍA-PREVIO (R3); close en bandas (R4)
                r2u, r2d = cx_u, cx_d
                _lvl = row["basis"] if r3_mode == "basis" else pmid
                r3u = (not pd.isna(_lvl)) and cls > _lvl
                r3d = (not pd.isna(_lvl)) and cls < _lvl
                if not use_r3:
                    r3u = r3d = True
                p_r4 = cls
                gate_u = (b - last_up) >= cd_bars
                gate_d = (b - last_dn) >= cd_bars

            # R4 = la vela abre DENTRO de las Bandas de Bollinger (lower <= precio <= upper)
            r4 = (not pd.isna(up)) and (not pd.isna(lo)) and (lo <= p_r4 <= up)
            if not use_r4:
                r4 = True
            full_up = bool(p["enableUp"] and r1u and r2u and r3u and r4 and gate_u)
            full_dn = bool(p["enableDown"] and r1d and r2d and r3d and r4 and gate_d)

            _entry = cls if intraday else opn
            # intradía: entrada al CIERRE de la vela (sin lookahead); saltear la última (15:45→16:00=expiry)
            if intraday:
                ets = row["timestamp"] + pd.Timedelta(minutes=15)
                tradeable = ets.strftime("%H:%M") < "16:00"
            else:
                ets, tradeable = row["timestamp"], True
            fire_up = (full_up if intraday else (full_up and not prev_up)) and tradeable
            fire_dn = (full_dn if intraday else (full_dn and not prev_dn)) and tradeable
            if fire_up:
                out.append(self._sig(row, ticker, "CALL", _entry, bcp, pmid, up_now, r1u, r2u, r3u, r4, entry_ts=ets))
                last_up = b
            if fire_dn:
                out.append(self._sig(row, ticker, "PUT", _entry, bcp, pmid, dn_now, r1d, r2d, r3d, r4, entry_ts=ets))
                last_dn = b
            if opening:
                opens.append(dict(
                    fecha=row["session_date"], open=round(opn, 2),
                    basis=round(float(row["basis"]), 2) if not pd.isna(row["basis"]) else None,
                    prevMid=round(float(pmid), 2) if not pd.isna(pmid) else None,
                    upLine=round(float(up_now), 2) if up_now is not None else None,
                    dnLine=round(float(dn_now), 2) if dn_now is not None else None,
                    R1up=r1u, R2up=r2u, R3up=r3u, R1dn=r1d, R2dn=r2d, R3dn=r3d, R4=r4,
                    senal=("CALL" if full_up else "PUT" if full_dn else "-")))
            prev_up, prev_dn = full_up, full_dn
            up_prev, dn_prev = up_now, dn_now

        self.last_opens = pd.DataFrame(opens)
        return pd.DataFrame(out, columns=SIGNAL_COLS)

    @staticmethod
    def _sig(row, ticker, direc, opn, bcp, pmid, tline, r1, r2, r3, r4, entry_ts=None) -> dict:
        ts = entry_ts if entry_ts is not None else row["timestamp"]
        return dict(
            fecha=row["session_date"], hora_et=ts.strftime("%H:%M"), ticker=ticker, direccion=direc,
            open=round(opn, 4), basis=round(float(row["basis"]), 4) if not pd.isna(row["basis"]) else None,
            prevMidpoint=round(float(pmid), 4) if not pd.isna(pmid) else None,
            trendline=round(float(tline), 4) if tline is not None else None,
            basisChangePct=round(float(bcp), 4) if not pd.isna(bcp) else None,
            atr=round(float(row["atr"]), 4) if not pd.isna(row["atr"]) else None,
            rango=round(float(row["rango"]), 4),
            R1=bool(r1), R2=bool(r2), R3=bool(r3), R4=bool(r4), fuente="python",
        )

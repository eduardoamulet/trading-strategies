"""`TickerProfile` — características intrínsecas de cada activo operable + el registro del universo.

Encapsula lo MEDIDO de los datos (rango diario, vol relativa, beta/corr vs SPY) y lo ESTRUCTURAL
(clase de activo, disponibilidad de 0DTE: diario vs solo-viernes). Es la base de la optimización
por activo: niveles escalados por volatilidad, confirmación ponderada por correlación, y el filtro
de "¿se puede operar 0DTE hoy?".

Universo (decidido con el usuario, 2026-06-30):
  · Núcleo DIARIO  → SPY, QQQ, IWM   (0DTE todos los días + movimiento decente)
  · Roster VIERNES → grandes movers (NVDA, AMD, TSLA, COIN, AVGO, MU, …)  (0DTE solo viernes)

Métricas medidas sobre ~60 días (rango) / ~120 días (beta·corr), snapshot 2026-06. RECALIBRABLE:
volver a correr la medición y actualizar la tabla `_DATA`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AssetClass(str, Enum):
    ETF = "ETF"
    STOCK = "Acción"
    INDEX = "Índice"


class ZeroDTE(str, Enum):
    DAILY = "Diario"          # 0DTE todos los días hábiles (índices/ETFs líquidos)
    MWF = "Lun/Mié/Vie"       # 0DTE Lun-Mié-Vie (mega-caps + GLD/SLV/USO) — NO Mar/Jue
    FRIDAY = "Solo viernes"   # weekly: 0DTE solo el viernes (incluye DIA, GOOG, y el resto)


# umbrales de "gran movimiento" por rango medio intradía (% del precio); SPY ≈ 1%/día = piso
_TIERS = ((4.0, "✓✓✓", "Enorme"), (2.8, "✓✓", "Muy alto"),
          (1.8, "✓", "Alto"), (1.2, "~", "Moderado"), (0.0, "✗", "Bajo"))


def movement_symbol_for(daily_range_pct: float) -> str:
    """Símbolo de tier para un rango dado (para métricas frescas fuera del snapshot)."""
    return next(sym for thr, sym, _ in _TIERS if daily_range_pct >= thr)


@dataclass(frozen=True, slots=True)
class TickerProfile:
    ticker: str
    name: str
    asset_class: AssetClass
    daily_range_pct: float            # rango medio intradía (high-low)/close, % — medido ~60d
    vol_x_spy: float                  # ese rango relativo a SPY
    zero_dte: ZeroDTE
    in_daily_core: bool = False
    in_friday_roster: bool = False
    beta_spy: Optional[float] = None  # medido ~120d (solo núcleo por ahora)
    corr_spy: Optional[float] = None
    notes: str = ""

    @property
    def movement_symbol(self) -> str:
        return next(sym for thr, sym, _ in _TIERS if self.daily_range_pct >= thr)

    @property
    def movement_label(self) -> str:
        return next(lab for thr, _, lab in _TIERS if self.daily_range_pct >= thr)

    def tradeable_on(self, weekday: int) -> bool:
        """¿Hay 0DTE el día `weekday` (0=lunes … 6=domingo)?  DIARIO→L-V · MWF→L/X/V · VIERNES→V."""
        if weekday >= 5:                      # fin de semana
            return False
        if self.zero_dte == ZeroDTE.DAILY:
            return True
        if self.zero_dte == ZeroDTE.MWF:
            return weekday in (0, 2, 4)        # lunes, miércoles, viernes
        return weekday == 4                   # viernes

    @property
    def confidence_weight(self) -> float:
        """Peso de la confirmación cross-asset según la correlación con SPY (corr alta → SPY manda;
        corr baja → manda la señal propia). Para el motor; cae a 0.6 si no hay corr medida."""
        if self.corr_spy is None:
            return 0.6
        return max(0.0, min(1.0, self.corr_spy))


# ---- universo: clasificación estática (núcleo diario / roster viernes) ----
_DAILY_CORE = {"SPY", "QQQ", "IWM"}
_FRIDAY_ROSTER = {"NVDA", "AMD", "AVGO", "MU", "QCOM", "TSLA", "COIN", "PLTR", "ORCL",
                  "AMZN", "META", "MSFT", "NFLX", "GOOG", "AAPL", "SOXL", "TNA"}
_BETA_CORR = {  # medido ~120d vs SPY (núcleo + las 2 acciones de referencia)
    "SPY": (1.02, 1.00), "QQQ": (1.51, 0.93), "IWM": (1.20, 0.85),
    "DIA": (0.79, 0.82), "AAPL": (0.79, 0.44), "NVDA": (1.92, 0.70),
}

# (ticker, nombre, clase, rango%/día, vol×SPY, 0DTE)  — snapshot medido 2026-06
_DATA = [
    ("SOXL", "Semis 3× alcista", AssetClass.ETF, 10.58, 10.29),
    ("OKLO", "Oklo", AssetClass.STOCK, 8.21, 7.98),
    ("MU", "Micron", AssetClass.STOCK, 6.45, 6.27),
    ("MRNA", "Moderna", AssetClass.STOCK, 6.43, 6.25),
    ("QCOM", "Qualcomm", AssetClass.STOCK, 6.30, 6.13),
    ("COIN", "Coinbase", AssetClass.STOCK, 5.78, 5.62),
    ("HOOD", "Robinhood", AssetClass.STOCK, 5.69, 5.53),
    ("AMD", "AMD", AssetClass.STOCK, 5.33, 5.18),
    ("ORCL", "Oracle", AssetClass.STOCK, 5.00, 4.86),
    ("DASH", "DoorDash", AssetClass.STOCK, 4.79, 4.66),
    ("TNA", "Small-cap 3× alcista", AssetClass.ETF, 4.77, 4.63),
    ("PLTR", "Palantir", AssetClass.STOCK, 4.34, 4.22),
    ("NIO", "NIO", AssetClass.STOCK, 4.32, 4.20),
    ("LYFT", "Lyft", AssetClass.STOCK, 4.24, 4.12),
    ("RCL", "Royal Caribbean", AssetClass.STOCK, 4.23, 4.11),
    ("TSLA", "Tesla", AssetClass.STOCK, 4.07, 3.95),
    ("URA", "Uranio ETF", AssetClass.ETF, 4.00, 3.89),
    ("CCL", "Carnival", AssetClass.STOCK, 3.94, 3.83),
    ("AAL", "American Airlines", AssetClass.STOCK, 3.93, 3.82),
    ("USO", "Petróleo ETF", AssetClass.ETF, 3.57, 3.47),
    ("AVGO", "Broadcom", AssetClass.STOCK, 3.56, 3.46),
    ("NVDA", "Nvidia", AssetClass.STOCK, 3.27, 3.18),
    ("DAL", "Delta Air Lines", AssetClass.STOCK, 3.23, 3.14),
    ("UBER", "Uber", AssetClass.STOCK, 3.17, 3.08),
    ("XPEV", "XPeng", AssetClass.STOCK, 3.08, 2.99),
    ("SLV", "Plata ETF", AssetClass.ETF, 2.87, 2.79),
    ("BA", "Boeing", AssetClass.STOCK, 2.81, 2.73),
    ("META", "Meta", AssetClass.STOCK, 2.72, 2.65),
    ("PYPL", "PayPal", AssetClass.STOCK, 2.71, 2.64),
    ("MSFT", "Microsoft", AssetClass.STOCK, 2.68, 2.60),
    ("NFLX", "Netflix", AssetClass.STOCK, 2.67, 2.59),
    ("AMZN", "Amazon", AssetClass.STOCK, 2.59, 2.51),
    ("BABA", "Alibaba", AssetClass.STOCK, 2.56, 2.49),
    ("GOOG", "Alphabet", AssetClass.STOCK, 2.55, 2.48),
    ("LOW", "Lowe's", AssetClass.STOCK, 2.51, 2.44),
    ("LI", "Li Auto", AssetClass.STOCK, 2.46, 2.39),
    ("C", "Citigroup", AssetClass.STOCK, 2.41, 2.34),
    ("CVS", "CVS Health", AssetClass.STOCK, 2.40, 2.34),
    ("HD", "Home Depot", AssetClass.STOCK, 2.34, 2.27),
    ("WMT", "Walmart", AssetClass.STOCK, 2.20, 2.14),
    ("AXP", "American Express", AssetClass.STOCK, 2.14, 2.08),
    ("AAPL", "Apple", AssetClass.STOCK, 2.12, 2.06),
    ("MA", "Mastercard", AssetClass.STOCK, 2.07, 2.01),
    ("PFE", "Pfizer", AssetClass.STOCK, 1.92, 1.87),
    ("V", "Visa", AssetClass.STOCK, 1.87, 1.82),
    ("IWM", "Russell 2000", AssetClass.ETF, 1.70, 1.65),
    ("QQQ", "Nasdaq-100", AssetClass.ETF, 1.69, 1.65),
    ("GLD", "Oro ETF", AssetClass.ETF, 1.42, 1.38),
    ("SPX", "S&P 500 (índice)", AssetClass.INDEX, 1.03, 1.00),
    ("SPY", "S&P 500", AssetClass.ETF, 1.03, 1.00),
    ("DIA", "Dow 30", AssetClass.ETF, 1.01, 0.98),
]

_NOTES = {
    "SOXL": "Apalancado 3× — movimiento ya amplificado, decay diario.",
    "TNA": "Apalancado 3× — movimiento ya amplificado, decay diario.",
    "SPX": "No cacheado; movimiento = S&P 500 (≈SPY). Cash-settled, europeo, notional grande.",
    "DIA": "Confirmar 0DTE diario con el bróker.",
}


# 0DTE por ticker — VERIFICADO contra Polygon (probe 3 martes + 3 miércoles); fuente: app.py.
#   DAILY = índices/ETFs líquidos (todos los días). MWF = mega-caps + commodities (Lun/Mié/Vie).
#   resto = weekly (viernes), INCLUIDO DIA (¡NO es daily!) y GOOG/GOOGL.
_DAILY_0DTE = {"SPX", "SPY", "QQQ", "IWM"}
_MWF_0DTE = {"AAPL", "AMZN", "AVGO", "META", "MSFT", "NVDA", "TSLA", "GLD", "SLV", "USO"}


def _zerodte_for(tk: str) -> ZeroDTE:
    if tk in _DAILY_0DTE:
        return ZeroDTE.DAILY
    if tk in _MWF_0DTE:
        return ZeroDTE.MWF
    return ZeroDTE.FRIDAY


def _build() -> dict:
    out = {}
    for tk, name, cls, rng, vx in _DATA:
        bc = _BETA_CORR.get(tk, (None, None))
        out[tk] = TickerProfile(
            ticker=tk, name=name, asset_class=cls, daily_range_pct=rng, vol_x_spy=vx,
            zero_dte=_zerodte_for(tk),
            in_daily_core=tk in _DAILY_CORE, in_friday_roster=tk in _FRIDAY_ROSTER,
            beta_spy=bc[0], corr_spy=bc[1], notes=_NOTES.get(tk, ""))
    return out


PROFILES: dict = _build()


def get(ticker: str) -> Optional[TickerProfile]:
    return PROFILES.get(ticker.upper())


def all_profiles() -> list:
    """Todos, ordenados por movimiento descendente."""
    return sorted(PROFILES.values(), key=lambda p: p.daily_range_pct, reverse=True)


def daily_core() -> list:
    return [PROFILES[t] for t in ("SPY", "QQQ", "IWM")]


def friday_roster() -> list:
    return sorted((p for p in PROFILES.values() if p.in_friday_roster),
                  key=lambda p: p.daily_range_pct, reverse=True)


def tradeable_on(weekday: int) -> list:
    """Perfiles con 0DTE disponible ese día de la semana, por movimiento descendente."""
    return [p for p in all_profiles() if p.tradeable_on(weekday)]

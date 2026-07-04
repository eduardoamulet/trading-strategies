"""Unit tests del criterio COMPUESTO de selección de contrato «spread_itm_first»
(«Menor spread en rango óptimo sino Primer contrato cerca de ITM»).

Headless y PURO: downloader fake (bars sintéticos), filtro de spread deshabilitado vía
spread_cfg para que el rango de prima se evalúe por el OPEN del bar (determinista, sin NBBO).
Corré:  pytest tests/test_contract_selection.py -q
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # options_replay/ en el path
from engine import _probe_premium_range  # noqa: E402
from ucbatch.scenario import _selection  # noqa: E402

# ---------------------------------------------------------------- mapeo de etiquetas


@pytest.mark.parametrize("etiqueta,esperado", [
    ("Menor spread en rango óptimo", "spread"),
    ("Primer contrato cerca de ITM", "itm_first"),
    ("Menor spread en rango óptimo sino Primer contrato cerca de ITM", "spread_itm_first"),
    ("menor spread en rango optimo SINO primer contrato cerca de itm", "spread_itm_first"),
    ("", "spread"),                      # default histórico
])
def test_selection_mapea_etiquetas(etiqueta, esperado):
    assert _selection(etiqueta) == esperado


# ---------------------------------------------------------------- fixture del selector

START = pd.Timestamp("2026-03-02 09:30:00")
END = pd.Timestamp("2026-03-02 16:00:00")


class _FakeDownloader:
    """Devuelve un bar de apertura por OCC (open = prima de entrada del contrato)."""

    def __init__(self, opens_por_strike):
        self._opens = opens_por_strike       # {strike: open}

    def option(self, occ, date):
        # el OCC generado termina en strike*1000 con padding OCC de 8 dígitos
        strike = int(occ[-8:]) / 1000.0
        o = self._opens.get(strike)
        if o is None:
            return pd.DataFrame()
        return pd.DataFrame([{"timestamp": START, "open": o, "volume": 100.0}])


def _candidatos(strikes):
    # orden de la lista = cercanía a ATM (como el chain real); ticker OCC estilo Polygon
    return pd.DataFrame([{"strike_price": float(s),
                          "ticker": f"O:QQQ260302C{int(s * 1000):08d}"} for s in strikes])


_SIN_SPREAD = {"enable_spread_filter": False}     # prima por OPEN del bar; sin NBBO


def _correr(criterio, premium_min, premium_max, opens, strikes, spot=100.0):
    return _probe_premium_range(
        _FakeDownloader(opens), "QQQ", "2026-03-02", _candidatos(strikes), "C",
        START, END, premium_min, premium_max, max_probe=10,
        spot=spot, spread_cfg=_SIN_SPREAD, selection_criterion=criterio)


# ---------------------------------------------------------------- comportamiento

# Strikes: 102 (OTM, prima 4.0) y 98 (ITM depth=2, prima 5.0); spot=100.
_OPENS = {102.0: 4.0, 98.0: 5.0}
_STRIKES = [102.0, 98.0]


def test_spread_sin_contrato_no_compra():
    """Rango [1, 1.5]: ninguna prima entra → la Opción 1 pura NO compra (comportamiento intacto)."""
    pick, probes, tier = _correr("spread", 1.0, 1.5, _OPENS, _STRIKES)
    assert pick is None and tier == "" and len(probes) == 2


def test_compuesto_cae_al_1itm_cuando_opcion1_no_compra():
    """Mismo rango imposible: el compuesto hace fallback al 1-ITM (strike 98) con tier trazable."""
    pick, probes, tier = _correr("spread_itm_first", 1.0, 1.5, _OPENS, _STRIKES)
    assert pick is not None and pick.strike == 98.0
    assert tier == "itm_fallback"


def test_compuesto_respeta_opcion1_cuando_si_hay_contrato():
    """Rango [3.5, 4.5]: la prima 4.0 (strike 102) entra al óptimo → el compuesto elige
    EXACTAMENTE lo mismo que la Opción 1 pura (sin activar el fallback)."""
    pick_c, _, tier_c = _correr("spread_itm_first", 3.5, 4.5, _OPENS, _STRIKES)
    pick_s, _, tier_s = _correr("spread", 3.5, 4.5, _OPENS, _STRIKES)
    assert pick_c.strike == pick_s.strike == 102.0
    assert tier_c == tier_s == "optimo"


def test_compuesto_sin_ninguna_prima_valida_no_compra():
    """Sin bars para ningún strike: ni la Opción 1 ni el fallback tienen candidatos → no se compra."""
    pick, probes, tier = _correr("spread_itm_first", 1.0, 1.5, {}, _STRIKES)
    assert pick is None and tier == ""


def test_compuesto_fallback_prefiere_itm_sobre_otm():
    """En el fallback manda la cercanía a ITM (regla 1-ITM): 98 (depth 2) le gana a 102 (OTM),
    aunque el OTM esté primero en el chain."""
    opens = {102.0: 9.0, 98.0: 9.5}       # ambos fuera del rango [1, 1.5]
    pick, _, tier = _correr("spread_itm_first", 1.0, 1.5, opens, [102.0, 98.0])
    assert pick.strike == 98.0 and tier == "itm_fallback"

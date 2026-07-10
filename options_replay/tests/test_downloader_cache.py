"""Tests del guard de CACHE CONFIABLE del Downloader (2026-07-10): un parquet escrito
ANTES del cierre de su sesión (fetch intradía/pre-market) es parcial/vacío y se refetchea;
un quote 0/0 cacheado (fallo transitorio de la API) se trata como miss y no se persiste.
Este fue el veneno del verify-drift del 2026-07-08/09."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from downloader import Downloader  # noqa: E402

FECHA = "2026-07-08"        # sesión ya cerrada hace días


class FakeAdapter:
    def __init__(self):
        self.calls = {"under": 0, "quote": 0}
        self.quote_result = {"bid": 1.10, "ask": 1.14, "bid_size": 5, "ask_size": 7,
                             "spread": 0.04}

    def underlying_minute_bars(self, ticker, date, mult, span):
        self.calls["under"] += 1
        ts = pd.Timestamp(f"{date} 09:30", tz="America/New_York")
        return pd.DataFrame({"timestamp": [ts], "open": [1.0], "high": [1.0],
                             "low": [1.0], "close": [100.0], "volume": [1]})

    def option_quote_at(self, occ, ts):
        self.calls["quote"] += 1
        return dict(self.quote_result)


def _set_mtime_antes_del_cierre(path: Path, fecha: str) -> None:
    t = pd.Timestamp(f"{fecha} 10:00", tz="America/New_York").timestamp()
    os.utime(path, (t, t))


def test_barras_parciales_se_refetchean(tmp_path):
    ad = FakeAdapter()
    dl = Downloader(ad, tmp_path)
    dl.underlying("QQQ", FECHA)                      # 1er fetch → cachea (mtime = ahora ✓)
    assert ad.calls["under"] == 1
    dl.underlying("QQQ", FECHA)                      # cache confiable → NO refetchea
    assert ad.calls["under"] == 1
    p = tmp_path / "underlying" / f"QQQ_{FECHA}.parquet"
    _set_mtime_antes_del_cierre(p, FECHA)            # simular fetch intradía viejo
    dl.underlying("QQQ", FECHA)                      # stale → refetch y sobreescribe
    assert ad.calls["under"] == 2
    dl.underlying("QQQ", FECHA)                      # ya saneado → cache
    assert ad.calls["under"] == 2


def test_quote_cero_cacheado_se_trata_como_miss(tmp_path):
    ad = FakeAdapter()
    dl = Downloader(ad, tmp_path)
    ts = pd.Timestamp(f"{FECHA} 09:30", tz="America/New_York")
    # Veneno pre-existente: quote 0/0 cacheado (como los de QQQ 2026-07-08).
    qdir = tmp_path / "quotes"
    qdir.mkdir(parents=True, exist_ok=True)
    veneno = qdir / f"O_TST_{FECHA}_0930.parquet"
    pd.DataFrame([{"bid": 0, "ask": 0, "bid_size": 0, "ask_size": 0,
                   "spread": 0}]).to_parquet(veneno)
    q = dl.option_quote("O:TST", FECHA, ts)
    assert ad.calls["quote"] == 1                    # refetcheó pese al cache
    assert q["ask"] == 1.14                          # y devolvió el NBBO real


def test_quote_transitorio_no_se_persiste(tmp_path):
    ad = FakeAdapter()
    ad.quote_result = {"bid": None, "ask": None, "bid_size": None, "ask_size": None,
                       "spread": None}
    dl = Downloader(ad, tmp_path)
    ts = pd.Timestamp(f"{FECHA} 09:30", tz="America/New_York")
    dl.option_quote("O:TST", FECHA, ts)
    assert ad.calls["quote"] == 1
    assert not (tmp_path / "quotes" / f"O_TST_{FECHA}_0930.parquet").exists()  # no persistió
    dl.option_quote("O:TST", FECHA, ts)              # próxima corrida reintenta
    assert ad.calls["quote"] == 2


def test_quote_bueno_se_cachea_normal(tmp_path):
    ad = FakeAdapter()
    dl = Downloader(ad, tmp_path)
    ts = pd.Timestamp(f"{FECHA} 09:30", tz="America/New_York")
    dl.option_quote("O:TST", FECHA, ts)
    dl.option_quote("O:TST", FECHA, ts)              # 2ª lectura desde cache
    assert ad.calls["quote"] == 1

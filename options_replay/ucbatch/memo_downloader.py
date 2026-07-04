"""`MemoDownloader` — envuelve un Downloader y MEMOIZA sus reads por (método, args).

Motivo: en el batch, los 480 escenarios de un mismo día leen EXACTAMENTE los mismos datos
(underlying, chain, quotes del contrato — la selección de contrato sale del Data seed, no del
escenario). Sin cache, cada día se relee 480×. Con este wrapper (uno POR DÍA en el worker, que se
descarta al terminar), se lee 1× y las otras 479 son cache-hits → de 377.280 cargas a ~786.

Transparente: delega TODO al inner; solo intercepta los reads pesados para cachear. La memoria queda
acotada al día (el wrapper se crea y descarta por día).
"""
from __future__ import annotations

_MISS = object()

# Reads del Downloader que se repiten idénticos entre escenarios del mismo día:
_CACHED = frozenset(["underlying", "chain", "nearest_expiry", "option",
                     "option_quote", "option_quote_series"])


class MemoDownloader:
    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_cache", {})
        # Cache de SEGUNDO nivel (lo consume el engine): contexto invariante por posición
        # (selección de contrato + merged + NBBO + primas) — los 480 escenarios de un
        # ticker/día lo comparten y el engine lo computa 1× (ver _prepare_iteration_context).
        # Vive y muere con este wrapper (= con el día del worker), igual que _cache.
        object.__setattr__(self, "_day_ctx", {})

    def __getattr__(self, name):
        inner = object.__getattribute__(self, "_inner")
        val = getattr(inner, name)
        if name not in _CACHED or not callable(val):
            return val                                  # atributos / métodos no cacheados → directo
        cache = object.__getattribute__(self, "_cache")

        def _wrapped(*a, **k):
            try:
                key = (name, a, tuple(sorted(k.items())))
                hit = cache.get(key, _MISS)
                if hit is not _MISS:
                    return hit
                res = val(*a, **k)
                cache[key] = res
                return res
            except TypeError:                           # args no hashables → sin cache
                return val(*a, **k)
        return _wrapped

    def __setattr__(self, name, value):
        # p.ej. `dl.resolution = ...` → va al inner (no rompe el estado del Downloader real)
        if name in ("_inner", "_cache", "_day_ctx"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_inner"), name, value)

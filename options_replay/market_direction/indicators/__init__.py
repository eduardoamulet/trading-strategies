"""indicators — cómputo CAUSAL de los ~50 features (solo con velas ≤ t).

Familias puras (volatility · trend · momentum · volume · structure) + `FeatureBuilder`, que las
ensambla en el `FeatureSet` del dominio. Ningún indicador mira más allá de `t`.
"""
from .builder import FeatureBuilder

__all__ = ["FeatureBuilder"]

"""Black-Scholes — precio, volatilidad implícita (bisección) y greeks. Solo `math`, sin dependencias.

Se usa para ENRIQUECER cada posición del backtest con Delta/Gamma/Theta a partir de datos YA
capturados (spot, strike, prima de entrada = mid del NBBO, y tiempo a las 16:00 ET del mismo día en
0DTE). NO agrega llamadas a Polygon. Aproximado (europeo, sin dividendos intradía; para 0DTE de índices
/ETFs es razonable, aunque cerca del vencimiento gamma/theta son numéricamente extremos).
"""
from __future__ import annotations

import math

_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Precio Black-Scholes de una call/put europea."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if is_call else (K - S))       # valor intrínseco
    st = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / st
    d2 = d1 - st
    if is_call:
        return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float, r: float, is_call: bool):
    """IV por bisección (robusta). None si el precio es inválido o < valor intrínseco."""
    if price is None or price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    intr = max(0.0, (S - K) if is_call else (K - S))
    if price < intr - 1e-6:
        return None
    lo, hi = 1e-3, 5.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        p = bs_price(S, K, T, r, mid, is_call)
        if abs(p - price) < 1e-5:
            return mid
        if p > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def greeks(S: float, K: float, T: float, r: float, sigma: float, is_call: bool):
    """(delta, gamma, theta_por_día). None si los inputs no son válidos."""
    if not sigma or sigma <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None, None, None
    st = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / st
    d2 = d1 - st
    delta = _cdf(d1) if is_call else _cdf(d1) - 1.0
    gamma = _pdf(d1) / (S * st)
    base = -S * _pdf(d1) * sigma / (2.0 * math.sqrt(T))
    theta_yr = (base - r * K * math.exp(-r * T) * _cdf(d2)) if is_call \
        else (base + r * K * math.exp(-r * T) * _cdf(-d2))
    return round(delta, 4), round(gamma, 6), round(theta_yr / 365.0, 4)


def leg_greeks(spot, strike, premium, entry_dt, is_call, r: float = 0.04):
    """(delta, gamma, theta, iv) de una pierna a partir de spot/strike/prima + tiempo a las 16:00 ET
    del día de `entry_dt` (0DTE). Todo capturado por el backtest. Devuelve Nones si falta algo."""
    if not (spot and strike and premium and premium > 0):
        return None, None, None, None
    try:
        et = entry_dt.tz_convert("America/New_York") if getattr(entry_dt, "tzinfo", None) else entry_dt
        secs = 16 * 3600 - (et.hour * 3600 + et.minute * 60 + getattr(et, "second", 0))
        T = max(secs, 120) / (365.0 * 24 * 3600)      # piso 2 min para no explotar cerca del cierre
    except Exception:
        return None, None, None, None
    iv = implied_vol(float(premium), float(spot), float(strike), T, r, is_call)
    if iv is None:
        return None, None, None, None
    d, g, th = greeks(float(spot), float(strike), T, r, iv, is_call)
    return d, g, th, round(iv, 4)

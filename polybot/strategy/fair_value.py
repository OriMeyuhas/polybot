"""Fair value model for crypto Up/Down prediction markets.

Resolution rule: Up wins if crypto price at end >= Price to Beat (PTB).
Uses binary option pricing: P(Up) = Phi(d) where
d = ln(S/K) / (sigma * sqrt(T)) and sigma is realized volatility.

Adapted from polytrader's fair_value.py — no external dependencies.
"""

import math
from decimal import Decimal

SECONDS_PER_YEAR = 365.25 * 24 * 3600
_D_CLAMP = 6.0  # Clamp d to [-6, 6] to avoid numerical edge cases


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def p_fair_up(
    start_price: Decimal | float | None,
    current_price: Decimal | float | None,
    seconds_to_resolution: float,
    vol_annualized: float | None = None,
) -> float:
    """P(Up) = P(crypto_end >= Price_to_Beat) using binary option pricing.

    Uses the normal CDF: P(Up) = Phi(d) where d = ln(S/K) / (sigma * sqrt(T)).
    Naturally converges to 0 or 1 as T -> 0 without hardcoded thresholds.
    Adapts to volatility: high vol = less certain, low vol = more decisive.

    Returns float in [0.01, 0.99]. Returns 0.5 when inputs are missing.
    """
    if start_price is None or current_price is None:
        return 0.5

    s = float(start_price)
    c = float(current_price)
    if s <= 0 or c <= 0:
        return 0.5

    if seconds_to_resolution <= 0:
        return 0.99 if c >= s else 0.01

    if vol_annualized is None or vol_annualized <= 0:
        return 0.5

    t_years = seconds_to_resolution / SECONDS_PER_YEAR
    denom = vol_annualized * math.sqrt(t_years)
    if denom < 1e-15:
        return 0.99 if c >= s else 0.01

    d = math.log(c / s) / denom
    d = max(-_D_CLAMP, min(_D_CLAMP, d))

    prob = _norm_cdf(d)
    return max(0.01, min(0.99, prob))


def certainty(p_up: float) -> float:
    """Return certainty level: max(p_up, 1-p_up) as float in [0.5, 0.99]."""
    return max(p_up, 1.0 - p_up)

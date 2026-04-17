"""Polymarket fee calculation utilities."""


def compute_fee(price: float, fee_rate: float) -> float:
    """Polymarket maker fee: fee_rate * min(price, 1 - price).

    The fee is symmetric around 0.50 — prices near 0 or 1 have minimal fees,
    prices near 0.50 have maximum fees.

    Returns 0.0 if fee_rate is 0 (disables all fee adjustments).
    """
    if fee_rate <= 0.0:
        return 0.0
    return fee_rate * min(price, 1.0 - price)

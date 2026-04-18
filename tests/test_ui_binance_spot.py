"""Regression tests for UI "BINANCE SPOT" price strip rendering.

Recurring bug family: the UI displays a Chainlink-leaning / RTDS-derived price
under a label that says "BINANCE SPOT". That defeats the whole arbitrage edge —
the lag between direct Binance WS and Polymarket's Chainlink-derived reference
is the thing we're trading against, so showing the laggy number there hides the
signal.

These tests lock the contract: the widget must always read direct Binance WS
prices when present, falling back to the blended `spots` dict only when Binance
WS is offline.

Do not delete these tests without reading
memory/feedback_ui_binance_spot_recurring_bug.md.
"""

from polybot.bot import _ui_binance_spot_values


def test_uses_binance_prices_when_present():
    binance_prices = {"BTC": 76105.50, "ETH": 3420.10}
    spots = {"BTC": 76100.00, "ETH": 3418.00}  # RTDS-blended, slightly laggy
    result = _ui_binance_spot_values(binance_prices, spots)
    assert result == {"BTC": 76105.50, "ETH": 3420.10}, \
        "Direct Binance WS must win — spots dict is RTDS-blended and defeats the arb edge"


def test_falls_back_to_spots_when_binance_empty():
    binance_prices: dict = {}
    spots = {"BTC": 76100.00}
    result = _ui_binance_spot_values(binance_prices, spots)
    assert result == {"BTC": 76100.00}


def test_falls_back_to_spots_when_binance_none():
    spots = {"BTC": 76100.00}
    result = _ui_binance_spot_values(None, spots)  # type: ignore[arg-type]
    assert result == {"BTC": 76100.00}


def test_returns_empty_when_both_missing():
    assert _ui_binance_spot_values({}, {}) == {}


def test_returns_empty_when_both_none():
    assert _ui_binance_spot_values(None, None) == {}  # type: ignore[arg-type]


def test_does_not_mutate_inputs():
    binance_prices = {"BTC": 76105.50}
    spots = {"BTC": 76100.00}
    result = _ui_binance_spot_values(binance_prices, spots)
    result["BTC"] = 999999.99
    assert binance_prices == {"BTC": 76105.50}, "helper must return a fresh dict, not a live view"


def test_per_symbol_resolution_is_all_or_nothing():
    """If Binance has ANY symbols, we use Binance dict as a whole.

    Mixing on a per-symbol basis would create visually confusing rows where
    one symbol is live-Binance and another is RTDS-blended. The widget picks
    one source and sticks with it.
    """
    binance_prices = {"BTC": 76105.50}  # ETH missing
    spots = {"BTC": 76100.00, "ETH": 3418.00}
    result = _ui_binance_spot_values(binance_prices, spots)
    assert result == {"BTC": 76105.50}  # ETH is not backfilled from spots

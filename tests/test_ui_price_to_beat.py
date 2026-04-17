"""Regression tests for UI "Price to Beat" rendering.

This is a recurring bug: the UI's Target field drifts from Polymarket's
displayed Price to Beat whenever someone touches the price/market-discovery
code. These tests lock in the contract: UI must display Gamma's priceToBeat
exactly, falling back to Chainlink/Binance only when Gamma is missing.

Do not delete these tests without reading
memory/feedback_price_to_beat_recurring_bug.md.
"""

from dataclasses import dataclass

from polybot.bot import _ui_price_to_beat


@dataclass
class _MktStub:
    price_to_beat: str = ""


def test_uses_gamma_price_to_beat_when_present():
    mkt = _MktStub(price_to_beat="75291.47")
    result = _ui_price_to_beat(mkt, chainlink_price=75301.88, binance_snapshot=75305.00)
    assert result == 75291.47, "Gamma priceToBeat must win — Polymarket UI shows this exact value"


def test_falls_back_to_chainlink_when_gamma_missing():
    mkt = _MktStub(price_to_beat="")
    result = _ui_price_to_beat(mkt, chainlink_price=75301.88, binance_snapshot=75305.00)
    assert result == 75301.88


def test_falls_back_to_binance_when_gamma_and_chainlink_missing():
    mkt = _MktStub(price_to_beat="")
    result = _ui_price_to_beat(mkt, chainlink_price=None, binance_snapshot=75305.00)
    assert result == 75305.00


def test_returns_zero_when_all_sources_missing():
    mkt = _MktStub(price_to_beat="")
    result = _ui_price_to_beat(mkt, chainlink_price=None, binance_snapshot=0)
    assert result == 0.0


def test_ignores_zero_or_negative_gamma_value():
    mkt = _MktStub(price_to_beat="0")
    result = _ui_price_to_beat(mkt, chainlink_price=75301.88, binance_snapshot=0)
    assert result == 75301.88


def test_ignores_malformed_gamma_value():
    mkt = _MktStub(price_to_beat="not-a-number")
    result = _ui_price_to_beat(mkt, chainlink_price=75301.88, binance_snapshot=0)
    assert result == 75301.88

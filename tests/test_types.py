import pytest
from polybot.types import (
    Side, StrategyType, Opportunity, Position, MarketWindow, OrderRecord,
)


def test_side_enum():
    assert Side.UP.value == "UP"
    assert Side.DOWN.value == "DOWN"


def test_strategy_type_enum():
    assert StrategyType.DIRECTIONAL.value == "DIRECTIONAL"
    assert StrategyType.SPREAD.value == "SPREAD"


def test_opportunity_directional():
    opp = Opportunity(
        strategy=StrategyType.DIRECTIONAL,
        market_id="btc-updown-15m-123",
        side=Side.UP,
        price=0.85,
        edge=0.12,
        confidence=0.003,
    )
    assert opp.strategy == StrategyType.DIRECTIONAL
    assert opp.price == 0.85
    assert opp.up_price is None
    assert opp.dn_price is None


def test_opportunity_spread():
    opp = Opportunity(
        strategy=StrategyType.SPREAD,
        market_id="btc-updown-15m-123",
        up_price=0.48,
        dn_price=0.49,
        edge=0.03,
    )
    assert opp.strategy == StrategyType.SPREAD
    assert opp.up_price + opp.dn_price == pytest.approx(0.97)


def test_position_pair_cost():
    pos = Position(market_id="btc-updown-15m-123")
    pos.up_qty = 100.0
    pos.up_cost = 48.0
    pos.dn_qty = 100.0
    pos.dn_cost = 49.0
    assert pos.pair_cost() == pytest.approx(0.97)
    assert pos.min_qty() == 100.0


def test_position_pair_cost_empty():
    pos = Position(market_id="test")
    assert pos.pair_cost() == 0.0
    assert pos.min_qty() == 0.0


def test_position_profit_if_up_wins():
    pos = Position(market_id="test")
    pos.up_qty = 1000.0
    pos.up_cost = 480.0  # avg 0.48
    pos.dn_qty = 1000.0
    pos.dn_cost = 490.0  # avg 0.49
    # Pi_UP = Su*(1 - Pu) - Sd*Pd = 1000*(1-0.48) - 1000*0.49 = 520 - 490 = 30
    assert pos.profit_if_up() == pytest.approx(30.0)
    assert pos.profit_if_down() == pytest.approx(30.0)


def test_market_window():
    mw = MarketWindow(
        market_id="btc-updown-15m-123",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1900,
    )
    assert mw.elapsed(1500) == 500
    assert mw.remaining(1500) == 400
    assert mw.is_active(1500) is True
    assert mw.is_active(2000) is False

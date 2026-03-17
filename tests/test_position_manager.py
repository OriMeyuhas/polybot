import pytest
from polybot.types import Side, StrategyType, Opportunity, Position
from polybot.position_manager import PositionManager
from polybot.config import BotConfig


@pytest.fixture
def cfg():
    return BotConfig(position_size_fraction=0.10, max_pair_cost=0.985)


@pytest.fixture
def pm(cfg):
    return PositionManager(cfg, bankroll=10_000.0)


class TestDirectionalSizing:
    def test_basic_sizing(self, pm):
        opp = Opportunity(
            strategy=StrategyType.DIRECTIONAL,
            market_id="m1",
            side=Side.UP,
            price=0.85,
            edge=0.15,
        )
        result = pm.compute_order_size(opp, book_depth=5000.0)
        assert result is not None
        side, qty = result
        assert side == Side.UP
        assert qty == pytest.approx(1176.47, rel=0.01)

    def test_capped_by_book_depth(self, pm):
        opp = Opportunity(
            strategy=StrategyType.DIRECTIONAL,
            market_id="m1",
            side=Side.DOWN,
            price=0.10,
            edge=0.90,
        )
        result = pm.compute_order_size(opp, book_depth=100.0)
        assert result is not None
        side, qty = result
        assert qty == pytest.approx(50.0)


class TestSpreadSizing:
    def test_basic_spread_sizing(self, pm):
        opp = Opportunity(
            strategy=StrategyType.SPREAD,
            market_id="m1",
            up_price=0.48,
            dn_price=0.49,
            edge=0.03,
        )
        result = pm.compute_spread_size(opp)
        assert result is not None
        up_qty, dn_qty = result
        assert up_qty == pytest.approx(dn_qty)
        assert up_qty == pytest.approx(1020.41, rel=0.01)

    def test_spread_rejected_if_pair_cost_too_high(self, pm):
        pos = Position(market_id="m1")
        pos.up_qty = 1000.0
        pos.up_cost = 490.0
        pos.dn_qty = 1000.0
        pos.dn_cost = 500.0
        pm.positions["m1"] = pos

        opp = Opportunity(
            strategy=StrategyType.SPREAD,
            market_id="m1",
            up_price=0.50,
            dn_price=0.50,
            edge=0.00,
        )
        result = pm.compute_spread_size(opp)
        assert result is None


class TestPositionTracking:
    def test_update_position_directional(self, pm):
        pm.update_position("m1", Side.UP, qty=100.0, cost=85.0)
        pos = pm.positions["m1"]
        assert pos.up_qty == 100.0
        assert pos.up_cost == 85.0
        assert pos.dn_qty == 0.0

    def test_update_position_accumulates(self, pm):
        pm.update_position("m1", Side.UP, qty=100.0, cost=85.0)
        pm.update_position("m1", Side.DOWN, qty=100.0, cost=49.0)
        pos = pm.positions["m1"]
        assert pos.up_qty == 100.0
        assert pos.dn_qty == 100.0
        assert pos.pair_cost() == pytest.approx(1.34)

    def test_remove_position(self, pm):
        pm.update_position("m1", Side.UP, qty=100.0, cost=85.0)
        pm.remove_position("m1")
        assert "m1" not in pm.positions

    def test_active_position_count(self, pm):
        pm.update_position("m1", Side.UP, qty=100.0, cost=85.0)
        pm.update_position("m2", Side.DOWN, qty=50.0, cost=25.0)
        assert pm.active_position_count() == 2

    def test_update_bankroll(self, pm):
        pm.update_bankroll(10_500.0)
        assert pm.bankroll == 10_500.0

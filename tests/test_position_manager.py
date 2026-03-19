import pytest
from polybot.types import Side, Position
from polybot.position_manager import PositionManager
from polybot.config import BotConfig


@pytest.fixture
def cfg():
    return BotConfig(position_size_fraction=0.10, max_pair_cost=0.985)


@pytest.fixture
def pm(cfg):
    return PositionManager(cfg, bankroll=10_000.0)


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


class TestSettlementStates:
    def test_mark_pending_settlement(self, pm):
        pm.mark_pending_settlement("m1")
        assert "m1" in pm.get_pending_settlements()

    def test_mark_failed_moves_from_pending(self, pm):
        pm.mark_pending_settlement("m1")
        pm.mark_failed_settlement("m1")
        assert "m1" not in pm.get_pending_settlements()
        assert "m1" in pm.get_failed_settlements()

    def test_complete_settlement_removes_from_both(self, pm):
        pm.mark_pending_settlement("m1")
        pm.mark_failed_settlement("m2")
        pm.mark_pending_settlement("m2")  # also in pending
        pm.complete_settlement("m1")
        pm.complete_settlement("m2")
        assert pm.get_pending_settlements() == []
        assert pm.get_failed_settlements() == []

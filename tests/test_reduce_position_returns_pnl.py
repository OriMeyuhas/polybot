"""Tests for reduce_position returning realized PnL and debiting cost proportionally.

TDD: these tests are written BEFORE the implementation change.
They verify the new signature: reduce_position(market_id, side, qty, proceeds) -> float
"""

import pytest
from polybot.types import Side
from polybot.strategy.position_manager import PositionManager
from polybot.config import BotConfig


@pytest.fixture
def cfg():
    return BotConfig(position_size_fraction=0.10, max_pair_cost=0.985)


@pytest.fixture
def pm(cfg):
    return PositionManager(cfg, bankroll=1000.0)


class TestReducePositionReturnsPnl:
    def test_reduce_position_returns_float(self, pm):
        """reduce_position must return a float, not None."""
        pm.update_position("m1", Side.UP, qty=100.0, cost=45.0)
        result = pm.reduce_position("m1", Side.UP, qty=50.0, proceeds=30.0)
        assert result is not None
        assert isinstance(result, float)

    def test_reduce_position_full_sell_returns_pnl(self, pm):
        """Selling all shares: realized = proceeds - cost_basis."""
        # Buy 100 UP shares at average $0.45 each → cost = $45.00
        pm.update_position("m1", Side.UP, qty=100.0, cost=45.0)
        # Sell all 100 shares at $0.60 each → proceeds = $60.00
        # realized = 60.00 - 45.00 = $15.00
        realized = pm.reduce_position("m1", Side.UP, qty=100.0, proceeds=60.0)
        assert realized == pytest.approx(15.0)

    def test_reduce_position_partial_sell_returns_proportional_pnl(self, pm):
        """Selling 50% of shares debits 50% of cost_basis."""
        # Buy 100 UP shares at avg $0.45 → cost = $45.00
        pm.update_position("m1", Side.UP, qty=100.0, cost=45.0)
        # Sell 50 shares at $0.55 → proceeds = $27.50
        # proportional cost_basis = 45.00 * (50/100) = $22.50
        # realized = 27.50 - 22.50 = $5.00
        realized = pm.reduce_position("m1", Side.UP, qty=50.0, proceeds=27.5)
        assert realized == pytest.approx(5.0)

    def test_reduce_position_debits_up_cost_proportionally(self, pm):
        """After partial sell, remaining up_cost is proportionally reduced."""
        pm.update_position("m1", Side.UP, qty=100.0, cost=45.0)
        pm.reduce_position("m1", Side.UP, qty=40.0, proceeds=24.0)
        pos = pm.positions["m1"]
        # 60% of shares remain → 60% of cost remains
        assert pos.up_cost == pytest.approx(45.0 * 0.6)
        assert pos.up_qty == pytest.approx(60.0)

    def test_reduce_position_debits_dn_cost_proportionally(self, pm):
        """Selling DOWN side debits dn_cost proportionally."""
        pm.update_position("m1", Side.DOWN, qty=80.0, cost=32.0)
        pm.reduce_position("m1", Side.DOWN, qty=20.0, proceeds=10.0)
        pos = pm.positions["m1"]
        # 75% of shares remain → 75% of cost remains = 24.0
        assert pos.dn_cost == pytest.approx(32.0 * 0.75)
        assert pos.dn_qty == pytest.approx(60.0)

    def test_reduce_position_dn_side_pnl(self, pm):
        """Selling DOWN side returns correct realized PnL."""
        pm.update_position("m1", Side.DOWN, qty=80.0, cost=32.0)
        # Sell 80 shares at $0.50 → proceeds = $40.00
        # cost_basis = $32.00
        # realized = $8.00
        realized = pm.reduce_position("m1", Side.DOWN, qty=80.0, proceeds=40.0)
        assert realized == pytest.approx(8.0)

    def test_reduce_position_missing_market_returns_zero(self, pm):
        """reduce_position on unknown market_id returns 0.0 (not None)."""
        result = pm.reduce_position("nonexistent", Side.UP, qty=10.0, proceeds=5.0)
        assert result == pytest.approx(0.0)

    def test_reduce_position_sell_loss_returns_negative_pnl(self, pm):
        """If sold proceeds < cost_basis, realized PnL is negative."""
        pm.update_position("m1", Side.UP, qty=100.0, cost=50.0)
        # Sell at $0.30 instead of $0.50 → proceeds = $30.00 < cost $50.00
        realized = pm.reduce_position("m1", Side.UP, qty=100.0, proceeds=30.0)
        assert realized == pytest.approx(-20.0)

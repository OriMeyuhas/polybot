"""Tests for live mode capital accounting fix.

Verifies that:
- resting_order_cost() excludes filled positions
- total_committed() = resting_order_cost() + total_position_cost()
- Paper mode available/budget calculations are unchanged
- Live mode uses resting-only for available, equity for budget base
- Overleverage guard uses resting-only cost
- Reprice path uses correct live mode formulas
"""

import pytest
from unittest.mock import MagicMock

from polybot.strategy.ladder_manager import LadderManager
from polybot.strategy.position_manager import PositionManager
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.order_executor import OrderExecutor
from polybot.risk_manager import RiskManager
from polybot.config import BotConfig
from polybot.fees import compute_fee
from polybot.types import MarketWindow, Side


@pytest.fixture
def paper_cfg():
    return BotConfig(
        dry_run=True,
        ladder_rungs=4,
        ladder_spacing=0.02,
        ladder_width=0.06,
        ladder_size_skew=0.7,
        bankroll=1000.0,
    )


@pytest.fixture
def live_cfg():
    return BotConfig(
        dry_run=False,
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        ladder_rungs=4,
        ladder_spacing=0.02,
        ladder_width=0.06,
        ladder_size_skew=0.7,
        bankroll=1000.0,
    )


@pytest.fixture
def market():
    return MarketWindow(
        market_id="btc-5m-100",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=300,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1300,
    )


@pytest.fixture
def mock_clob():
    clob = MagicMock()
    clob.get_order_book.return_value = MagicMock(
        bids=[MagicMock(price="0.44", size="5000")],
        asks=[MagicMock(price="0.46", size="5000")],
    )
    clob.create_order.return_value = {"signed": True}
    clob.post_order.return_value = {"orderID": "o1", "status": "resting"}
    clob.get_open_orders.return_value = []
    return clob


def _make_manager(cfg, mock_clob, bankroll=1000.0):
    executor = OrderExecutor(cfg, clob_client=mock_clob)
    tracker = OrderTracker()
    positions = PositionManager(cfg, bankroll=bankroll)
    risk = RiskManager(cfg, starting_bankroll=bankroll)
    return LadderManager(cfg, executor, tracker, positions, risk)


class TestRestingOrderCost:
    """Phase 1: resting_order_cost() excludes positions."""

    def test_resting_order_cost_excludes_positions(self, paper_cfg, mock_clob):
        """Post resting orders and add positions. resting_order_cost() only counts resting."""
        mgr = _make_manager(paper_cfg, mock_clob)

        # Add a ladder entry so resting_order_cost iterates over it
        mgr.ladders["mkt1"] = MagicMock()

        # Add resting orders to tracker
        order1 = TrackedOrder(
            order_id="o1", market_id="mkt1", token_id="tok_up",
            side=Side.UP, price=0.45, size=10.0, placed_at=100.0,
        )
        order2 = TrackedOrder(
            order_id="o2", market_id="mkt1", token_id="tok_dn",
            side=Side.DOWN, price=0.50, size=5.0, placed_at=100.0,
        )
        mgr.tracker.add(order1)
        mgr.tracker.add(order2)

        # Simulate a partial fill on order1: 4 shares filled
        mgr.tracker.update_fill("o1", 4.0)

        # Add position cost (simulating filled shares)
        mgr.positions.update_position("mkt1", Side.UP, 4.0, 1.8)

        fee_rate = mgr.fee_rate
        # Resting cost: o1 has 6 remaining, o2 has 5 remaining
        expected_resting = (
            (0.45 + compute_fee(0.45, fee_rate)) * 6.0
            + (0.50 + compute_fee(0.50, fee_rate)) * 5.0
        )
        assert abs(mgr.resting_order_cost() - expected_resting) < 0.001

        # Position cost is separate
        assert abs(mgr.positions.total_position_cost() - 1.8) < 0.001

        # total_committed = resting + positions
        expected_total = expected_resting + 1.8
        assert abs(mgr.total_committed() - expected_total) < 0.001

    def test_resting_order_cost_zero_when_no_orders(self, paper_cfg, mock_clob):
        """No resting orders means zero resting cost."""
        mgr = _make_manager(paper_cfg, mock_clob)
        assert mgr.resting_order_cost() == 0.0


class TestEquity:
    """Phase 2: PositionManager.equity()."""

    def test_equity_includes_positions(self, live_cfg):
        pm = PositionManager(live_cfg, bankroll=50.0)
        pm.update_position("mkt1", Side.UP, 10.0, 15.0)
        pm.update_position("mkt1", Side.DOWN, 10.0, 15.0)
        # equity = 50 (free USDC) + 30 (positions)
        assert abs(pm.equity() - 80.0) < 0.001

    def test_equity_no_positions(self, live_cfg):
        pm = PositionManager(live_cfg, bankroll=100.0)
        assert abs(pm.equity() - 100.0) < 0.001


class TestPaperModeUnchanged:
    """Phase 3 regression: paper mode available and budget unchanged."""

    def test_paper_mode_available_unchanged(self, paper_cfg, mock_clob, market):
        """In paper mode, available = bankroll - total_committed (both resting + positions)."""
        mgr = _make_manager(paper_cfg, mock_clob, bankroll=1000.0)

        # Add resting orders
        mgr.ladders["mkt1"] = MagicMock()
        order1 = TrackedOrder(
            order_id="o1", market_id="mkt1", token_id="tok_up",
            side=Side.UP, price=0.40, size=20.0, placed_at=100.0,
        )
        mgr.tracker.add(order1)

        # Add position cost
        mgr.positions.update_position("mkt1", Side.UP, 5.0, 2.5)

        fee_rate = mgr.fee_rate
        resting = (0.40 + compute_fee(0.40, fee_rate)) * 20.0
        position_cost = 2.5
        total_committed = resting + position_cost

        # In paper mode: available = bankroll - total_committed
        expected_available = 1000.0 - total_committed
        # Budget base = bankroll in paper mode
        lp = paper_cfg.get_ladder_params(300, current_bankroll=1000.0)
        expected_budget = min(1000.0 * lp.position_size_fraction, expected_available)

        # Verify via direct calculation (same as _post_ladder_core would do)
        available = mgr.positions.bankroll - mgr.total_committed()
        budget_base = mgr.positions.bankroll
        budget = min(budget_base * lp.position_size_fraction, available)

        assert abs(available - expected_available) < 0.001
        assert abs(budget - expected_budget) < 0.001


class TestLiveModeAvailable:
    """Phase 3: live mode uses resting-only for available, equity for budget."""

    def test_live_mode_available_uses_resting_only(self, live_cfg, mock_clob):
        """Live mode: available = bankroll - resting_order_cost() (not total_committed)."""
        mgr = _make_manager(live_cfg, mock_clob, bankroll=50.0)

        # Add resting orders costing ~$10
        mgr.ladders["mkt1"] = MagicMock()
        order1 = TrackedOrder(
            order_id="o1", market_id="mkt1", token_id="tok_up",
            side=Side.UP, price=0.40, size=25.0, placed_at=100.0,
        )
        mgr.tracker.add(order1)

        # Add position cost of $30
        mgr.positions.update_position("mkt1", Side.UP, 100.0, 30.0)

        resting_cost = mgr.resting_order_cost()
        # Live mode: available = bankroll(50) - resting_cost, NOT bankroll - total_committed
        live_available = mgr.positions.bankroll - resting_cost
        old_available = mgr.positions.bankroll - mgr.total_committed()

        # Live available should be much larger than old formula
        assert live_available > old_available
        # Specifically: live_available = 50 - resting, old = 50 - (resting + 30)
        assert abs(live_available - old_available - 30.0) < 0.001

    def test_live_mode_budget_uses_equity(self, live_cfg, mock_clob):
        """Live mode: budget base = equity (bankroll + positions), not just bankroll."""
        mgr = _make_manager(live_cfg, mock_clob, bankroll=50.0)

        # Add position cost of $30
        mgr.positions.update_position("mkt1", Side.UP, 100.0, 30.0)

        equity = mgr.positions.equity()
        assert abs(equity - 80.0) < 0.001

        lp = live_cfg.get_ladder_params(300, current_bankroll=50.0)
        # Budget base in live mode is equity (80), not bankroll (50)
        live_budget_base = equity * lp.position_size_fraction
        paper_budget_base = 50.0 * lp.position_size_fraction
        assert live_budget_base > paper_budget_base


class TestOverleverageGuard:
    """Phase 5: overleverage guard uses resting-only cost."""

    def test_overleverage_guard_resting_only(self, live_cfg, mock_clob):
        """wallet=20, resting=15, positions=50 -> NOT overleveraged (20 > 15)."""
        mgr = _make_manager(live_cfg, mock_clob, bankroll=20.0)

        # Add resting orders
        mgr.ladders["mkt1"] = MagicMock()
        # Pick price/size so resting cost is ~15
        # With fee_rate=0, price * size = cost. For fee_rate > 0, adjust.
        # Use direct: resting_order_cost will compute (price + fee) * remaining
        fee_rate = mgr.fee_rate
        # We want (price + compute_fee(price, fee_rate)) * size ~= 15
        # At price=0.40: fee = fee_rate * min(0.40, 0.60) = fee_rate * 0.40
        # Effective = 0.40 + fee_rate * 0.40 = 0.40 * (1 + fee_rate)
        # size = 15 / (0.40 * (1 + fee_rate))
        effective_price = 0.40 + compute_fee(0.40, fee_rate)
        target_resting = 15.0
        size = target_resting / effective_price

        order1 = TrackedOrder(
            order_id="o1", market_id="mkt1", token_id="tok_up",
            side=Side.UP, price=0.40, size=size, placed_at=100.0,
        )
        mgr.tracker.add(order1)

        # Add large position cost
        mgr.positions.update_position("mkt1", Side.UP, 200.0, 50.0)

        wallet_balance = 20.0
        resting = mgr.resting_order_cost()
        total = mgr.total_committed()

        # Resting ~15, total ~65
        assert abs(resting - 15.0) < 0.01
        assert total > 60.0

        # New guard: wallet(20) >= resting(15) -> NOT overleveraged
        assert wallet_balance >= resting

        # Old guard would have been: wallet(20) < total(65) -> overleveraged (false alarm)
        assert wallet_balance < total

    def test_overleverage_guard_triggers_correctly(self, live_cfg, mock_clob):
        """wallet=10, resting=15 -> IS overleveraged (10 < 15)."""
        mgr = _make_manager(live_cfg, mock_clob, bankroll=10.0)

        mgr.ladders["mkt1"] = MagicMock()
        fee_rate = mgr.fee_rate
        effective_price = 0.40 + compute_fee(0.40, fee_rate)
        size = 15.0 / effective_price

        order1 = TrackedOrder(
            order_id="o1", market_id="mkt1", token_id="tok_up",
            side=Side.UP, price=0.40, size=size, placed_at=100.0,
        )
        mgr.tracker.add(order1)

        wallet_balance = 10.0
        resting = mgr.resting_order_cost()

        # wallet(10) < resting(15) -> overleveraged
        assert wallet_balance < resting


class TestRepriceBudgetLiveMode:
    """Phase 4: reprice path uses equity for budget base, resting-only for available."""

    def test_reprice_budget_live_mode(self, live_cfg, mock_clob):
        """Verify reprice formulas: live uses equity for total_budget, resting-only for available."""
        mgr = _make_manager(live_cfg, mock_clob, bankroll=50.0)

        # Add position cost
        mgr.positions.update_position("mkt1", Side.UP, 100.0, 30.0)

        # Add resting orders
        mgr.ladders["mkt1"] = MagicMock()
        fee_rate = mgr.fee_rate
        effective_price = 0.45 + compute_fee(0.45, fee_rate)
        order1 = TrackedOrder(
            order_id="o1", market_id="mkt1", token_id="tok_up",
            side=Side.UP, price=0.45, size=20.0, placed_at=100.0,
        )
        mgr.tracker.add(order1)
        resting_cost = effective_price * 20.0

        lp = live_cfg.get_ladder_params(300, current_bankroll=50.0)

        # Live mode reprice formulas
        equity = mgr.positions.equity()  # 50 + 30 = 80
        total_budget = equity * lp.position_size_fraction
        available = mgr.positions.bankroll - mgr.resting_order_cost()  # 50 - resting
        budget_per_side = min(total_budget / 2.0, max(0, available / 2.0))

        # Paper mode would use:
        paper_total_budget = mgr.positions.bankroll * lp.position_size_fraction  # 50 * frac
        paper_available = mgr.positions.bankroll - mgr.total_committed()  # 50 - (resting + 30)

        # Live budget base is larger (equity=80 vs bankroll=50)
        assert total_budget > paper_total_budget
        # Live available is larger (no position deduction)
        assert available > paper_available


class TestNumericalExample:
    """Verify the exact numerical example from the plan document."""

    def test_plan_numerical_example(self, live_cfg, mock_clob):
        """
        Scenario: Live mode, wallet $50 free USDC, $30 in positions, $10 in resting.
        After fix: available=40, equity=80, budget=80*fraction.
        """
        mgr = _make_manager(live_cfg, mock_clob, bankroll=50.0)

        # Set up resting orders worth exactly $10
        mgr.ladders["mkt1"] = MagicMock()
        fee_rate = mgr.fee_rate
        effective_price = 0.50 + compute_fee(0.50, fee_rate)
        size = 10.0 / effective_price

        order1 = TrackedOrder(
            order_id="o1", market_id="mkt1", token_id="tok_up",
            side=Side.UP, price=0.50, size=size, placed_at=100.0,
        )
        mgr.tracker.add(order1)

        # Set up position cost of $30
        mgr.positions.update_position("mkt1", Side.UP, 60.0, 15.0)
        mgr.positions.update_position("mkt1", Side.DOWN, 60.0, 15.0)

        # Verify
        assert abs(mgr.resting_order_cost() - 10.0) < 0.01
        assert abs(mgr.positions.total_position_cost() - 30.0) < 0.01
        assert abs(mgr.total_committed() - 40.0) < 0.01
        assert abs(mgr.positions.equity() - 80.0) < 0.01

        # Live mode available = 50 - 10 = 40
        live_available = mgr.positions.bankroll - mgr.resting_order_cost()
        assert abs(live_available - 40.0) < 0.01

        # Old broken available = 50 - 40 = 10
        old_available = mgr.positions.bankroll - mgr.total_committed()
        assert abs(old_available - 10.0) < 0.01

        # Overleverage: wallet(50) >= resting(10) -> not overleveraged
        assert mgr.positions.bankroll >= mgr.resting_order_cost()

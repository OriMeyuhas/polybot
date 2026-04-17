"""Tests for imbalanced position detection.

Covers:
- Loss cap: highly imbalanced positions (ratio >= 3:1) treated as one-sided
- Settlement logging: pair_cost only reported on balanced positions
- Force-buy: warning log when deficit is very large
- Tracker-level imbalance ratio check in loss cap
"""

import logging
import time

import pytest
from unittest.mock import MagicMock

from polybot.bot import Bot
from polybot.config import BotConfig
from polybot.order_executor import OrderExecutor
from polybot.strategy.ladder_manager import LadderManager, LadderState
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.types import MarketWindow, Side, Position


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def cfg():
    return BotConfig(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        ladder_rungs=8,
        ladder_spacing=0.02,
        ladder_width=0.10,
        ladder_size_skew=1.0,
        max_pair_cost=0.90,
        reprice_threshold=0.02,
        max_imbalance_ratio=0.60,
        imbalance_timeout_sec=120,
        imbalance_min_heavy_fills=3,
        boost_elapsed_pct=0.20,
        force_buy_elapsed_pct=0.70,
        force_buy_max_pair_cost=0.93,
    )


@pytest.fixture
def market():
    return MarketWindow(
        market_id="btc-15m-100",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1900,
    )


@pytest.fixture
def mock_clob():
    clob = MagicMock()
    book = MagicMock()
    book.asks = [
        MagicMock(price="0.42", size="100"),
        MagicMock(price="0.43", size="200"),
        MagicMock(price="0.44", size="300"),
    ]
    book.bids = [MagicMock(price="0.40", size="5000")]
    clob.get_order_book.return_value = book
    clob.create_order.return_value = {"signed": True}
    clob.post_order.return_value = {"orderID": "o1", "status": "resting"}
    clob.get_open_orders.return_value = []
    return clob


def _make_manager(cfg, mock_clob, bankroll=10000.0):
    executor = OrderExecutor(cfg, clob_client=mock_clob)
    tracker = OrderTracker()
    positions = PositionManager(cfg, bankroll=bankroll)
    risk = RiskManager(cfg, starting_bankroll=bankroll)
    return LadderManager(cfg, executor, tracker, positions, risk)


def _add_filled_orders(tracker, market_id, side, token_id, count, price=0.45, size=10.0):
    """Helper: add fully filled tracked orders."""
    for i in range(count):
        oid = f"fill-{side.value}-{market_id}-{i}"
        order = TrackedOrder(
            order_id=oid,
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            filled=size,
            status="filled",
            placed_at=1000.0,
            credited_to_pm=size,
        )
        tracker.add(order)


def _make_bot(cfg, mock_clob=None):
    bot = Bot(cfg)
    if mock_clob is not None:
        bot.clob_client = mock_clob
        bot.order_executor.client = mock_clob
    return bot


# ─── Task 1: Loss cap treats highly imbalanced positions as one-sided ───

class TestLossCapImbalanceRatio:
    def test_loss_cap_skips_balanced_two_sided_position_via_pm(self, cfg, mock_clob, market):
        """A 2:1 ratio position (e.g. 100 UP : 50 DN) should be skipped by loss cap."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        mid = market.market_id

        # Credit to position manager: 100 UP at $0.45, 50 DN at $0.45
        mgr.positions.update_position(mid, Side.UP, 100.0, 45.0)
        mgr.positions.update_position(mid, Side.DOWN, 50.0, 22.5)

        # No tracker fills needed (PM check happens first)
        result = mgr.check_loss_cap({"BTC": 100.0})
        assert mid not in result

    def test_loss_cap_catches_imbalanced_position_via_pm(self, cfg, mock_clob, market):
        """A 319:5 ratio position should NOT be skipped — treat as one-sided."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        mid = market.market_id

        # Credit to position manager: 319 UP at $0.45, 5 DN at $0.45
        # This gives ratio = 319/5 = 63.8 which is >> 3.0
        mgr.positions.update_position(mid, Side.UP, 319.0, 143.55)
        mgr.positions.update_position(mid, Side.DOWN, 5.0, 2.25)

        # Also add tracker fills for the one-sided check to calculate cost_basis
        # UP fills: total cost of $143.55 > max_loss $5 (5% of $100)
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 32, price=0.45, size=10.0)

        result = mgr.check_loss_cap({"BTC": 100.0})
        # Should be killed because 319:5 ratio > 3:1, treated as one-sided
        assert mid in result

    def test_loss_cap_skips_exactly_3_to_1_ratio_via_pm(self, cfg, mock_clob, market):
        """A position with exactly 3:1 ratio should NOT be skipped (ratio < 3.0 is the threshold)."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        mid = market.market_id

        # 30:10 = ratio 3.0, NOT < 3.0 so it should NOT be skipped
        mgr.positions.update_position(mid, Side.UP, 30.0, 13.5)
        mgr.positions.update_position(mid, Side.DOWN, 10.0, 4.5)

        # Add tracker fills too
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)

        result = mgr.check_loss_cap({"BTC": 100.0})
        # Ratio is exactly 3.0, not < 3.0, so it falls through to one-sided check
        # cost_basis from tracker: 3 * 10 * 0.45 = $13.50 > max_loss $5 => KILLED
        assert mid in result


# ─── Task 4: Tracker-level imbalance ratio check in loss cap ───

class TestLossCapTrackerImbalanceRatio:
    def test_tracker_balanced_two_sided_skipped(self, cfg, mock_clob, market):
        """Tracker-level check: balanced 2:1 fills should be skipped."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        mid = market.market_id

        # No PM position, but tracker has balanced fills
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 2, price=0.45, size=10.0)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 1, price=0.45, size=10.0)

        # UP: 20 qty, DN: 10 qty => ratio 2.0 < 3.0 => skip
        result = mgr.check_loss_cap({"BTC": 100.0})
        assert mid not in result

    def test_tracker_imbalanced_not_skipped(self, cfg, mock_clob, market):
        """Tracker-level check: highly imbalanced fills should NOT be skipped."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        mid = market.market_id

        # Tracker has imbalanced fills: 40 UP, 5 DN => ratio 8.0 >> 3.0
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 4, price=0.45, size=10.0)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 1, price=0.45, size=5.0)

        # UP cost = 4*10*0.45 = $18, DN cost = 1*5*0.45 = $2.25
        # total cost_basis = $20.25 > max_loss $5 => KILLED
        result = mgr.check_loss_cap({"BTC": 100.0})
        assert mid in result


# ─── Task 2: Settlement pair_cost only reported on balanced positions ───

class TestSettlementImbalancedPairCost:
    def test_settle_balanced_reports_pair_cost(self, cfg, mock_clob, market):
        """A balanced position (100:100) should report pair_cost."""
        bot = _make_bot(cfg, mock_clob)
        bot.redeemer = MagicMock()
        mid = market.market_id
        bot.position_manager.update_position(mid, Side.UP, qty=100.0, cost=43.0)
        bot.position_manager.update_position(mid, Side.DOWN, qty=100.0, cost=48.0)
        bot._expired_market_cache[mid] = market
        bot.position_manager.mark_pending_settlement(mid)

        bot._settle_position(mid, market, "UP")

        assert len(bot._settled_pair_costs) == 1
        assert bot._activity_log[0].meta["pair_cost"] is not None

    def test_settle_imbalanced_does_not_report_pair_cost(self, cfg, mock_clob, market):
        """A 319:5 position should NOT report pair_cost (ratio > 3:1)."""
        bot = _make_bot(cfg, mock_clob)
        bot.redeemer = MagicMock()
        mid = market.market_id
        bot.position_manager.update_position(mid, Side.UP, qty=319.0, cost=143.55)
        bot.position_manager.update_position(mid, Side.DOWN, qty=5.0, cost=2.25)
        bot._expired_market_cache[mid] = market
        bot.position_manager.mark_pending_settlement(mid)

        bot._settle_position(mid, market, "UP")

        # pair_cost should NOT be appended because ratio 63.8 > 3.0
        assert len(bot._settled_pair_costs) == 0
        assert bot._activity_log[0].meta["pair_cost"] is None

    def test_settle_moderately_imbalanced_does_not_report_pair_cost(self, cfg, mock_clob, market):
        """A 3:1 ratio (exactly) should NOT report pair_cost."""
        bot = _make_bot(cfg, mock_clob)
        bot.redeemer = MagicMock()
        mid = market.market_id
        bot.position_manager.update_position(mid, Side.UP, qty=30.0, cost=13.5)
        bot.position_manager.update_position(mid, Side.DOWN, qty=10.0, cost=4.5)
        bot._expired_market_cache[mid] = market
        bot.position_manager.mark_pending_settlement(mid)

        bot._settle_position(mid, market, "UP")

        # ratio 3.0, not < 3.0, so pair_cost should be None
        assert len(bot._settled_pair_costs) == 0
        assert bot._activity_log[0].meta["pair_cost"] is None

    def test_settle_just_under_3_to_1_reports_pair_cost(self, cfg, mock_clob, market):
        """A 2.9:1 ratio should report pair_cost."""
        bot = _make_bot(cfg, mock_clob)
        bot.redeemer = MagicMock()
        mid = market.market_id
        bot.position_manager.update_position(mid, Side.UP, qty=29.0, cost=13.05)
        bot.position_manager.update_position(mid, Side.DOWN, qty=10.0, cost=4.5)
        bot._expired_market_cache[mid] = market
        bot.position_manager.mark_pending_settlement(mid)

        bot._settle_position(mid, market, "UP")

        # ratio 2.9 < 3.0 => pair_cost should be reported
        assert len(bot._settled_pair_costs) == 1
        assert bot._activity_log[0].meta["pair_cost"] is not None


# ─── Task 3: Force-buy warning on large deficit ───

class TestForceBuyLargeDeficit:
    def test_force_buy_large_deficit_warning(self, cfg, mock_clob, market, caplog):
        """Force-buy with deficit > 50 should log a warning."""
        mgr = _make_manager(cfg, mock_clob, bankroll=10000.0)
        mgr.post_ladder(market)
        mid = market.market_id

        # Create a heavily one-sided position: 200 UP, 0 DN
        mgr.positions.update_position(mid, Side.UP, 200.0, 90.0)

        # Add tracker fills for the min_heavy check
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 5, price=0.45, size=40.0)

        # Force the book to give a cheap ask for light side
        book = MagicMock()
        book.asks = [MagicMock(price="0.42", size="500")]
        book.bids = [MagicMock(price="0.40", size="5000")]
        mock_clob.get_order_book.return_value = book

        with caplog.at_level(logging.WARNING):
            result = mgr.try_complete_pair(market, now=1700.0)

        if result is not None:
            # Deficit is 200 - 0 = 200 > 50, should have logged warning
            assert any("FORCE-BUY LARGE" in r.message and "deficit" in r.message
                       for r in caplog.records)

    def test_force_buy_small_deficit_no_warning(self, cfg, mock_clob, market, caplog):
        """Force-buy with deficit <= 50 should NOT log a warning."""
        mgr = _make_manager(cfg, mock_clob, bankroll=10000.0)
        mgr.post_ladder(market)
        mid = market.market_id

        # Create a position: 30 UP, 5 DN => deficit=25 < 50
        mgr.positions.update_position(mid, Side.UP, 30.0, 13.5)
        mgr.positions.update_position(mid, Side.DOWN, 5.0, 2.25)

        # Add tracker fills for the min_heavy check
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 5, price=0.45, size=6.0)

        with caplog.at_level(logging.WARNING):
            result = mgr.try_complete_pair(market, now=1700.0)

        # No FORCE-BUY LARGE warning should appear
        assert not any("FORCE-BUY LARGE" in r.message for r in caplog.records)

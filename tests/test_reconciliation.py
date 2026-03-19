"""Integration tests for failure scenarios and reconciliation.

Covers:
- Phantom fill prevention (API errors must not create phantom positions)
- Tick size cache invalidation on rejection
- Heartbeat loss -> state reset -> recovery flow
- Failed cancel reconciliation
"""

from unittest.mock import MagicMock

from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.ladder_manager import LadderManager
from polybot.order_executor import OrderExecutor
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.tick_size_cache import TickSizeCache
from polybot.types import Side


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> BotConfig:
    defaults = dict(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _make_ladder_manager(cfg=None, mock_clob=None, bankroll=10_000.0):
    cfg = cfg or _cfg()
    mock_clob = mock_clob or MagicMock()
    executor = OrderExecutor(cfg, clob_client=mock_clob)
    tracker = OrderTracker()
    positions = PositionManager(cfg, bankroll=bankroll)
    risk = RiskManager(cfg, starting_bankroll=bankroll)
    return LadderManager(cfg, executor, tracker, positions, risk)


# ---------------------------------------------------------------------------
# TestPhantomFillPrevention
# ---------------------------------------------------------------------------

class TestPhantomFillPrevention:
    """Verify that API failures don't cause phantom fills."""

    def test_api_failure_skips_fill_check(self):
        """When get_open_orders() raises ClobApiError, check_fills returns 0
        and no positions are created."""
        mock_clob = MagicMock()
        # get_open_orders raises a generic Exception; OrderExecutor wraps it
        # into ClobApiError, and LadderManager.check_fills catches ClobApiError.
        mock_clob.get_open_orders.side_effect = Exception("connection timeout")

        mgr = _make_ladder_manager(mock_clob=mock_clob)

        # Seed a resting order so there's something that *could* be mis-detected
        mgr.tracker.add(TrackedOrder(
            order_id="o1",
            market_id="m1",
            token_id="tok_up",
            side=Side.UP,
            price=0.45,
            size=10.0,
            placed_at=1000.0,
        ))

        result = mgr.check_fills()

        assert result == 0, "check_fills must return 0 on API failure"
        assert mgr.positions.active_position_count() == 0, (
            "No positions should be created when the API call fails"
        )
        # The order should still be resting (not incorrectly filled)
        assert mgr.tracker.orders["o1"].status == "resting"


# ---------------------------------------------------------------------------
# TestTickSizeRetry
# ---------------------------------------------------------------------------

class TestTickSizeRetry:
    """Verify tick size cache invalidation triggers a refetch."""

    def test_tick_size_rejection_invalidates_cache(self):
        """After invalidation the cache refetches the updated tick size."""
        mock_client = MagicMock()
        # First call returns 0.01, subsequent calls return 0.001
        mock_client.get_tick_size.side_effect = [0.01, 0.001]

        cache = TickSizeCache(mock_client, ttl_sec=300)

        first = cache.get_tick_size("cond_abc")
        assert first == 0.01

        # Simulate a tick-size rejection: invalidate the cached value
        cache.invalidate("cond_abc")

        second = cache.get_tick_size("cond_abc")
        assert second == 0.001, "After invalidation the refetched value should be 0.001"
        assert mock_client.get_tick_size.call_count == 2


# ---------------------------------------------------------------------------
# TestHeartbeatRecoveryFlow
# ---------------------------------------------------------------------------

class TestHeartbeatRecoveryFlow:
    """Verify heartbeat loss -> state reset -> recovery."""

    def test_connection_lost_wipes_state(self):
        """After mark_all_unknown + reconcile with empty exchange, the order
        is detected as filled."""
        tracker = OrderTracker()

        tracker.add(TrackedOrder(
            order_id="o1",
            market_id="m1",
            token_id="tok_up",
            side=Side.UP,
            price=0.50,
            size=10.0,
            placed_at=1000.0,
        ))

        # Simulate heartbeat loss: mark everything unknown
        tracker.mark_all_unknown()
        assert tracker.orders["o1"].status == "unknown"

        # Reconnect and reconcile with empty open-orders (order gone from exchange)
        result = tracker.reconcile(open_orders=[])

        filled_ids = [o.order_id for o in result["filled"]]
        assert "o1" in filled_ids, "Order should appear in filled list"
        assert tracker.orders["o1"].status == "filled"
        assert tracker.orders["o1"].filled == 10.0


# ---------------------------------------------------------------------------
# TestCancelReconciliation
# ---------------------------------------------------------------------------

class TestCancelReconciliation:
    """Verify failed cancels are reconciled."""

    def test_cancelled_order_still_on_exchange_reverted(self):
        """If we marked an order cancelled but the exchange still has it,
        reconcile should revert the order to 'resting'."""
        tracker = OrderTracker()

        tracker.add(TrackedOrder(
            order_id="o1",
            market_id="m1",
            token_id="tok_up",
            side=Side.UP,
            price=0.50,
            size=10.0,
            placed_at=1000.0,
        ))

        # Cancel locally (e.g. cancel_order API call failed silently)
        tracker.cancel("o1")
        assert tracker.orders["o1"].status == "cancelled"

        # Reconcile: exchange still shows this order as open
        result = tracker.reconcile(open_orders=[{"id": "o1"}])

        assert "o1" in result["reverted"], "Order should appear in reverted list"
        assert tracker.orders["o1"].status == "resting", (
            "Order status should be reverted to 'resting'"
        )

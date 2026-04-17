"""Integration tests for failure scenarios and reconciliation.

Covers:
- Phantom fill prevention (API errors must not create phantom positions)
- Tick size cache invalidation on rejection
- Heartbeat loss -> state reset -> recovery flow
- Failed cancel reconciliation
"""

from unittest.mock import MagicMock

import pytest

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

        assert len(result) == 0, "check_fills must return empty list on API failure"
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
        """After mark_all_unknown + reconcile with empty exchange, unknown
        orders should NOT be phantom-filled — they stay 'unknown' pending
        explicit resolution."""
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

        # Unknown orders that disappeared should NOT be filled
        assert len(result["filled"]) == 0, "Unknown orders must not be phantom-filled"
        assert tracker.orders["o1"].status == "unknown", (
            "Order should remain 'unknown' pending explicit resolution"
        )


# ---------------------------------------------------------------------------
# TestCancelReconciliation
# ---------------------------------------------------------------------------

class TestPartialFillReconciliation:
    """Verify partial fill reconciliation round-trip."""

    def test_partial_fill_via_size_matched(self):
        """Order on exchange with size_matched=4.0 appears in 'partial' key."""
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
        ))
        result = tracker.reconcile([{"id": "o1", "size_matched": "4.0"}])
        assert len(result["partial"]) == 1
        assert result["partial"][0].order_id == "o1"
        assert result["partial"][0].filled == 4.0
        assert len(result["filled"]) == 0

    def test_partial_fill_then_disappears(self):
        """After partial fill, order disappears — remaining fills are credited."""
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
        ))
        # First: partial fill
        result1 = tracker.reconcile([{"id": "o1", "size_matched": "4.0"}])
        assert len(result1["partial"]) == 1
        assert tracker.orders["o1"].filled == 4.0
        # Second: order disappears
        result2 = tracker.reconcile([])
        assert len(result2["filled"]) == 1
        assert tracker.orders["o1"].filled == 10.0
        assert tracker.orders["o1"].status == "filled"

    def test_cancel_after_partial_flushes_to_pm(self):
        """End-to-end: partial fill -> cancel_ladder -> position manager has correct qty."""
        mgr = _make_ladder_manager()
        from polybot.ladder_manager import LadderState
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.45, size=10.0, placed_at=1000.0,
        ))
        mgr.ladders["m1"] = LadderState(
            market_id="m1", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
        )
        # Simulate partial fill via update_fill (as reconcile would do)
        mgr.tracker.update_fill("o1", 4.0)
        # Cancel ladder — should flush uncredited fills to PM first
        mgr.cancel_ladder("m1")
        pos = mgr.positions.positions.get("m1")
        assert pos is not None
        assert pos.up_qty == 4.0
        from polybot.fees import compute_fee
        expected_cost = 4.0 * (0.45 + compute_fee(0.45, mgr.fee_rate))
        assert pos.up_cost == pytest.approx(expected_cost)


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


# ---------------------------------------------------------------------------
# TestPhantomFillPreventionReconcile — Phase 1 & 2 tests (6.2–6.5)
# ---------------------------------------------------------------------------

class TestPhantomFillPreventionReconcile:
    """Verify phantom fill prevention after connection loss."""

    def test_unknown_order_not_phantom_filled(self):
        """6.2: mark order as unknown, reconcile with empty exchange,
        verify order stays 'unknown' and no fills are reported."""
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
        ))
        tracker.mark_all_unknown()
        result = tracker.reconcile(open_orders=[])
        assert len(result["filled"]) == 0
        assert tracker.orders["o1"].status == "unknown"

    def test_unknown_order_still_on_exchange_reverts(self):
        """6.3: mark order as unknown, reconcile with exchange showing it,
        verify status reverts to 'resting'."""
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
        ))
        tracker.mark_all_unknown()
        result = tracker.reconcile(open_orders=[{"id": "o1"}])
        assert "o1" in result["reverted"]
        assert tracker.orders["o1"].status == "resting"

    def test_cancel_market_includes_unknown(self):
        """6.4: add order, set status to 'unknown', call cancel_market(),
        verify it gets cancelled."""
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
        ))
        tracker.orders["o1"].status = "unknown"
        cancelled = tracker.cancel_market("m1")
        assert "o1" in cancelled
        assert tracker.orders["o1"].status == "cancelling"
        tracker.confirm_cancels(cancelled)
        assert tracker.orders["o1"].status == "cancelled"

    def test_cancel_side_includes_unknown(self):
        """6.5: same as 6.4 but for cancel_side()."""
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
        ))
        tracker.orders["o1"].status = "unknown"
        cancelled = tracker.cancel_side("m1", Side.UP)
        assert "o1" in cancelled
        assert tracker.orders["o1"].status == "cancelling"
        tracker.confirm_cancels(cancelled)
        assert tracker.orders["o1"].status == "cancelled"


# ---------------------------------------------------------------------------
# TestResolveUnknowns — Phase 3 tests (6.6–6.7)
# ---------------------------------------------------------------------------

class TestResolveUnknowns:
    """Verify resolve_unknowns() method on OrderTracker."""

    def test_resolve_unknowns_live(self):
        """6.6: create 3 unknown orders, resolve with LIVE/MATCHED/CANCELLED."""
        tracker = OrderTracker()
        for oid in ("o1", "o2", "o3"):
            tracker.add(TrackedOrder(
                order_id=oid, market_id="m1", token_id="tok_up",
                side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
            ))
        tracker.mark_all_unknown()

        result = tracker.resolve_unknowns({
            "o1": "LIVE",
            "o2": "MATCHED",
            "o3": "CANCELLED",
        })

        # o1 reverted to resting
        assert "o1" in result["reverted"]
        assert tracker.orders["o1"].status == "resting"
        # o2 filled with full qty
        assert "o2" in result["filled"]
        assert tracker.orders["o2"].status == "filled"
        assert tracker.orders["o2"].filled == 10.0
        # o3 cancelled
        assert "o3" in result["cancelled"]
        assert tracker.orders["o3"].status == "cancelled"

    def test_resolve_unknowns_not_found(self):
        """6.7: unknown order resolved with unrecognized status defaults to cancelled."""
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
        ))
        tracker.orders["o1"].status = "unknown"

        result = tracker.resolve_unknowns({"o1": "SOMETHING_WEIRD"})
        assert "o1" in result["cancelled"]
        assert tracker.orders["o1"].status == "cancelled"

    def test_get_unknown_ids(self):
        """get_unknown_ids() returns only unknown order IDs."""
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok_up",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000.0,
        ))
        tracker.add(TrackedOrder(
            order_id="o2", market_id="m1", token_id="tok_dn",
            side=Side.DOWN, price=0.50, size=10.0, placed_at=1000.0,
        ))
        tracker.orders["o1"].status = "unknown"
        assert tracker.get_unknown_ids() == ["o1"]


# ---------------------------------------------------------------------------
# TestPaperClientGetOrder — Phase 4 test (6.8)
# ---------------------------------------------------------------------------

class TestPaperClientGetOrder:
    """Verify PaperClobClient.get_order() method."""

    def test_paper_client_get_order(self):
        """6.8: get_order returns LIVE for resting, CANCELLED for missing."""
        from polybot.oms.clob_client import PaperClobClient
        client = PaperClobClient()

        # Place an order
        resp = client.post_order({"token_id": "tok1", "price": "0.50", "size": "10", "side": "BUY"})
        oid = resp["orderID"]

        # Resting order → LIVE
        info = client.get_order(oid)
        assert info["status"] == "LIVE"
        assert info["orderID"] == oid

        # Cancel it, then query → CANCELLED
        client.cancel(oid)
        info2 = client.get_order(oid)
        assert info2["status"] == "CANCELLED"

        # Nonexistent order → CANCELLED
        info3 = client.get_order("nonexistent-id")
        assert info3["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# TestFullConnectionLossRecoveryFlow — Phase 5 test (6.9)
# ---------------------------------------------------------------------------

class TestFullConnectionLossRecoveryFlow:
    """End-to-end connection loss recovery: no phantom fills."""

    def test_full_connection_loss_recovery_flow(self):
        """6.9: mark_all_unknown -> cancel_all on exchange -> resolve_unknowns
        with all CANCELLED -> verify zero phantom fills."""
        from polybot.oms.clob_client import PaperClobClient

        tracker = OrderTracker()
        client = PaperClobClient()

        # Place orders on both tracker and exchange
        for i, side in enumerate([Side.UP, Side.DOWN], start=1):
            oid = f"o{i}"
            tracker.add(TrackedOrder(
                order_id=oid, market_id="m1", token_id=f"tok_{side.value}",
                side=side, price=0.45, size=10.0, placed_at=1000.0,
            ))
            client._resting[oid] = {"orderID": oid, "status": "resting"}

        # Step 1: Connection lost — mark unknown
        tracker.mark_all_unknown()
        assert tracker.orders["o1"].status == "unknown"
        assert tracker.orders["o2"].status == "unknown"

        # Step 2: Cancel all on exchange
        client.cancel_all()
        assert len(client._resting) == 0

        # Step 3: Reconcile (should NOT phantom-fill)
        result = tracker.reconcile(open_orders=[])
        assert len(result["filled"]) == 0, "No phantom fills during reconcile"

        # Step 4: Resolve unknowns via exchange API
        unknown_ids = tracker.get_unknown_ids()
        statuses = {}
        for oid in unknown_ids:
            resp = client.get_order(oid)
            statuses[oid] = resp.get("status", "CANCELLED")
        resolve_result = tracker.resolve_unknowns(statuses)

        # All should be cancelled, none filled
        assert len(resolve_result["filled"]) == 0
        assert len(resolve_result["cancelled"]) == 2
        assert tracker.orders["o1"].status == "cancelled"
        assert tracker.orders["o2"].status == "cancelled"

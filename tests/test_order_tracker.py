import pytest
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.types import Side


@pytest.fixture
def tracker():
    return OrderTracker()


def _make_order(order_id, market_id="m1", side=Side.UP, price=0.50, size=10.0):
    return TrackedOrder(
        order_id=order_id, market_id=market_id,
        token_id=f"tok_{side.value.lower()}", side=side,
        price=price, size=size, placed_at=1000.0,
    )


class TestAddAndRetrieve:
    def test_add_order(self, tracker):
        order = _make_order("o1")
        tracker.add(order)
        assert "o1" in tracker.orders
        assert tracker.has_orders("m1")

    def test_get_resting(self, tracker):
        tracker.add(_make_order("o1"))
        tracker.add(_make_order("o2"))
        resting = tracker.get_resting("m1")
        assert len(resting) == 2

    def test_get_resting_side(self, tracker):
        tracker.add(_make_order("o1", side=Side.UP))
        tracker.add(_make_order("o2", side=Side.DOWN))
        up = tracker.get_resting_side("m1", Side.UP)
        dn = tracker.get_resting_side("m1", Side.DOWN)
        assert len(up) == 1
        assert len(dn) == 1


class TestFills:
    def test_full_fill(self, tracker):
        tracker.add(_make_order("o1", size=10.0))
        tracker.update_fill("o1", 10.0)
        assert tracker.orders["o1"].status == "filled"
        assert tracker.orders["o1"].filled == 10.0

    def test_partial_fill(self, tracker):
        tracker.add(_make_order("o1", size=10.0))
        tracker.update_fill("o1", 4.0)
        assert tracker.orders["o1"].status == "partial"
        assert tracker.orders["o1"].filled == 4.0

    def test_multiple_partial_fills(self, tracker):
        tracker.add(_make_order("o1", size=10.0))
        tracker.update_fill("o1", 3.0)
        tracker.update_fill("o1", 7.0)
        assert tracker.orders["o1"].status == "filled"
        assert tracker.orders["o1"].filled == 10.0

    def test_filled_qty_and_cost(self, tracker):
        tracker.add(_make_order("o1", side=Side.UP, price=0.45, size=10.0))
        tracker.add(_make_order("o2", side=Side.UP, price=0.50, size=20.0))
        tracker.update_fill("o1", 10.0)
        tracker.update_fill("o2", 20.0)

        assert tracker.filled_qty("m1", Side.UP) == pytest.approx(30.0)
        assert tracker.filled_cost("m1", Side.UP) == pytest.approx(10 * 0.45 + 20 * 0.50)

    def test_filled_excludes_unfilled(self, tracker):
        tracker.add(_make_order("o1", side=Side.UP, size=10.0))
        tracker.add(_make_order("o2", side=Side.DOWN, size=10.0))
        tracker.update_fill("o1", 10.0)
        # o2 not filled
        assert tracker.filled_qty("m1", Side.UP) == 10.0
        assert tracker.filled_qty("m1", Side.DOWN) == 0.0


class TestCancel:
    def test_cancel_single(self, tracker):
        tracker.add(_make_order("o1"))
        tracker.cancel("o1")
        assert tracker.orders["o1"].status == "cancelled"
        assert not tracker.has_orders("m1")

    def test_cancel_market(self, tracker):
        tracker.add(_make_order("o1"))
        tracker.add(_make_order("o2"))
        cancelled = tracker.cancel_market("m1")
        assert len(cancelled) == 2
        assert not tracker.has_orders("m1")

    def test_cancel_side(self, tracker):
        tracker.add(_make_order("o1", side=Side.UP))
        tracker.add(_make_order("o2", side=Side.DOWN))
        cancelled = tracker.cancel_side("m1", Side.UP)
        assert len(cancelled) == 1
        # DOWN still resting
        assert len(tracker.get_resting_side("m1", Side.DOWN)) == 1

    def test_cancel_doesnt_affect_filled(self, tracker):
        tracker.add(_make_order("o1"))
        tracker.update_fill("o1", 10.0)
        cancelled = tracker.cancel_market("m1")
        assert len(cancelled) == 0  # already filled, not cancelled


class TestMarkAllUnknown:
    def test_filled_stays_filled(self, tracker):
        tracker.add(_make_order("o1", size=10.0))
        tracker.update_fill("o1", 10.0)
        tracker.mark_all_unknown()
        assert tracker.orders["o1"].status == "filled"

    def test_resting_becomes_unknown(self, tracker):
        tracker.add(_make_order("o1"))
        tracker.mark_all_unknown()
        assert tracker.orders["o1"].status == "unknown"

    def test_cancelled_becomes_unknown(self, tracker):
        tracker.add(_make_order("o1"))
        tracker.cancel("o1")
        tracker.mark_all_unknown()
        assert tracker.orders["o1"].status == "unknown"


class TestReconcile:
    def test_filled_detection(self, tracker):
        """Order not on exchange and resting -> filled."""
        tracker.add(_make_order("o1", size=10.0))
        result = tracker.reconcile([])  # no open orders on exchange
        assert len(result["filled"]) == 1
        assert tracker.orders["o1"].status == "filled"
        assert tracker.orders["o1"].filled == 10.0

    def test_reverted_detection(self, tracker):
        """Order on exchange but cancelled locally -> reverted to resting."""
        tracker.add(_make_order("o1"))
        tracker.cancel("o1")
        result = tracker.reconcile([{"id": "o1"}])
        assert "o1" in result["reverted"]
        assert tracker.orders["o1"].status == "resting"

    def test_orphan_detection(self, tracker):
        """Order on exchange but not in our tracker -> orphaned."""
        tracker.add(_make_order("o1"))
        result = tracker.reconcile([{"id": "o1"}, {"id": "o_orphan"}])
        assert "o_orphan" in result["orphaned"]

    def test_unknown_order_not_on_exchange_filled(self, tracker):
        """Unknown order not on exchange -> filled."""
        tracker.add(_make_order("o1", size=10.0))
        tracker.mark_all_unknown()
        result = tracker.reconcile([])
        assert len(result["filled"]) == 1
        assert tracker.orders["o1"].status == "filled"

    def test_unknown_order_on_exchange_reverted(self, tracker):
        """Unknown order still on exchange -> reverted to resting."""
        tracker.add(_make_order("o1"))
        tracker.mark_all_unknown()
        result = tracker.reconcile([{"id": "o1"}])
        assert "o1" in result["reverted"]
        assert tracker.orders["o1"].status == "resting"


class TestFillThreshold:
    def test_relative_threshold_large_order(self, tracker):
        """For a 1000-size order, filling 999.5 should count as filled (>= 999.0)."""
        tracker.add(_make_order("o1", size=1000.0))
        tracker.update_fill("o1", 999.5)
        assert tracker.orders["o1"].status == "filled"
        assert tracker.orders["o1"].filled == 1000.0

    def test_relative_threshold_small_order(self, tracker):
        """For a 5-size order, filling 4.996 should count as filled (>= 4.995)."""
        tracker.add(_make_order("o1", size=5.0))
        tracker.update_fill("o1", 4.996)
        assert tracker.orders["o1"].status == "filled"
        assert tracker.orders["o1"].filled == 5.0

    def test_below_threshold_not_filled(self, tracker):
        """For a 1000-size order, filling 998 should NOT count as filled (< 999.0)."""
        tracker.add(_make_order("o1", size=1000.0))
        tracker.update_fill("o1", 998.0)
        assert tracker.orders["o1"].status == "partial"


class TestCleanup:
    def test_cleanup_removes_all(self, tracker):
        tracker.add(_make_order("o1"))
        tracker.add(_make_order("o2"))
        tracker.cleanup_market("m1")
        assert "o1" not in tracker.orders
        assert "o2" not in tracker.orders
        assert "m1" not in tracker._by_market


class TestFilledCount:
    def test_filled_count_only_fully_filled(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0))
        tracker.add(TrackedOrder(order_id="o2", market_id="m1", token_id="t", side=Side.UP, price=0.46, size=10.0))
        tracker.update_fill("o1", 10.0)  # fully filled
        tracker.update_fill("o2", 3.0)   # partial
        assert tracker.filled_count("m1", Side.UP) == 1  # only o1

    def test_filled_count_excludes_other_side(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0))
        tracker.add(TrackedOrder(order_id="o2", market_id="m1", token_id="t", side=Side.DOWN, price=0.48, size=10.0))
        tracker.update_fill("o1", 10.0)
        tracker.update_fill("o2", 10.0)
        assert tracker.filled_count("m1", Side.UP) == 1
        assert tracker.filled_count("m1", Side.DOWN) == 1


class TestTotalCount:
    def test_total_count_excludes_cancelled(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0))
        tracker.add(TrackedOrder(order_id="o2", market_id="m1", token_id="t", side=Side.UP, price=0.46, size=10.0))
        tracker.add(TrackedOrder(order_id="o3", market_id="m1", token_id="t", side=Side.UP, price=0.47, size=10.0))
        tracker.cancel("o3")
        assert tracker.total_count("m1", Side.UP) == 2

    def test_total_count_includes_filled_and_resting(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0))
        tracker.add(TrackedOrder(order_id="o2", market_id="m1", token_id="t", side=Side.UP, price=0.46, size=10.0))
        tracker.update_fill("o1", 10.0)  # filled
        assert tracker.total_count("m1", Side.UP) == 2

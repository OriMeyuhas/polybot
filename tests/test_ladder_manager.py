import pytest
from unittest.mock import MagicMock
from polybot.ladder_manager import LadderManager, build_ladder_rungs
from polybot.order_tracker import OrderTracker
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.config import BotConfig
from polybot.order_executor import OrderExecutor
from polybot.types import MarketWindow, Side, Position


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
        ladder_size_skew=2.0,
        reprice_threshold=0.02,
        max_imbalance_ratio=0.60,
        imbalance_timeout_sec=30,
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


def _make_manager(cfg, mock_clob, bankroll=10000.0):
    executor = OrderExecutor(cfg, clob_client=mock_clob)
    tracker = OrderTracker()
    positions = PositionManager(cfg, bankroll=bankroll)
    risk = RiskManager(cfg, starting_bankroll=bankroll)
    return LadderManager(cfg, executor, tracker, positions, risk)


class TestBuildLadderRungs:
    def test_correct_number_of_rungs(self):
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=500.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
            tick_size=0.01, max_rung_price=1.0,
        )
        assert len(rungs) == 8

    def test_prices_ascending(self):
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=500.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
            tick_size=0.01,
        )
        prices = [p for p, s in rungs]
        assert prices == sorted(prices)

    def test_sizes_ascending_with_skew(self):
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=500.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
            tick_size=0.01,
        )
        sizes = [s for p, s in rungs]
        # Most expensive rung should be larger than cheapest
        assert sizes[-1] > sizes[0]

    def test_skew_ratio(self):
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=500.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=3.0,
            tick_size=0.01,
        )
        sizes = [s for p, s in rungs]
        ratio = sizes[-1] / sizes[0]
        # Should be close to the skew ratio (not exact due to price differences)
        assert ratio > 2.0

    def test_total_cost_matches_budget(self):
        budget = 500.0
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=budget, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
            tick_size=0.01,
        )
        total_cost = sum(p * s for p, s in rungs)
        assert total_cost == pytest.approx(budget, rel=0.05)

    def test_anchor_clamped_to_min(self):
        rungs = build_ladder_rungs(
            best_ask=0.05, budget=500.0, rungs=4,
            spacing=0.01, width=0.10, size_skew=1.0,
            tick_size=0.01,
        )
        prices = [p for p, s in rungs]
        assert all(p >= 0.01 for p in prices)

    def test_small_budget_fewer_rungs(self):
        """With min size 5.0, small budgets produce fewer rungs."""
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=10.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
            tick_size=0.01,
        )
        # Small budget can't fill all 8 rungs at min size 5.0
        assert len(rungs) < 8

    def test_tick_size_applied(self):
        rungs = build_ladder_rungs(
            best_ask=0.455, budget=500.0, rungs=4,
            spacing=0.025, width=0.05, size_skew=1.0,
            tick_size=0.01,
        )
        for price, _size in rungs:
            # All prices should be multiples of tick_size
            remainder = round(price / 0.01) * 0.01
            assert price == pytest.approx(remainder, abs=1e-9)


class TestPostLadder:
    def test_posts_orders_both_sides(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        count = mgr.post_ladder(market)
        assert count > 0
        assert mgr.has_ladder(market.market_id)

    def test_no_ladder_when_halted(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        mgr.risk.update_pnl(-1000.0)  # trigger halt (>5% of 10000 bankroll)
        count = mgr.post_ladder(market)
        assert count == 0

    def test_pair_cost_guard(self, cfg, market, mock_clob):
        # Set asks very high so combined VWAP > max_pair_cost
        mock_clob.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.54", size="5000")],
            asks=[MagicMock(price="0.56", size="5000")],
        )
        cfg_strict = BotConfig(
            private_key="0xfake", api_key="key",
            api_secret="secret", api_passphrase="pass",
            ladder_rungs=4, ladder_spacing=0.01,
            ladder_width=0.02, max_pair_cost=0.90,
            ladder_rungs_5m=4, ladder_spacing_5m=0.01,
            ladder_width_5m=0.02, max_pair_cost_5m=0.90,
        )
        mgr = _make_manager(cfg_strict, mock_clob)
        count = mgr.post_ladder(market)
        assert count == 0  # rejected by pair cost guard


class TestCheckFills:
    def test_fill_detected_when_order_disappears(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        # Manually add a tracked order
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        # get_open_orders returns empty -> o1 filled
        mock_clob.get_open_orders.return_value = []
        fills = mgr.check_fills()
        assert len(fills) == 1
        assert mgr.positions.positions[market.market_id].up_qty == 10.0

    def test_no_fill_when_order_still_open(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        mock_clob.get_open_orders.return_value = [{"id": "o1"}]
        fills = mgr.check_fills()
        assert len(fills) == 0

    def test_clob_error_returns_zero(self, cfg, market, mock_clob):
        from polybot.errors import ClobApiError
        mgr = _make_manager(cfg, mock_clob)
        mock_clob.get_open_orders.side_effect = Exception("timeout")
        fills = mgr.check_fills()
        assert len(fills) == 0


class TestImbalance:
    def test_no_action_when_balanced(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.ladder_manager import LadderState
        mgr.ladders[market.market_id] = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
        )
        # Add balanced fills
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        mgr.tracker.add(TrackedOrder(
            order_id="o2", market_id=market.market_id,
            token_id="tok_dn", side=Side.DOWN,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        mgr.tracker.update_fill("o1", 10.0)
        mgr.tracker.update_fill("o2", 8.0)
        acted = mgr.check_imbalance(now_epoch=2000)
        assert len(acted) == 0

    def test_cancel_heavy_side_on_severe_imbalance(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.ladder_manager import LadderState
        mgr.ladders[market.market_id] = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
        )
        from polybot.order_tracker import TrackedOrder
        # 3 UP fills (>= imbalance_min_heavy_fills=3), 0 DN fills -> fully one-sided
        for i in range(3):
            mgr.tracker.add(TrackedOrder(
                order_id=f"up_{i}", market_id=market.market_id,
                token_id="tok_up", side=Side.UP,
                price=0.45, size=7.0, placed_at=1000.0,
            ))
            mgr.tracker.update_fill(f"up_{i}", 7.0)
        # Add a resting UP order that should get cancelled
        mgr.tracker.add(TrackedOrder(
            order_id="o3", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.50, size=10.0, placed_at=1000.0,
        ))
        acted = mgr.check_imbalance(now_epoch=2000)
        assert market.market_id in acted
        # o3 should be cancelling (transient) or cancelled
        assert mgr.tracker.orders["o3"].status in ("cancelling", "cancelled")

    def test_imbalance_timeout_sets_accepted(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.ladder_manager import LadderState
        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
            imbalance_alert_at=1000,
            heavy_side_locked="UP",
            timeframe_sec=300,  # 5m window
        )
        mgr.ladders[market.market_id] = state
        from polybot.order_tracker import TrackedOrder
        # 3 UP fills, 0 DN fills -> fully one-sided severe imbalance
        for i in range(3):
            mgr.tracker.add(TrackedOrder(
                order_id=f"up_{i}", market_id=market.market_id,
                token_id="tok_up", side=Side.UP,
                price=0.45, size=7.0, placed_at=1000.0,
            ))
            mgr.tracker.update_fill(f"up_{i}", 7.0)
        # Dynamic timeout for 5m = max(30, 300*0.30) = max(30, 90) = 90s
        # Call with time past dynamic timeout (90s)
        acted = mgr.check_imbalance(now_epoch=1000 + 91)
        assert market.market_id in acted
        assert state.imbalance_accepted is True
        assert state.imbalance_alert_at is None

    def test_imbalance_skipped_when_accepted(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.ladder_manager import LadderState
        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
            imbalance_accepted=True,
        )
        mgr.ladders[market.market_id] = state
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=20.0, placed_at=1000.0,
        ))
        mgr.tracker.add(TrackedOrder(
            order_id="o2", market_id=market.market_id,
            token_id="tok_dn", side=Side.DOWN,
            price=0.45, size=5.0, placed_at=1000.0,
        ))
        mgr.tracker.update_fill("o1", 20.0)
        mgr.tracker.update_fill("o2", 5.0)
        acted = mgr.check_imbalance(now_epoch=2000)
        assert len(acted) == 0


class TestCancelLadder:
    def test_cancel_unfilled(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        mgr.tracker.add(TrackedOrder(
            order_id="o2", market_id=market.market_id,
            token_id="tok_dn", side=Side.DOWN,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        cancelled = mgr.cancel_ladder(market.market_id)
        assert cancelled == 2


class TestCancelAllLadders:
    def test_cancel_all_cancels_every_market(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0, placed_at=1000.0))
        mgr.tracker.add(TrackedOrder(order_id="o2", market_id="m2", token_id="t", side=Side.DOWN, price=0.48, size=10.0, placed_at=1000.0))
        from polybot.ladder_manager import LadderState
        mgr.ladders["m1"] = LadderState(market_id="m1", asset="BTC", anchor_up=0.45, anchor_dn=0.48, posted_at=1000.0)
        mgr.ladders["m2"] = LadderState(market_id="m2", asset="ETH", anchor_up=0.44, anchor_dn=0.49, posted_at=1000.0)

        cancelled = mgr.cancel_all_ladders()
        assert cancelled == 2
        assert len(mgr.tracker.get_resting("m1")) == 0
        assert len(mgr.tracker.get_resting("m2")) == 0
        assert "m1" in mgr.ladders
        assert "m2" in mgr.ladders


class TestPartialFillCrediting:
    def test_partial_fill_credited_to_position_manager(self, cfg, market, mock_clob):
        """Partial fill via size_matched should credit PositionManager."""
        mgr = _make_manager(cfg, mock_clob)
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        # Exchange returns o1 with size_matched=4.0 (partial fill)
        mock_clob.get_open_orders.return_value = [{"id": "o1", "size_matched": "4.0"}]
        fills = mgr.check_fills()
        # check_fills returns only fully filled orders
        assert len(fills) == 0
        # But position manager should have the partial fill credited
        pos = mgr.positions.positions.get(market.market_id)
        assert pos is not None
        assert pos.up_qty == pytest.approx(4.0)
        from polybot.fees import compute_fee
        expected_cost = 4.0 * (0.45 + compute_fee(0.45, cfg.maker_fee_rate))
        assert pos.up_cost == pytest.approx(expected_cost)

    def test_partial_fill_then_cancel_no_loss(self, cfg, market, mock_clob):
        """Partial fill + cancel_ladder should keep the partial fill in position manager."""
        mgr = _make_manager(cfg, mock_clob)
        from polybot.order_tracker import TrackedOrder
        from polybot.ladder_manager import LadderState
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        mgr.ladders[market.market_id] = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
        )
        # Simulate partial fill
        mgr.tracker.update_fill("o1", 4.0)
        # Cancel ladder (which should flush uncredited fills first)
        mgr.cancel_ladder(market.market_id)
        # Position manager should have the partial fill
        pos = mgr.positions.positions.get(market.market_id)
        assert pos is not None
        assert pos.up_qty == pytest.approx(4.0)
        from polybot.fees import compute_fee
        expected_cost = 4.0 * (0.45 + compute_fee(0.45, cfg.maker_fee_rate))
        assert pos.up_cost == pytest.approx(expected_cost)

    def test_full_fill_after_partial_no_double_credit(self, cfg, market, mock_clob):
        """Partial fill credited, then order disappears (full fill) — no double credit."""
        mgr = _make_manager(cfg, mock_clob)
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        # First check_fills: partial fill of 4
        mock_clob.get_open_orders.return_value = [{"id": "o1", "size_matched": "4.0"}]
        mgr.check_fills()
        # Second check_fills: order disappeared (fully filled)
        mock_clob.get_open_orders.return_value = []
        mgr.check_fills()
        # Position manager should have total of 10.0, not 14.0
        pos = mgr.positions.positions[market.market_id]
        assert pos.up_qty == pytest.approx(10.0)
        from polybot.fees import compute_fee
        expected_cost = 10.0 * (0.45 + compute_fee(0.45, cfg.maker_fee_rate))
        assert pos.up_cost == pytest.approx(expected_cost)

    def test_reprice_flushes_partial_before_cancel(self, cfg, market, mock_clob):
        """Partial fill on UP side, reprice triggers, verify UP partial is in position manager."""
        mgr = _make_manager(cfg, mock_clob)
        from polybot.order_tracker import TrackedOrder
        from polybot.ladder_manager import LadderState
        mgr.tracker.add(TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        ))
        mgr.ladders[market.market_id] = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
            last_reprice_at=0.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        # Simulate partial fill
        mgr.tracker.update_fill("o1", 4.0)
        # Set best_ask to trigger reprice
        mock_clob.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.34", size="5000")],
            asks=[MagicMock(price="0.36", size="5000")],
        )
        mgr.reprice_if_needed({market.market_id: market})
        # Partial fill should be in position manager
        pos = mgr.positions.positions.get(market.market_id)
        assert pos is not None
        assert pos.up_qty == pytest.approx(4.0)
        from polybot.fees import compute_fee
        expected_cost = 4.0 * (0.45 + compute_fee(0.45, cfg.maker_fee_rate))
        assert pos.up_cost == pytest.approx(expected_cost)


class TestClearCancelledLadders:
    def test_clear_removes_ladders_with_no_resting(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.ladder_manager import LadderState
        mgr.ladders["m1"] = LadderState(market_id="m1", asset="BTC", anchor_up=0.45, anchor_dn=0.48, posted_at=1000.0)
        mgr.ladders["m2"] = LadderState(market_id="m2", asset="ETH", anchor_up=0.44, anchor_dn=0.49, posted_at=1000.0)
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(order_id="o1", market_id="m2", token_id="t", side=Side.UP, price=0.44, size=10.0))

        mgr.clear_cancelled_ladders()
        assert "m1" not in mgr.ladders
        assert "m2" in mgr.ladders


# ── Proposal #47: FV cancel circuit breaker ──────────────────────────────────

class TestFvCancelCircuitBreaker:
    """Tests for the FV-cancel circuit breaker added by Proposal #47."""

    def _make_mgr_with_ladder(self, cfg, mock_clob):
        """Create a LadderManager with a pre-seeded ladder and resting orders."""
        from polybot.ladder_manager import LadderState
        from polybot.order_tracker import TrackedOrder
        import time as _time

        mgr = _make_manager(cfg, mock_clob)
        mid = market_id = "btc-15m-100"
        now = int(_time.time())
        mw = MarketWindow(
            market_id=mid, condition_id="0xabc", asset="BTC",
            timeframe_sec=900, up_token_id="tok_up", dn_token_id="tok_dn",
            open_epoch=now - 100, close_epoch=now + 800,
        )
        state = LadderState(
            market_id=mid, asset="BTC",
            anchor_up=0.45, anchor_dn=0.48,
            posted_at=float(now - 100),
            timeframe_sec=900,
            up_token_id="tok_up",
            dn_token_id="tok_dn",
        )
        mgr.ladders[mid] = state

        # Add 10 UP orders so cancels return non-empty each call
        for i in range(10):
            mgr.tracker.add(TrackedOrder(
                order_id=f"up_{i}", market_id=mid, token_id="tok_up",
                side=Side.UP, price=0.45 - i * 0.01, size=5.0,
                placed_at=float(now),
            ))
        return mgr, mw, mid, state

    def test_circuit_breaker_fires_after_3_cancels_in_60s(self, cfg, mock_clob):
        """After 3 FV cancels within 60s, the 4th call kills the ladder.

        Circuit breaker: if history has >= 3 entries (from prior calls), kill whole ladder.
        Each successful cancel appends to history. Kill fires on the call that sees >= 3 entries.
        """
        import time as _time

        mgr, mw, mid, state = self._make_mgr_with_ladder(cfg, mock_clob)
        # Use fair_up=0.12 -> certainty=0.88 > 0.75 threshold, UP is losing side

        def _replenish_and_reset(batch_name):
            from polybot.order_tracker import TrackedOrder
            for i in range(5):
                mgr.tracker.add(TrackedOrder(
                    order_id=f"{batch_name}_{i}", market_id=mid, token_id="tok_up",
                    side=Side.UP, price=0.44 - i * 0.01, size=5.0,
                    placed_at=_time.time(),
                ))
            state.heavy_side_locked = None  # reset lock to allow next cancel

        # Call 1 → history has 0 before, breaker skips, cancel succeeds, history=[t1]
        mgr.cancel_losing_side_orders(mw, fair_up=0.12)
        assert mid not in mgr._killed_ladders
        assert len(state.fv_cancel_history) == 1

        _replenish_and_reset("b2")
        # Call 2 → history has 1 before, breaker skips, cancel succeeds, history=[t1,t2]
        mgr.cancel_losing_side_orders(mw, fair_up=0.12)
        assert mid not in mgr._killed_ladders
        assert len(state.fv_cancel_history) == 2

        _replenish_and_reset("b3")
        # Call 3 → history has 2 before, breaker skips, cancel succeeds, history=[t1,t2,t3]
        mgr.cancel_losing_side_orders(mw, fair_up=0.12)
        assert mid not in mgr._killed_ladders
        assert len(state.fv_cancel_history) == 3

        _replenish_and_reset("b4")
        # Call 4 → history has 3 before → circuit breaker fires → kills ladder
        result = mgr.cancel_losing_side_orders(mw, fair_up=0.12)
        assert mid in mgr._killed_ladders, (
            "Ladder should be killed when fv_cancel_history reaches 3 entries within 60s"
        )
        assert result == 0, "Circuit breaker returns 0 (whole-ladder kill, not per-side cancel)"

    def test_circuit_breaker_does_not_fire_if_cancels_sparse(self, cfg, mock_clob):
        """3 cancels spread over 180s should NOT trigger the breaker."""
        import time as _time

        mgr, mw, mid, state = self._make_mgr_with_ladder(cfg, mock_clob)

        # Manually seed history with 2 old timestamps (>60s ago) and 0 recent
        state.fv_cancel_history = [_time.time() - 120.0, _time.time() - 90.0]

        # One fresh cancel: after pruning, only 1 recent entry total
        mgr.cancel_losing_side_orders(mw, fair_up=0.12)

        # Should NOT be killed — only 1 recent cancel
        assert mid not in mgr._killed_ladders, "Should not kill after 1 recent cancel (2 old pruned)"

    def test_threshold_is_75pct(self, mock_clob):
        """cancel_losing_side_orders must NOT fire at cert < 0.75 (Proposal #48).

        Note: _make_mgr_with_ladder seeds UP orders.  fair_up=0.20 -> UP is losing
        side, so cancel_side(Side.UP) returns the seeded orders.
        """
        fv_cfg = BotConfig(
            private_key="0xfake", api_key="key", api_secret="secret", api_passphrase="pass",
            ladder_rungs=8, ladder_spacing=0.02, ladder_width=0.10, ladder_size_skew=2.0,
            reprice_threshold=0.02, max_imbalance_ratio=0.60, imbalance_timeout_sec=30,
            fair_value_enabled=True,
        )

        # Test: fair_up=0.20 -> certainty=80% >= 0.75 -> UP is losing -> should cancel UP orders
        mgr, mw, mid, state = self._make_mgr_with_ladder(fv_cfg, mock_clob)
        result = mgr.cancel_losing_side_orders(mw, fair_up=0.20)
        assert result > 0, "Should cancel at 80% certainty (above 0.75 threshold)"

        # Test: fair_up=0.26 -> certainty=74% -> below 0.75 threshold -> should NOT cancel
        mgr2, mw2, mid2, state2 = self._make_mgr_with_ladder(fv_cfg, mock_clob)
        result2 = mgr2.cancel_losing_side_orders(mw2, fair_up=0.26)
        assert result2 == 0, (
            f"Should NOT cancel at 74% certainty (threshold is 0.75), got result={result2}"
        )

    def test_circuit_breaker_prunes_old_history(self, cfg, mock_clob):
        """Entries older than 60s must be pruned before checking the threshold."""
        import time as _time

        mgr, mw, mid, state = self._make_mgr_with_ladder(cfg, mock_clob)

        # Plant 5 old entries (>60s) — these should be pruned
        old_ts = _time.time() - 90.0
        state.fv_cancel_history = [old_ts] * 5

        # Call once — should prune old entries, then add 1 fresh
        mgr.cancel_losing_side_orders(mw, fair_up=0.12)

        # After the call, history has only the 1 fresh entry (old 5 were pruned)
        assert len(state.fv_cancel_history) == 1, (
            f"Expected 1 entry after pruning 5 old ones, got {len(state.fv_cancel_history)}"
        )
        assert mid not in mgr._killed_ladders, "Should not kill after prune left only 1 recent"

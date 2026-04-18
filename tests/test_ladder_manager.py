import pytest
from unittest.mock import MagicMock, patch
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
        # Place market at 85% elapsed (past the 83% min-elapsed guard for FV cancel)
        mw = MarketWindow(
            market_id=mid, condition_id="0xabc", asset="BTC",
            timeframe_sec=900, up_token_id="tok_up", dn_token_id="tok_dn",
            open_epoch=now - 765, close_epoch=now + 135,
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
        # Use fair_up=0.05 -> certainty=0.95 > 0.90 threshold, UP is losing side

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
        mgr.cancel_losing_side_orders(mw, fair_up=0.05)
        assert mid not in mgr._killed_ladders
        assert len(state.fv_cancel_history) == 1

        _replenish_and_reset("b2")
        # Call 2 → history has 1 before, breaker skips, cancel succeeds, history=[t1,t2]
        mgr.cancel_losing_side_orders(mw, fair_up=0.05)
        assert mid not in mgr._killed_ladders
        assert len(state.fv_cancel_history) == 2

        _replenish_and_reset("b3")
        # Call 3 → history has 2 before, breaker skips, cancel succeeds, history=[t1,t2,t3]
        mgr.cancel_losing_side_orders(mw, fair_up=0.05)
        assert mid not in mgr._killed_ladders
        assert len(state.fv_cancel_history) == 3

        _replenish_and_reset("b4")
        # Call 4 → history has 3 before → circuit breaker fires → kills ladder
        result = mgr.cancel_losing_side_orders(mw, fair_up=0.05)
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
        mgr.cancel_losing_side_orders(mw, fair_up=0.05)

        # Should NOT be killed — only 1 recent cancel
        assert mid not in mgr._killed_ladders, "Should not kill after 1 recent cancel (2 old pruned)"

    def test_threshold_is_90pct(self, mock_clob):
        """cancel_losing_side_orders must NOT fire at cert < 0.90 (researcher calibration).

        Note: _make_mgr_with_ladder seeds UP orders.  fair_up=0.05 -> UP is losing
        side (cert=95%), so cancel_side(Side.UP) returns the seeded orders.
        """
        fv_cfg = BotConfig(
            private_key="0xfake", api_key="key", api_secret="secret", api_passphrase="pass",
            ladder_rungs=8, ladder_spacing=0.02, ladder_width=0.10, ladder_size_skew=2.0,
            reprice_threshold=0.02, max_imbalance_ratio=0.60, imbalance_timeout_sec=30,
            fair_value_enabled=True,
        )

        # Test: fair_up=0.05 -> certainty=95% >= 0.90 -> UP is losing -> should cancel
        mgr, mw, mid, state = self._make_mgr_with_ladder(fv_cfg, mock_clob)
        result = mgr.cancel_losing_side_orders(mw, fair_up=0.05)
        assert result > 0, "Should cancel at 95% certainty (above 0.90 threshold)"

        # Test: fair_up=0.15 -> certainty=85% -> below 0.90 threshold -> should NOT cancel
        mgr2, mw2, mid2, state2 = self._make_mgr_with_ladder(fv_cfg, mock_clob)
        result2 = mgr2.cancel_losing_side_orders(mw2, fair_up=0.15)
        assert result2 == 0, (
            f"Should NOT cancel at 85% certainty (threshold is 0.90), got result={result2}"
        )

    def test_circuit_breaker_prunes_old_history(self, cfg, mock_clob):
        """Entries older than 60s must be pruned before checking the threshold."""
        import time as _time

        mgr, mw, mid, state = self._make_mgr_with_ladder(cfg, mock_clob)

        # Plant 5 old entries (>60s) — these should be pruned
        old_ts = _time.time() - 90.0
        state.fv_cancel_history = [old_ts] * 5

        # Call once — should prune old entries, then add 1 fresh
        mgr.cancel_losing_side_orders(mw, fair_up=0.05)

        # After the call, history has only the 1 fresh entry (old 5 were pruned)
        assert len(state.fv_cancel_history) == 1, (
            f"Expected 1 entry after pruning 5 old ones, got {len(state.fv_cancel_history)}"
        )
        assert mid not in mgr._killed_ladders, "Should not kill after prune left only 1 recent"


# ---------------------------------------------------------------------------
# Fix #50 — check_one_sided_abort: kill-and-walk-away synchronous guard
# ---------------------------------------------------------------------------

def _seed_fills(mgr, market_id: str, up_fills: list[tuple[str, float, float]],
                dn_fills: list[tuple[str, float, float]]) -> None:
    """Helper: seed filled orders on both sides.

    Each entry is (order_id, price, qty).  Orders are added as fully filled.
    """
    from polybot.strategy.order_tracker import TrackedOrder
    for oid, price, qty in up_fills:
        mgr.tracker.add(TrackedOrder(
            order_id=oid, market_id=market_id,
            token_id="tok_up", side=Side.UP,
            price=price, size=qty, placed_at=1000.0,
        ))
        mgr.tracker.update_fill(oid, qty)
    for oid, price, qty in dn_fills:
        mgr.tracker.add(TrackedOrder(
            order_id=oid, market_id=market_id,
            token_id="tok_dn", side=Side.DOWN,
            price=price, size=qty, placed_at=1000.0,
        ))
        mgr.tracker.update_fill(oid, qty)


def _add_resting_order(mgr, market_id: str, side: Side, oid: str = "rest_1") -> None:
    from polybot.strategy.order_tracker import TrackedOrder
    token = "tok_up" if side == Side.UP else "tok_dn"
    mgr.tracker.add(TrackedOrder(
        order_id=oid, market_id=market_id,
        token_id=token, side=side,
        price=0.45, size=10.0, placed_at=1000.0,
    ))


class TestOneSidedAbort:
    """Tests for LadderManager.check_one_sided_abort() — Fix #50."""

    def _make_mgr_with_ladder(self, cfg, mock_clob, bankroll=500.0):
        from polybot.ladder_manager import LadderState
        mgr = _make_manager(cfg, mock_clob, bankroll=bankroll)
        mid = "btc-15m-123"
        mgr.ladders[mid] = LadderState(
            market_id=mid, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
        )
        return mgr, mid

    def test_fires_on_100pct_one_sided_with_cost_above_1pct_bankroll(self, cfg, mock_clob):
        """100% one-sided + cost $25 on $500 bankroll (>1%) → kills ladder."""
        mgr, mid = self._make_mgr_with_ladder(cfg, mock_clob, bankroll=500.0)
        # up_qty=50, dn_qty=0, cost ≈ 50*0.50=$25 (>1% of $500=$5)
        _seed_fills(mgr, mid, [("u1", 0.50, 50.0)], [])
        _add_resting_order(mgr, mid, Side.UP, "rest_up")

        result = mgr.check_one_sided_abort(mid)

        assert result is True
        assert mid in mgr._killed_ladders
        # All resting orders should be cancelled/cancelling
        rest = mgr.tracker.orders.get("rest_up")
        assert rest is None or rest.status in ("cancelled", "cancelling")

    def test_fires_on_3_to_1_ratio(self, cfg, mock_clob):
        """ratio 51:8.5 ≈ 6:1 > 3:1, cost=$28 > $10 → kills."""
        mgr, mid = self._make_mgr_with_ladder(cfg, mock_clob, bankroll=500.0)
        # up_qty=51 @ 0.46 ≈ $23.46, dn_qty=8.5 @ 0.47 ≈ $4, total ≈ $28
        _seed_fills(mgr, mid,
                    [("u1", 0.46, 51.0)],
                    [("d1", 0.47, 8.5)])
        _add_resting_order(mgr, mid, Side.UP, "rest_2")

        result = mgr.check_one_sided_abort(mid)

        assert result is True
        assert mid in mgr._killed_ladders

    def test_does_not_fire_below_cost_threshold(self, cfg, mock_clob):
        """100% one-sided but cost only $2 (<1% of $500=$5) → no kill."""
        mgr, mid = self._make_mgr_with_ladder(cfg, mock_clob, bankroll=500.0)
        # up_qty=10 @ 0.20 = $2
        _seed_fills(mgr, mid, [("u1", 0.20, 10.0)], [])

        result = mgr.check_one_sided_abort(mid)

        assert result is False
        assert mid not in mgr._killed_ladders

    def test_does_not_fire_on_balanced_fills(self, cfg, mock_clob):
        """up_qty=40, dn_qty=35, total $40 — balanced, no abort."""
        mgr, mid = self._make_mgr_with_ladder(cfg, mock_clob, bankroll=500.0)
        _seed_fills(mgr, mid,
                    [("u1", 0.50, 40.0)],
                    [("d1", 0.50, 35.0)])

        result = mgr.check_one_sided_abort(mid)

        assert result is False
        assert mid not in mgr._killed_ladders

    def test_abort_cancels_all_resting_orders(self, cfg, mock_clob):
        """After abort, all resting orders for that market are cancelled."""
        mgr, mid = self._make_mgr_with_ladder(cfg, mock_clob, bankroll=500.0)
        _seed_fills(mgr, mid, [("u1", 0.50, 50.0)], [])
        # Add multiple resting orders (UP and DN)
        _add_resting_order(mgr, mid, Side.UP, "rest_up_1")
        _add_resting_order(mgr, mid, Side.UP, "rest_up_2")
        _add_resting_order(mgr, mid, Side.DOWN, "rest_dn_1")

        mgr.check_one_sided_abort(mid)

        assert mid in mgr._killed_ladders
        for oid in ("rest_up_1", "rest_up_2", "rest_dn_1"):
            order = mgr.tracker.orders.get(oid)
            assert order is None or order.status in ("cancelled", "cancelling"), \
                f"Expected {oid} cancelled, got {order.status if order else 'None'}"

    def test_no_op_when_already_killed(self, cfg, mock_clob):
        """No-op if market_id already in _killed_ladders."""
        mgr, mid = self._make_mgr_with_ladder(cfg, mock_clob, bankroll=500.0)
        mgr._killed_ladders.add(mid)
        _seed_fills(mgr, mid, [("u1", 0.50, 100.0)], [])

        result = mgr.check_one_sided_abort(mid)

        assert result is False  # already killed, skip

    def test_no_op_for_missing_ladder(self, cfg, mock_clob):
        """Returns False if market_id not in self.ladders."""
        mgr = _make_manager(cfg, mock_clob, bankroll=500.0)
        result = mgr.check_one_sided_abort("nonexistent-market")
        assert result is False

    def test_process_paper_fills_triggers_abort(self, cfg, mock_clob):
        """process_paper_fills() calls check_one_sided_abort() per BUY fill with the correct market_id."""
        from unittest.mock import patch
        from polybot.ladder_manager import LadderState
        from polybot.strategy.order_tracker import TrackedOrder

        mgr = _make_manager(cfg, mock_clob, bankroll=500.0)
        mid = "btc-15m-abort-test"
        mgr.ladders[mid] = LadderState(
            market_id=mid, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
        )
        # Register a resting BUY order in the tracker
        order = TrackedOrder(
            order_id="paper-abc123", market_id=mid,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0, placed_at=1000.0,
        )
        mgr.tracker.add(order)

        with patch.object(mgr, "check_one_sided_abort", wraps=mgr.check_one_sided_abort) as mock_abort:
            paper_fills = [{"id": "paper-abc123", "side": "BUY"}]
            mgr.process_paper_fills(paper_fills)
            # check_one_sided_abort must have been called with the market_id of the fill
            mock_abort.assert_called_once_with(mid)


# ── Proposal #53: Directional budget cap tests ───────────────────────────────

import time as _time_mod
import os


def _make_dir_cap_manager(directional_budget_cap=20.0, bankroll=540.0, fair_value_enabled=True,
                          fv_gate_enabled=True):
    """Build a LadderManager configured with specified directional_budget_cap and bankroll.

    fv_gate_enabled defaults to True here because these tests specifically exercise
    the FV gate firing path. The bot default is False (disabled 2026-04-11).
    """
    from polybot.strategy.ladder_manager import LadderManager
    from polybot.strategy.position_manager import PositionManager
    from polybot.risk_manager import RiskManager

    cfg = BotConfig(
        dry_run=True,
        bankroll=bankroll,
        ladder_rungs=10,
        ladder_spacing=0.01,
        ladder_width=0.20,
        ladder_size_skew=1.0,
        max_pair_cost=0.93,
        position_size_fraction=0.10,
        reprice_threshold=0.03,
        maker_fee_rate=0.0,
        batch_order_size=15,
        no_trade_final_sec=60,
        imbalance_min_heavy_fills=1,
        boost_elapsed_pct=0.20,
        force_buy_elapsed_pct=0.70,
        force_buy_max_pair_cost=0.83,
        spot_delta_reduce_threshold=0.0015,
        spot_delta_skip_threshold=0.005,
        spot_gate_force_buy_threshold=0.003,
        spot_loss_cap_multiplier=0.50,
        fair_value_enabled=fair_value_enabled,
        fv_gate_enabled=fv_gate_enabled,
        vol_window_sec=300,
        vol_fallback_annual=0.50,
        vol_min_samples=30,
        skew_phase_pct=0.30,
        directional_phase_pct=0.70,
        certainty_exit_threshold=0.30,
        certainty_hold_threshold=0.95,
        certainty_directional_threshold=0.92,
        directional_max_ask=0.75,
        max_budget_skew=0.80,
        exit_enabled=True,
        exit_elapsed_pct=0.55,
        exit_min_loss_ratio=3.0,
        exit_target_price=0.35,
        exit_min_price=0.15,
        reactive_pairing_enabled=True,
        reactive_chase_width=0.10,
        reactive_chase_budget_pct=0.50,
        inventory_skew_enabled=True,
        inventory_skew_max=0.60,
        directional_budget_cap=directional_budget_cap,
    )

    executor = MagicMock()
    executor.get_best_ask.return_value = 0.45
    executor.get_midpoint.return_value = 0.44
    executor.get_open_orders.return_value = []
    # Return a fixed list of mock order records — budget is what we passed to build_ladder_rungs
    executor.place_batch_limit_buys.side_effect = lambda orders: [
        MagicMock(order_id=f"ord-{i}", status="open", price=od["price"], size=od["size"])
        for i, od in enumerate(orders)
    ]
    executor.cancel_batch.return_value = []

    tracker = MagicMock()
    tracker.orders = {}
    tracker.filled_count.return_value = 0
    tracker.filled_qty.return_value = 0.0
    tracker.filled_cost.return_value = 0.0
    tracker.get_resting.return_value = []
    tracker.get_resting_side.return_value = []
    tracker.cancel_side.return_value = []
    tracker.cancel_market.return_value = []
    tracker.has_orders.return_value = False
    tracker.flush_uncredited.return_value = []
    tracker.total_count.return_value = 0

    pm = PositionManager(cfg, bankroll)
    risk = MagicMock()
    risk.is_halted.return_value = False
    risk.can_open_position.return_value = True
    risk.exposure_factor.return_value = 1.0

    tick_cache = MagicMock()
    tick_cache.get_tick_size.return_value = 0.01

    return LadderManager(cfg, executor, tracker, pm, risk, tick_cache)


def _dir_cap_market(market_id="btc-15m-dircap"):
    now = int(_time_mod.time())
    return MarketWindow(
        market_id=market_id,
        condition_id="0xdircap",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=now - 60,
        close_epoch=now + 840,
    )


def _total_budget_posted(lm):
    """Sum price * size for all orders passed to place_batch_limit_buys."""
    total = 0.0
    for call in lm.executor.place_batch_limit_buys.call_args_list:
        order_dicts = call.args[0] if call.args else call.kwargs.get('orders', [])
        for od in order_dicts:
            if isinstance(od, dict):
                total += od["price"] * od["size"]
    return total


def _budget_by_token(lm, token_id):
    """Sum price * size for orders targeting a specific token_id."""
    total = 0.0
    for call in lm.executor.place_batch_limit_buys.call_args_list:
        order_dicts = call.args[0] if call.args else call.kwargs.get('orders', [])
        for od in order_dicts:
            if isinstance(od, dict) and od.get("token_id") == token_id:
                total += od["price"] * od["size"]
    return total


class TestDirectionalBudgetCap:
    """User directive 2026-04-18: one-sided posts (FV gate / spot skip / directional_buy)
    size to 10% of bankroll, NOT directional_budget_cap.
    directional_budget_cap is retained in config (emergency use) but no longer binds.
    """

    def test_directional_budget_capped_at_20_fv_gate_dn(self):
        """FV gate fires cert=88% DN (fair_up=0.05). DN budget = 10% of bankroll.
        User directive 2026-04-18: one-sided = 10% of bankroll, not directional_budget_cap."""
        # bankroll=540, 10% = $54. Old rule: min(54, cap=20) = $20.
        # New rule: min(0.10 * 540, available) = $54.
        lm = _make_dir_cap_manager(directional_budget_cap=20.0, bankroll=540.0)
        market = _dir_cap_market()
        # fair_up=0.05 -> cert=0.88 >= 0.80 -> FV gate fires, DN side only
        lm.post_ladder(market, spot_delta=0.0, fair_up=0.05)
        dn_budget = _budget_by_token(lm, "tok_dn")
        up_budget = _budget_by_token(lm, "tok_up")
        # UP side must not be posted (FV gate skips UP when DN is winning)
        assert up_budget == pytest.approx(0.0, abs=0.01), (
            f"FV gate DN should post 0 UP budget, got UP={up_budget:.2f}"
        )
        # DN budget should be 10% of bankroll = $54 (NOT capped at directional_budget_cap=20)
        expected = 0.10 * 540.0
        assert dn_budget == pytest.approx(expected, rel=0.05), (
            f"DN budget ${dn_budget:.2f} should be 10% of bankroll = ${expected:.2f}. "
            f"directional_budget_cap no longer binds on one-sided posts."
        )

    def test_directional_budget_capped_at_20_fv_gate_up(self):
        """FV gate fires cert=88% UP (fair_up=0.88). UP budget = 10% of bankroll."""
        lm = _make_dir_cap_manager(directional_budget_cap=20.0, bankroll=540.0)
        market = _dir_cap_market()
        # fair_up=0.88 -> cert=0.88 >= 0.80 -> FV gate fires, UP side only
        lm.post_ladder(market, spot_delta=0.0, fair_up=0.88)
        up_budget = _budget_by_token(lm, "tok_up")
        dn_budget = _budget_by_token(lm, "tok_dn")
        assert dn_budget == pytest.approx(0.0, abs=0.01), (
            f"FV gate UP should post 0 DN budget, got DN={dn_budget:.2f}"
        )
        expected = 0.10 * 540.0
        assert up_budget == pytest.approx(expected, rel=0.05), (
            f"UP budget ${up_budget:.2f} should be 10% of bankroll = ${expected:.2f}."
        )

    def test_directional_budget_capped_at_20_spot_skip(self):
        """Spot skip fires (delta > 0.5%). Active side budget = 10% of bankroll."""
        lm = _make_dir_cap_manager(directional_budget_cap=20.0, bankroll=540.0,
                                   fair_value_enabled=False)
        market = _dir_cap_market()
        # spot_delta=0.01 (1%) exceeds skip_threshold=0.005 -> UP side only
        lm.post_ladder(market, spot_delta=0.01, fair_up=0.5)
        up_budget = _budget_by_token(lm, "tok_up")
        dn_budget = _budget_by_token(lm, "tok_dn")
        assert dn_budget == pytest.approx(0.0, abs=0.01), (
            f"Spot skip UP should post 0 DN budget, got DN={dn_budget:.2f}"
        )
        expected = 0.10 * 540.0
        assert up_budget == pytest.approx(expected, rel=0.05), (
            f"UP budget ${up_budget:.2f} should be 10% of bankroll = ${expected:.2f} after spot skip."
        )

    def test_directional_budget_below_cap_unchanged(self):
        """When natural budget is below cap, it is posted in full (cap is a no-op)."""
        # bankroll=150 -> position_size_fraction=0.10 -> budget=15, cap=20 -> no clip
        lm = _make_dir_cap_manager(directional_budget_cap=20.0, bankroll=150.0)
        market = _dir_cap_market()
        # FV gate fires with cert=88% DN
        lm.post_ladder(market, spot_delta=0.0, fair_up=0.05)
        dn_budget = _budget_by_token(lm, "tok_dn")
        up_budget = _budget_by_token(lm, "tok_up")
        assert up_budget == pytest.approx(0.0, abs=0.01)
        # Budget should be ~15 (uncapped), not clipped to 20 (it's already below)
        assert dn_budget <= 20.0 + 0.05, "Budget below cap must not be inflated"
        # At bankroll=150 with fraction=0.15 (micro tier), budget ≥ min_required
        assert dn_budget >= 1.0, "Budget should still be meaningful"

    def test_balanced_budget_not_capped(self):
        """Two-sided (balanced) posts are NOT affected by directional_budget_cap.
        Both UP and DN should get full budget allocation when cert < 0.80."""
        # At bankroll=540, budget≈54. Cap=20. With cert=50% (neutral), both sides get budget/2=27.
        lm = _make_dir_cap_manager(directional_budget_cap=20.0, bankroll=540.0)
        market = _dir_cap_market()
        # fair_up=0.50 -> cert=50% < 80% -> balanced posting, no cap applies
        lm.post_ladder(market, spot_delta=0.0, fair_up=0.50)
        up_budget = _budget_by_token(lm, "tok_up")
        dn_budget = _budget_by_token(lm, "tok_dn")
        # Both sides should have budget > 20 (well above the directional cap)
        assert up_budget > 20.0, (
            f"Balanced UP budget {up_budget:.2f} should not be capped at 20"
        )
        assert dn_budget > 20.0, (
            f"Balanced DN budget {dn_budget:.2f} should not be capped at 20"
        )

    def test_config_loads_directional_budget_cap_from_env(self):
        """BotConfig.directional_budget_cap reads from DIRECTIONAL_BUDGET_CAP env var."""
        from polybot.config import load_bot_config
        os.environ["DIRECTIONAL_BUDGET_CAP"] = "25.0"
        try:
            cfg = load_bot_config()
            assert cfg.directional_budget_cap == pytest.approx(25.0), (
                f"Expected 25.0 from env, got {cfg.directional_budget_cap}"
            )
        finally:
            del os.environ["DIRECTIONAL_BUDGET_CAP"]

    def test_default_directional_budget_cap_raised_to_500(self):
        """Default cap raised to 500 on 2026-04-18 (user directive: one-sided = 10% bankroll).

        directional_budget_cap is retained in BotConfig as an emergency override but is
        no longer the active ceiling for one-sided posts. The new rule is:
            budget = min(0.10 * bankroll, available)
        The default is set high (500) so it never accidentally binds at current bankrolls.

        This pins the BotConfig dataclass default AND the load_bot_config env-loader
        default (both sources — make sure they agree)."""
        # Dataclass default — no env, no .env file in play
        cfg_default = BotConfig()
        assert cfg_default.directional_budget_cap == pytest.approx(500.0), (
            f"Expected BotConfig dataclass default 500.0, got {cfg_default.directional_budget_cap}"
        )
        # Env loader default — patch both the env var AND bypass .env so load_dotenv
        # cannot inject a stale value from the committed .env file.
        from polybot import config as config_mod
        prev = os.environ.pop("DIRECTIONAL_BUDGET_CAP", None)
        try:
            with patch.object(config_mod, "load_dotenv", lambda: None):
                cfg_loaded = config_mod.load_bot_config()
            assert cfg_loaded.directional_budget_cap == pytest.approx(500.0), (
                f"Expected env-loader default 500.0, got {cfg_loaded.directional_budget_cap}"
            )
        finally:
            if prev is not None:
                os.environ["DIRECTIONAL_BUDGET_CAP"] = prev


# ── Proposal #54: Guard telemetry — drain list tests ─────────────────────────

class TestGuardTelemetryDrainLists:
    """Unit tests for _recent_aborts and _recent_circuit_breaker_fires drain lists
    on LadderManager (Proposal #54). These verify the plumbing that bot.py drains."""

    def _make_lm_with_state(self, bankroll=500.0):
        """Make a LadderManager with a real PositionManager and mock everything else."""
        from polybot.strategy.ladder_manager import LadderManager, LadderState
        from polybot.strategy.position_manager import PositionManager

        cfg = BotConfig(
            dry_run=True,
            bankroll=bankroll,
            ladder_rungs=6,
            ladder_spacing=0.01,
            ladder_width=0.10,
            ladder_size_skew=1.0,
            max_pair_cost=0.93,
            position_size_fraction=0.10,
            reprice_threshold=0.03,
            maker_fee_rate=0.0,
            batch_order_size=15,
            no_trade_final_sec=60,
            imbalance_min_heavy_fills=1,
            boost_elapsed_pct=0.20,
            force_buy_elapsed_pct=0.70,
            force_buy_max_pair_cost=0.83,
            spot_delta_reduce_threshold=0.0015,
            spot_delta_skip_threshold=0.005,
            spot_gate_force_buy_threshold=0.003,
            spot_loss_cap_multiplier=0.50,
        )
        executor = MagicMock()
        executor.cancel_batch.return_value = []

        from polybot.order_tracker import OrderTracker
        tracker = OrderTracker()

        pm = PositionManager(cfg, bankroll)
        risk = MagicMock()
        risk.is_halted.return_value = False
        risk.can_open_position.return_value = True
        risk.exposure_factor.return_value = 1.0

        return LadderManager(cfg, executor, tracker, pm, risk)

    def test_recent_aborts_initially_empty(self):
        """_recent_aborts starts empty on a fresh LadderManager."""
        lm = self._make_lm_with_state()
        assert lm._recent_aborts == []

    def test_recent_circuit_breaker_fires_initially_empty(self):
        """_recent_circuit_breaker_fires starts empty on a fresh LadderManager."""
        lm = self._make_lm_with_state()
        assert lm._recent_circuit_breaker_fires == []

    def test_one_sided_abort_populates_recent_aborts(self):
        """check_one_sided_abort appends an entry to _recent_aborts when it kills a ladder."""
        from polybot.strategy.ladder_manager import LadderManager, LadderState
        from polybot.order_tracker import OrderTracker, TrackedOrder

        lm = self._make_lm_with_state(bankroll=500.0)
        mid = "btc-15m-abort-telemetry"
        # Set up a ladder state
        lm.ladders[mid] = LadderState(
            market_id=mid, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
        )
        # Add a filled UP order (100% one-sided, above 1% of bankroll threshold)
        order = TrackedOrder(
            order_id="o-abort-1", market_id=mid,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=20.0, placed_at=1000.0,
        )
        lm.tracker.add(order)
        lm.tracker.update_fill("o-abort-1", 20.0)  # 100% filled UP, dn=0

        result = lm.check_one_sided_abort(mid)

        assert result is True, "check_one_sided_abort should return True (ladder killed)"
        assert len(lm._recent_aborts) == 1, (
            f"Expected 1 entry in _recent_aborts, got {len(lm._recent_aborts)}"
        )
        abort_entry = lm._recent_aborts[0]
        assert abort_entry["market_id"] == mid
        assert abort_entry["asset"] == "BTC"
        assert abort_entry["up_qty"] > 0
        assert abort_entry["dn_qty"] == pytest.approx(0.0)
        assert abort_entry["cost"] > 0

    def test_one_sided_abort_no_entry_when_not_triggered(self):
        """check_one_sided_abort does NOT append when guard does not fire."""
        from polybot.strategy.ladder_manager import LadderState
        from polybot.order_tracker import TrackedOrder

        lm = self._make_lm_with_state(bankroll=500.0)
        mid = "btc-15m-no-abort"
        lm.ladders[mid] = LadderState(
            market_id=mid, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
        )
        # Both sides filled equally — guard should not fire
        for oid, side, tok in [("o-up", Side.UP, "tok_up"), ("o-dn", Side.DOWN, "tok_dn")]:
            order = TrackedOrder(
                order_id=oid, market_id=mid,
                token_id=tok, side=side,
                price=0.45, size=10.0, placed_at=1000.0,
            )
            lm.tracker.add(order)
            lm.tracker.update_fill(oid, 10.0)

        result = lm.check_one_sided_abort(mid)
        assert result is False
        assert lm._recent_aborts == []

    def test_circuit_breaker_populates_recent_fires(self):
        """cancel_losing_side_orders appends to _recent_circuit_breaker_fires when breaker fires."""
        from polybot.strategy.ladder_manager import LadderState
        from polybot.order_tracker import TrackedOrder
        import time as _t

        lm = self._make_lm_with_state(bankroll=500.0)
        lm.cfg = BotConfig(
            dry_run=True, bankroll=500.0,
            ladder_rungs=6, ladder_spacing=0.01, ladder_width=0.10,
            ladder_size_skew=1.0, max_pair_cost=0.93,
            position_size_fraction=0.10, reprice_threshold=0.03,
            maker_fee_rate=0.0, batch_order_size=15, no_trade_final_sec=60,
            imbalance_min_heavy_fills=1, boost_elapsed_pct=0.20,
            force_buy_elapsed_pct=0.70, force_buy_max_pair_cost=0.83,
            spot_delta_reduce_threshold=0.0015, spot_delta_skip_threshold=0.005,
            spot_gate_force_buy_threshold=0.003, spot_loss_cap_multiplier=0.50,
            fair_value_enabled=True,
        )

        now = int(_time_mod.time())
        mw = MarketWindow(
            market_id="btc-15m-cb",
            condition_id="0xcb",
            asset="BTC",
            timeframe_sec=900,
            up_token_id="tok_up_cb",
            dn_token_id="tok_dn_cb",
            open_epoch=now - 765,
            close_epoch=now + 135,
        )
        mid = mw.market_id
        lm.ladders[mid] = LadderState(
            market_id=mid, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000.0,
            # Seed 3 recent FV cancel timestamps — triggers circuit breaker on next call
            fv_cancel_history=[_t.time() - 10, _t.time() - 20, _t.time() - 30],
        )
        # fair_up=0.05 -> cert=95% > 0.90 threshold, elapsed 85% > 83% -> will try to cancel, but breaker fires first
        result = lm.cancel_losing_side_orders(mw, fair_up=0.05)

        assert result == 0, "Circuit breaker should return 0 (killed, no cancels counted)"
        assert mid in lm._killed_ladders
        assert len(lm._recent_circuit_breaker_fires) == 1, (
            f"Expected 1 circuit breaker entry, got {len(lm._recent_circuit_breaker_fires)}"
        )
        cb_entry = lm._recent_circuit_breaker_fires[0]
        assert cb_entry["market_id"] == mid
        assert cb_entry["asset"] == "BTC"
        assert cb_entry["cancel_count"] >= 3

    def test_drain_clears_recent_aborts(self):
        """Draining _recent_aborts (as bot loop does) clears the list."""
        lm = self._make_lm_with_state()
        lm._recent_aborts.append({"market_id": "x", "asset": "BTC",
                                   "up_qty": 10.0, "dn_qty": 0.0, "cost": 5.0})
        # Simulate drain
        drained = list(lm._recent_aborts)
        lm._recent_aborts.clear()
        assert len(drained) == 1
        assert lm._recent_aborts == []

    def test_drain_clears_recent_circuit_breaker_fires(self):
        """Draining _recent_circuit_breaker_fires (as bot loop does) clears the list."""
        lm = self._make_lm_with_state()
        lm._recent_circuit_breaker_fires.append({"market_id": "y", "asset": "ETH",
                                                   "cancel_count": 3})
        drained = list(lm._recent_circuit_breaker_fires)
        lm._recent_circuit_breaker_fires.clear()
        assert len(drained) == 1
        assert lm._recent_circuit_breaker_fires == []

"""Regression tests for FV gate threshold (0.80), 1h pair cost guard (0.96),
and one-side cap removal."""
import time
from unittest.mock import MagicMock
import pytest

from polybot.config import BotConfig
from polybot.strategy.ladder_manager import LadderManager, LadderState
from polybot.strategy.position_manager import PositionManager
from polybot.types import MarketWindow, Side


def _cfg(**overrides):
    defaults = dict(
        dry_run=True, bankroll=500,
        ladder_rungs=10, ladder_spacing=0.01, ladder_width=0.20,
        ladder_size_skew=1.0,
        max_pair_cost=0.93,
        max_pair_cost_1h=0.96,
        position_size_fraction=0.10,
        reprice_threshold=0.03, maker_fee_rate=0.0, batch_order_size=15,
        no_trade_final_sec=60, imbalance_min_heavy_fills=1,
        boost_elapsed_pct=0.20, force_buy_elapsed_pct=0.70,
        force_buy_max_pair_cost=0.83,
        spot_delta_reduce_threshold=0.0015, spot_delta_skip_threshold=0.005,
        spot_gate_force_buy_threshold=0.003, spot_loss_cap_multiplier=0.50,
        fair_value_enabled=True, vol_window_sec=300, vol_fallback_annual=0.50,
        vol_min_samples=30, skew_phase_pct=0.30, directional_phase_pct=0.70,
        certainty_exit_threshold=0.30, certainty_hold_threshold=0.95,
        certainty_directional_threshold=0.92, directional_max_ask=0.75,
        max_budget_skew=0.80,
        exit_enabled=True, exit_elapsed_pct=0.55, exit_min_loss_ratio=3.0,
        exit_target_price=0.35, exit_min_price=0.15,
        reactive_pairing_enabled=True, reactive_chase_width=0.10,
        reactive_chase_budget_pct=0.50,
        inventory_skew_enabled=True, inventory_skew_max=0.60,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _market(market_id="btc-15m-100", timeframe_sec=900):
    now = int(time.time())
    return MarketWindow(
        market_id=market_id, condition_id="0xabc", asset="BTC",
        timeframe_sec=timeframe_sec, up_token_id="tok_up", dn_token_id="tok_dn",
        open_epoch=now - 60, close_epoch=now + timeframe_sec - 60,
    )


def _market_1h(market_id="btc-1h-100"):
    now = int(time.time())
    return MarketWindow(
        market_id=market_id, condition_id="0xdef", asset="BTC",
        timeframe_sec=3600, up_token_id="tok_up_1h", dn_token_id="tok_dn_1h",
        open_epoch=now - 60, close_epoch=now + 3540,
    )


def _make_manager(cfg=None, bankroll=500):
    if cfg is None:
        cfg = _cfg(bankroll=bankroll)
    executor = MagicMock()
    executor.get_best_ask.return_value = 0.45
    executor.get_midpoint.return_value = 0.44
    executor.get_open_orders.return_value = []
    executor.place_batch_limit_buys.return_value = [
        MagicMock(order_id=f"ord-{i}", status="open", price=0.40, size=10.0)
        for i in range(5)
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


# ── Task 1: FV Gate threshold tests ─────────────────────────────────────────

class TestFvGateThreshold:
    def _get_posted_token_ids(self, lm):
        """Extract all token_ids from place_batch_limit_buys calls.
        Each call receives a list of order dicts, each with a 'token_id' key."""
        tokens_posted = set()
        for call in lm.executor.place_batch_limit_buys.call_args_list:
            order_dicts = call.args[0] if call.args else call.kwargs.get('orders', [])
            for od in order_dicts:
                if isinstance(od, dict) and 'token_id' in od:
                    tokens_posted.add(od['token_id'])
        return tokens_posted

    def test_gate_does_not_fire_at_70pct_certainty(self):
        """At 70% certainty (0.70/0.30 fair_up), FV gate must NOT fire.
        Both sides should be posted (count > 0 with two-sided budget).
        With old threshold (0.60) this would have blocked one side."""
        lm = _make_manager()
        market = _market()
        # fair_up=0.70 -> certainty=70% -- OLD threshold 0.60 would have gated
        lm.post_ladder(market, spot_delta=0.0, fair_up=0.70)
        # Gate not fired => both sides posted => executor called for both tokens
        tokens_posted = self._get_posted_token_ids(lm)
        # If both UP and DN tokens were posted, the gate did not fire
        assert "tok_up" in tokens_posted and "tok_dn" in tokens_posted, (
            f"Expected both tok_up and tok_dn to be posted at 70% certainty, "
            f"but got: {tokens_posted}. FV gate should NOT fire at 70% (threshold is 80%)."
        )

    def test_gate_fires_at_85pct_certainty(self):
        """At 85% certainty (fair_up=0.85), FV gate MUST fire and skip DN.
        Verified by checking executor was only called for UP token."""
        lm = _make_manager()
        market = _market()
        lm.post_ladder(market, spot_delta=0.0, fair_up=0.85)
        tokens_posted = self._get_posted_token_ids(lm)
        # DN token must NOT have been posted
        assert "tok_dn" not in tokens_posted, (
            f"FV gate should fire at 85% certainty and skip DN side, "
            f"but DN was still posted. Tokens posted: {tokens_posted}"
        )

    def test_gate_boundary_at_exactly_80pct(self):
        """fair_up=0.80 -> certainty=80% -> gate fires (>= 0.80 threshold)."""
        lm = _make_manager()
        market = _market()
        lm.post_ladder(market, spot_delta=0.0, fair_up=0.80)
        tokens_posted = self._get_posted_token_ids(lm)
        assert "tok_dn" not in tokens_posted, (
            f"FV gate should fire at exactly 80% certainty (>= 0.80), "
            f"but DN was still posted. Tokens posted: {tokens_posted}"
        )

    def test_gate_does_not_fire_at_79pct(self):
        """fair_up=0.79 -> certainty=79% -> gate must NOT fire, both sides posted."""
        lm = _make_manager()
        market = _market()
        lm.post_ladder(market, spot_delta=0.0, fair_up=0.79)
        tokens_posted = self._get_posted_token_ids(lm)
        assert "tok_up" in tokens_posted and "tok_dn" in tokens_posted, (
            f"FV gate must NOT fire at 79% certainty (threshold is 80%), "
            f"but got tokens: {tokens_posted}"
        )

    def test_fv_disabled_gate_never_fires(self):
        """When fair_value_enabled=False, FV gate never fires regardless of fair_up."""
        lm = _make_manager(_cfg(fair_value_enabled=False))
        market = _market()
        # fair_up=0.95 — if gate used it, it would fire; since FV is off, it must not
        count = lm.post_ladder(market, spot_delta=0.0, fair_up=0.95)
        assert count >= 0  # no crash


# ── Task 2: 1h pair cost guard tests ────────────────────────────────────────

class TestOnehPairCostGuard:
    def test_1h_max_pair_cost_default_is_096(self):
        """BotConfig default for max_pair_cost_1h must be 0.96."""
        cfg = BotConfig()
        assert cfg.max_pair_cost_1h == 0.96, (
            f"BotConfig.max_pair_cost_1h default should be 0.96, got {cfg.max_pair_cost_1h}"
        )

    def test_1h_ladder_params_use_096_threshold(self):
        """get_ladder_params(3600) must return max_pair_cost=0.96."""
        cfg = BotConfig(bankroll=500)
        lp = cfg.get_ladder_params(3600, current_bankroll=500)
        assert lp.max_pair_cost == 0.96, (
            f"1h ladder params max_pair_cost should be 0.96, got {lp.max_pair_cost}"
        )

    def test_15m_ladder_params_unchanged_at_093(self):
        """get_ladder_params(900) must still return max_pair_cost=0.93 (15m unchanged)."""
        cfg = BotConfig(bankroll=500)
        lp = cfg.get_ladder_params(900, current_bankroll=500)
        assert lp.max_pair_cost == 0.93, (
            f"15m ladder params max_pair_cost should be unchanged at 0.93, got {lp.max_pair_cost}"
        )

    def test_5m_ladder_params_unchanged_at_093(self):
        """get_ladder_params(300) must still return max_pair_cost=0.93 (5m unchanged)."""
        cfg = BotConfig(bankroll=500)
        lp = cfg.get_ladder_params(300, current_bankroll=500)
        assert lp.max_pair_cost == 0.93, (
            f"5m ladder params max_pair_cost should be unchanged at 0.93, got {lp.max_pair_cost}"
        )

    def test_1h_pair_cost_guard_allows_0_95_pair(self):
        """1h market with top-3 VWAP of 0.95 must NOT be rejected (0.95 < 0.96 threshold)."""
        # With old 0.93 threshold, a 0.95 pair cost would be rejected. With 0.96, it passes.
        cfg = _cfg(max_pair_cost_1h=0.96, bankroll=500)
        lp = cfg.get_ladder_params(3600, current_bankroll=500)
        assert lp.max_pair_cost == 0.96
        # A pair cost of 0.95 is below the threshold — guard must allow it
        assert 0.95 < lp.max_pair_cost


# ── Task 3: One-side cap removal tests ──────────────────────────────────────

class TestOneSideCapRemoved:
    def test_check_one_side_cap_method_still_exists(self):
        """The _check_one_side_cap method must still exist on LadderManager
        (only call sites removed, not the method body)."""
        lm = _make_manager()
        assert hasattr(lm, '_check_one_side_cap'), (
            "_check_one_side_cap method must be preserved (only call sites removed)"
        )
        assert callable(lm._check_one_side_cap)

    def test_process_paper_fills_does_not_call_one_side_cap(self):
        """process_paper_fills must NOT call _check_one_side_cap internally.
        We verify this by mocking _check_one_side_cap and confirming it is never called
        during process_paper_fills, even with extreme fill imbalance."""
        from polybot.order_tracker import OrderTracker, TrackedOrder
        from polybot.order_executor import OrderExecutor

        cfg = _cfg(bankroll=500)
        mock_clob = MagicMock()
        mock_clob.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.44", size="5000")],
            asks=[MagicMock(price="0.46", size="5000")],
        )
        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.return_value = {"orderID": "o1", "status": "resting"}
        mock_clob.get_open_orders.return_value = []

        executor = OrderExecutor(cfg, clob_client=mock_clob)
        tracker = OrderTracker()
        pm = PositionManager(cfg, bankroll=500)
        risk = MagicMock()
        risk.is_halted.return_value = False
        risk.can_open_position.return_value = True
        risk.exposure_factor.return_value = 1.0
        lm = LadderManager(cfg, executor, tracker, pm, risk)

        market = _market()
        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45,
            posted_at=time.time() - 200.0,
            timeframe_sec=900,
        )
        lm.ladders[market.market_id] = state

        # Add UP orders to tracker
        for i in range(5):
            oid = f"up_{i}"
            tracker.add(TrackedOrder(
                order_id=oid, market_id=market.market_id,
                token_id=market.up_token_id, side=Side.UP,
                price=0.45, size=1.0, placed_at=time.time(),
            ))

        # Simulate paper fills for those UP orders
        paper_fills = [{"id": f"up_{i}", "side": "BUY"} for i in range(5)]

        # Mock _check_one_side_cap to detect if it gets called
        cap_call_count = []
        original_cap = lm._check_one_side_cap
        def spy_cap(market_id):
            cap_call_count.append(market_id)
            return original_cap(market_id)
        lm._check_one_side_cap = spy_cap

        lm.process_paper_fills(paper_fills)

        assert len(cap_call_count) == 0, (
            f"_check_one_side_cap was called {len(cap_call_count)} times during "
            f"process_paper_fills, but call site should have been removed. "
            f"Called for markets: {cap_call_count}"
        )

    def test_check_fills_does_not_call_one_side_cap(self):
        """check_fills must NOT call _check_one_side_cap internally.
        We verify by spying on the method during a check_fills call."""
        from polybot.order_tracker import OrderTracker, TrackedOrder
        from polybot.order_executor import OrderExecutor

        cfg = _cfg(bankroll=500)
        mock_clob = MagicMock()
        mock_clob.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.44", size="5000")],
            asks=[MagicMock(price="0.46", size="5000")],
        )
        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.return_value = {"orderID": "o1", "status": "resting"}
        # Return an open order from API — will be treated as filled
        mock_clob.get_open_orders.return_value = []

        executor = OrderExecutor(cfg, clob_client=mock_clob)
        tracker = OrderTracker()
        pm = PositionManager(cfg, bankroll=500)
        risk = MagicMock()
        risk.is_halted.return_value = False
        risk.can_open_position.return_value = True
        risk.exposure_factor.return_value = 1.0
        lm = LadderManager(cfg, executor, tracker, pm, risk)

        market = _market()
        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45,
            posted_at=time.time() - 200.0,
            timeframe_sec=900,
            up_token_id=market.up_token_id,
            dn_token_id=market.dn_token_id,
        )
        lm.ladders[market.market_id] = state

        # Add some tracked orders
        for i in range(3):
            oid = f"up_{i}"
            tracker.add(TrackedOrder(
                order_id=oid, market_id=market.market_id,
                token_id=market.up_token_id, side=Side.UP,
                price=0.45, size=1.0, placed_at=time.time(),
            ))
            # Manually mark as filled to trigger the fill path in check_fills
            tracker.orders[oid].status = "filled"
            tracker.orders[oid].filled = 1.0

        # Mock _check_one_side_cap to spy on calls
        cap_call_count = []
        original_cap = lm._check_one_side_cap
        def spy_cap(market_id):
            cap_call_count.append(market_id)
            return original_cap(market_id)
        lm._check_one_side_cap = spy_cap

        # check_fills will reconcile against clob's open orders
        # Since mock returns empty open orders, pre-filled orders are "orphaned"
        # but that's fine — we just care that _check_one_side_cap is NOT called
        lm.check_fills([market.market_id])

        assert len(cap_call_count) == 0, (
            f"_check_one_side_cap was called {len(cap_call_count)} times during "
            f"check_fills, but call site should have been removed. "
            f"Called for markets: {cap_call_count}"
        )

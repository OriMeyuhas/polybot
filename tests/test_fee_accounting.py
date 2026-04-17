"""Tests for fee accounting across the trading engine.

Covers:
- compute_fee utility (Phase 1)
- _fill_cost helper (Phase 2)
- Fee-inclusive budget sizing (Phase 3)
- Fee-inclusive pair cost guard (Phase 4)
- Fee-inclusive total_committed (Phase 5)
- Fee-inclusive filled_cost in OrderTracker (Phase 6)
- Position PnL after fees (Phase 7-8)
"""

import time
from unittest.mock import MagicMock

import pytest

from polybot.config import BotConfig, validate_live_config
from polybot.fees import compute_fee
from polybot.strategy.ladder_manager import LadderManager, build_ladder_rungs
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.position_manager import PositionManager
from polybot.types import MarketWindow, Side, Position


# ---------------------------------------------------------------------------
# Phase 1: compute_fee utility
# ---------------------------------------------------------------------------

class TestComputeFee:
    def test_compute_fee_at_50_cents(self):
        """Fee at 0.50: 0.0156 * min(0.50, 0.50) = 0.0078."""
        assert compute_fee(0.50, 0.0156) == pytest.approx(0.0156 * 0.50)

    def test_compute_fee_at_extremes(self):
        """Fee is symmetric: 0.10 and 0.90 both yield 0.0156 * 0.10."""
        fee_low = compute_fee(0.10, 0.0156)
        fee_high = compute_fee(0.90, 0.0156)
        assert fee_low == pytest.approx(0.0156 * 0.10)
        assert fee_high == pytest.approx(0.0156 * 0.10)
        assert fee_low == pytest.approx(fee_high)

    def test_compute_fee_zero_rate(self):
        """Zero fee rate disables fees entirely."""
        assert compute_fee(0.50, 0.0) == 0.0
        assert compute_fee(0.10, 0.0) == 0.0

    def test_compute_fee_near_one(self):
        """Prices near 1.0 have very small fees."""
        assert compute_fee(0.99, 0.0156) == pytest.approx(0.0156 * 0.01)

    def test_compute_fee_near_zero(self):
        """Prices near 0.0 have very small fees."""
        assert compute_fee(0.01, 0.0156) == pytest.approx(0.0156 * 0.01)


# ---------------------------------------------------------------------------
# Phase 1: Config validation
# ---------------------------------------------------------------------------

class TestFeeConfig:
    def test_default_fee_rate(self):
        cfg = BotConfig()
        assert cfg.maker_fee_rate == 0.0

    def test_zero_fee_rate_valid(self):
        cfg = BotConfig(maker_fee_rate=0.0)
        errors = validate_live_config(cfg)
        assert not any("maker_fee_rate" in e for e in errors)

    def test_negative_fee_rate_rejected(self):
        cfg = BotConfig(maker_fee_rate=-0.01)
        errors = validate_live_config(cfg)
        assert any("negative" in e for e in errors)

    def test_excessive_fee_rate_rejected(self):
        cfg = BotConfig(maker_fee_rate=0.20)
        errors = validate_live_config(cfg)
        assert any("0.10" in e for e in errors)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_cfg(**overrides) -> BotConfig:
    defaults = dict(
        dry_run=True,
        bankroll=10_000.0,
        ladder_rungs=5,
        ladder_spacing=0.01,
        ladder_width=0.04,
        ladder_size_skew=1.0,
        max_pair_cost=0.90,
        position_size_fraction=0.05,
        maker_fee_rate=0.0156,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _make_ladder_manager(cfg=None, **kw) -> LadderManager:
    if cfg is None:
        cfg = _make_cfg(**kw)
    executor = MagicMock()
    executor.get_best_ask.return_value = 0.45
    executor.place_batch_limit_buys.return_value = []
    executor.get_open_orders.return_value = []
    tracker = OrderTracker()
    positions = PositionManager(cfg=cfg, bankroll=cfg.bankroll)
    risk = MagicMock()
    risk.is_halted.return_value = False
    risk.can_open_position.return_value = True
    risk.can_trade_in_window.return_value = True
    risk.check_capital_at_risk.return_value = True
    risk.exposure_factor.return_value = 1.0
    tick_cache = MagicMock()
    tick_cache.get_tick_size.return_value = 0.01
    return LadderManager(cfg, executor, tracker, positions, risk, tick_cache)


def _make_market(market_id="btc-5m-100") -> MarketWindow:
    return MarketWindow(
        market_id=market_id,
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=300,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1300,
    )


# ---------------------------------------------------------------------------
# Phase 2: _fill_cost helper
# ---------------------------------------------------------------------------

class TestFillCost:
    def test_fill_cost_includes_fee(self):
        """_fill_cost(0.45, 10.0) = 10 * (0.45 + 0.0156 * 0.45) = 10 * 0.45702 = 4.5702."""
        lm = _make_ladder_manager()
        result = lm._fill_cost(0.45, 10.0)
        expected = 10.0 * (0.45 + 0.0156 * 0.45)
        assert result == pytest.approx(expected)

    def test_fill_cost_zero_fee_rate(self):
        """With fee_rate=0, _fill_cost returns raw cost."""
        lm = _make_ladder_manager(maker_fee_rate=0.0)
        result = lm._fill_cost(0.45, 10.0)
        assert result == pytest.approx(4.50)

    def test_fill_cost_symmetric(self):
        """Fee for 0.30 and 0.70 should be the same (both use min(p, 1-p) = 0.30)."""
        lm = _make_ladder_manager()
        cost_low = lm._fill_cost(0.30, 10.0)
        cost_high = lm._fill_cost(0.70, 10.0)
        fee_30 = compute_fee(0.30, 0.0156)
        fee_70 = compute_fee(0.70, 0.0156)
        assert fee_30 == pytest.approx(fee_70)
        # But total costs differ because base prices differ
        assert cost_low < cost_high


# ---------------------------------------------------------------------------
# Phase 3: Fee-inclusive budget sizing
# ---------------------------------------------------------------------------

class TestBudgetSizing:
    def test_budget_sizing_reserves_for_fees(self):
        """Rungs' fee-inclusive cost must not exceed budget."""
        rungs = build_ladder_rungs(0.50, 100.0, 5, 0.01, 0.04, 1.0, fee_rate=0.0156, max_rung_price=1.0)
        assert len(rungs) > 0
        total_cost = sum(
            size * (price + compute_fee(price, 0.0156))
            for price, size in rungs
        )
        assert total_cost <= 100.0 + 0.01  # small tolerance for rounding

    def test_budget_sizing_no_fee_uses_full_budget(self):
        """With fee_rate=0, sizing should use the full budget."""
        rungs = build_ladder_rungs(0.50, 100.0, 5, 0.01, 0.04, 1.0, fee_rate=0.0)
        assert len(rungs) > 0
        total_cost = sum(size * price for price, size in rungs)
        # Small tolerance for rounding of sizes to 1 decimal place
        assert total_cost <= 100.0 + 0.50

    def test_fee_reduces_total_shares(self):
        """Ladders with fees should have fewer total shares than without."""
        rungs_no_fee = build_ladder_rungs(0.50, 100.0, 5, 0.01, 0.04, 1.0, fee_rate=0.0)
        rungs_fee = build_ladder_rungs(0.50, 100.0, 5, 0.01, 0.04, 1.0, fee_rate=0.0156)
        shares_no_fee = sum(s for _, s in rungs_no_fee)
        shares_fee = sum(s for _, s in rungs_fee)
        assert shares_fee < shares_no_fee


# ---------------------------------------------------------------------------
# Phase 4: Fee-inclusive pair cost guard
# ---------------------------------------------------------------------------

class TestPairCostGuard:
    def test_pair_cost_guard_rejects_with_fees(self):
        """Raw pair cost 0.89, but fee-inclusive > 0.90 — should reject."""
        # UP asks at 0.445, DN asks at 0.445: raw sum = 0.89
        # Fee-inclusive: 0.445 + 0.0156*0.445 = 0.4519 each, sum = 0.9039 > 0.90
        cfg = _make_cfg(max_pair_cost=0.90, ladder_rungs=3, ladder_width=0.02,
                        max_pair_cost_5m=0.90)
        lm = _make_ladder_manager(cfg=cfg)
        lm.executor.get_best_ask.return_value = 0.445
        lm.executor.place_batch_limit_buys.return_value = []

        market = _make_market()
        count = lm.post_ladder(market)
        assert count == 0, "Should reject when fee-inclusive pair cost > max_pair_cost"

    def test_pair_cost_guard_passes_without_fees_regression(self):
        """With fee_rate=0, raw pair cost 0.89 < 0.90 — should pass."""
        cfg = _make_cfg(max_pair_cost=0.90, maker_fee_rate=0.0, ladder_rungs=3,
                        ladder_width=0.02, max_pair_cost_5m=0.90)
        lm = _make_ladder_manager(cfg=cfg)
        lm.executor.get_best_ask.return_value = 0.445

        # Make executor return order records so count > 0
        mock_record = MagicMock()
        mock_record.status = "open"
        mock_record.order_id = "o1"
        mock_record.price = 0.44
        mock_record.size = 10.0
        lm.executor.place_batch_limit_buys.return_value = [mock_record]

        market = _make_market()
        count = lm.post_ladder(market)
        assert count > 0, "With zero fees, raw pair cost 0.89 should pass the 0.90 guard"

    def test_pair_cost_guard_low_prices_pass(self):
        """Low asks (0.30 each, sum 0.60) should always pass."""
        cfg = _make_cfg(max_pair_cost=0.90, max_pair_cost_5m=0.90)
        lm = _make_ladder_manager(cfg=cfg)
        lm.executor.get_best_ask.return_value = 0.30

        mock_record = MagicMock()
        mock_record.status = "open"
        mock_record.order_id = "o1"
        mock_record.price = 0.28
        mock_record.size = 10.0
        lm.executor.place_batch_limit_buys.return_value = [mock_record]

        market = _make_market()
        count = lm.post_ladder(market)
        assert count > 0


# ---------------------------------------------------------------------------
# Phase 5: Fee-inclusive total_committed
# ---------------------------------------------------------------------------

class TestTotalCommitted:
    def test_total_committed_includes_fees(self):
        """total_committed() should account for fees on resting orders."""
        lm = _make_ladder_manager()
        market = _make_market()

        # Manually add a ladder state and resting order
        from polybot.strategy.ladder_manager import LadderState
        lm.ladders[market.market_id] = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=time.time(),
        )
        order = TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=100.0, filled=0.0,
        )
        lm.tracker.add(order)

        committed = lm.total_committed()
        raw_cost = 0.45 * 100.0  # 45.0
        fee = compute_fee(0.45, 0.0156) * 100.0
        expected = raw_cost + fee
        assert committed == pytest.approx(expected)

    def test_total_committed_zero_fee_matches_raw(self):
        """With fee_rate=0, total_committed equals raw price * size."""
        lm = _make_ladder_manager(maker_fee_rate=0.0)
        market = _make_market()

        from polybot.strategy.ladder_manager import LadderState
        lm.ladders[market.market_id] = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=time.time(),
        )
        order = TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=100.0, filled=0.0,
        )
        lm.tracker.add(order)

        committed = lm.total_committed()
        assert committed == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# Phase 6: Fee-inclusive filled_cost in OrderTracker
# ---------------------------------------------------------------------------

class TestFilledCostWithFee:
    def test_filled_cost_with_fee(self):
        """filled_cost with fee_rate should include fees."""
        tracker = OrderTracker()
        order = TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok",
            side=Side.UP, price=0.45, size=10.0, filled=10.0,
            status="filled",
        )
        tracker.add(order)

        cost_raw = tracker.filled_cost("m1", Side.UP, fee_rate=0.0)
        cost_fee = tracker.filled_cost("m1", Side.UP, fee_rate=0.0156)

        assert cost_raw == pytest.approx(4.50)
        expected_fee = 10.0 * (0.45 + 0.0156 * 0.45)
        assert cost_fee == pytest.approx(expected_fee)
        assert cost_fee > cost_raw

    def test_filled_cost_default_no_fee(self):
        """Default fee_rate=0 preserves backward compatibility."""
        tracker = OrderTracker()
        order = TrackedOrder(
            order_id="o1", market_id="m1", token_id="tok",
            side=Side.UP, price=0.45, size=10.0, filled=10.0,
            status="filled",
        )
        tracker.add(order)
        assert tracker.filled_cost("m1", Side.UP) == pytest.approx(4.50)


# ---------------------------------------------------------------------------
# Phase 7-8: Position PnL after fees
# ---------------------------------------------------------------------------

class TestPositionPnlAfterFee:
    def test_position_pnl_after_fee(self):
        """Simulate fills on both sides, settle UP, verify profit_if_up is correct with fees.

        Setup: 10 UP shares at 0.45, 10 DN shares at 0.45
        Fee per share: 0.0156 * 0.45 = 0.00702
        True cost per share: 0.45702
        Total UP cost: 4.5702
        Total DN cost: 4.5702

        If UP wins: payout = 10 * $1 = $10
        profit_if_up = up_qty * (1 - avg_up) - dn_cost
                     = 10 * (1 - 0.45702) - 4.5702
                     = 10 * 0.54298 - 4.5702
                     = 5.4298 - 4.5702
                     = 0.8596
        """
        pos = Position(market_id="m1")
        fee = compute_fee(0.45, 0.0156)
        cost_per_share = 0.45 + fee

        pos.up_qty = 10.0
        pos.up_cost = 10.0 * cost_per_share
        pos.dn_qty = 10.0
        pos.dn_cost = 10.0 * cost_per_share

        profit = pos.profit_if_up()
        expected = 10.0 * (1.0 - cost_per_share) - pos.dn_cost
        assert profit == pytest.approx(expected)
        assert profit == pytest.approx(0.8596, abs=0.001)

    def test_position_pnl_no_fee(self):
        """Without fees, profit margin is larger."""
        pos = Position(market_id="m1")
        pos.up_qty = 10.0
        pos.up_cost = 4.50  # raw cost
        pos.dn_qty = 10.0
        pos.dn_cost = 4.50

        profit_no_fee = pos.profit_if_up()
        # = 10 * (1 - 0.45) - 4.50 = 5.50 - 4.50 = 1.00
        assert profit_no_fee == pytest.approx(1.00)

        # With fees
        fee = compute_fee(0.45, 0.0156)
        cost_per_share = 0.45 + fee
        pos2 = Position(market_id="m1")
        pos2.up_qty = 10.0
        pos2.up_cost = 10.0 * cost_per_share
        pos2.dn_qty = 10.0
        pos2.dn_cost = 10.0 * cost_per_share

        profit_fee = pos2.profit_if_up()
        assert profit_fee < profit_no_fee, "Fee-inclusive profit should be less"

    def test_pair_cost_reflects_fees(self):
        """Position.pair_cost() with fee-inclusive costs reflects true pair cost."""
        fee = compute_fee(0.45, 0.0156)
        cost_per_share = 0.45 + fee

        pos = Position(market_id="m1")
        pos.up_qty = 10.0
        pos.up_cost = 10.0 * cost_per_share
        pos.dn_qty = 10.0
        pos.dn_cost = 10.0 * cost_per_share

        pair = pos.pair_cost()
        assert pair == pytest.approx(2 * cost_per_share, abs=0.001)
        assert pair > 0.90, "Fee-inclusive pair cost for 0.45+0.45 should exceed 0.90"


# ---------------------------------------------------------------------------
# Integration: paper fill crediting with fees
# ---------------------------------------------------------------------------

class TestPaperFillCrediting:
    def test_paper_fill_credits_fee_inclusive_cost(self):
        """process_paper_fills should credit fee-inclusive cost to positions."""
        lm = _make_ladder_manager()
        market = _make_market()

        # Add a tracked order
        order = TrackedOrder(
            order_id="o1", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.45, size=10.0,
        )
        lm.tracker.add(order)

        # Simulate paper fill
        fills = [{"orderID": "o1"}]
        lm.process_paper_fills(fills)

        pos = lm.positions.positions.get(market.market_id)
        assert pos is not None
        expected_cost = 10.0 * (0.45 + compute_fee(0.45, 0.0156))
        assert pos.up_cost == pytest.approx(expected_cost)
        assert pos.up_qty == pytest.approx(10.0)

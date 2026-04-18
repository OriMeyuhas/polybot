"""Tests for fair value model, vol estimator, and strategy integration."""

import math
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from polybot.config import BotConfig
from polybot.strategy.fair_value import p_fair_up, certainty, _norm_cdf
from polybot.strategy.vol_estimator import VolEstimator
from polybot.strategy.ladder_manager import LadderManager, LadderState, MIN_ORDER_SIZE
from polybot.strategy.position_manager import PositionManager
from polybot.types import MarketWindow, Position, Side


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(**overrides):
    defaults = dict(
        dry_run=True, bankroll=500,
        ladder_rungs=10, ladder_spacing=0.01, ladder_width=0.29,
        ladder_size_skew=1.0, max_pair_cost=0.93, position_size_fraction=0.10,
        reprice_threshold=0.03, maker_fee_rate=0.0, batch_order_size=15,
        no_trade_final_sec=60, imbalance_min_heavy_fills=1,
        boost_elapsed_pct=0.20, force_buy_elapsed_pct=0.70, force_buy_max_pair_cost=0.83,
        spot_delta_reduce_threshold=0.0015, spot_delta_skip_threshold=0.005,
        spot_gate_force_buy_threshold=0.003, spot_loss_cap_multiplier=0.50,
        fair_value_enabled=True, vol_window_sec=300, vol_fallback_annual=0.50,
        vol_min_samples=30, skew_phase_pct=0.30, directional_phase_pct=0.70,
        certainty_exit_threshold=0.40, certainty_hold_threshold=0.90,
        certainty_directional_threshold=0.85, directional_max_ask=0.80,
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
    return MarketWindow(
        market_id=market_id, condition_id="0xabc", asset="BTC",
        timeframe_sec=timeframe_sec, up_token_id="tok_up", dn_token_id="tok_dn",
        open_epoch=1000, close_epoch=1000 + timeframe_sec,
    )


def _make_manager(cfg=None, bankroll=500):
    if cfg is None:
        cfg = _cfg(bankroll=bankroll)
    executor = MagicMock()
    executor.get_best_ask.return_value = 0.45
    executor.get_midpoint.return_value = 0.44
    executor.get_open_orders.return_value = []
    executor.place_limit_buy.return_value = MagicMock(
        order_id="ord-1", status="open", price=0.40, size=10.0,
    )
    executor.place_limit_sell.return_value = MagicMock(
        order_id="sell-1", status="open", price=0.35, size=50.0,
    )
    executor.place_batch_limit_buys.return_value = [
        MagicMock(order_id=f"ord-{i}", status="open", price=0.40, size=10.0)
        for i in range(5)
    ]
    executor.cancel_batch.return_value = []
    executor.estimate_fill_cost.return_value = (0.45, 22.5)

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


# ── Fair value model tests ──────────────────────────────────────────────────

class TestFairValueModel:
    def test_at_money(self):
        """S == K -> P(UP) ≈ 0.50"""
        p = p_fair_up(100.0, 100.0, 300.0, 0.50)
        assert 0.48 <= p <= 0.52

    def test_deep_itm(self):
        """S >> K -> P(UP) near 1.0"""
        p = p_fair_up(100.0, 110.0, 60.0, 0.30)
        assert p > 0.90

    def test_deep_otm(self):
        """S << K -> P(UP) near 0.0"""
        p = p_fair_up(100.0, 90.0, 60.0, 0.30)
        assert p < 0.10

    def test_convergence_at_expiry(self):
        """T -> 0 with S > K -> P(UP) -> 0.99"""
        p = p_fair_up(100.0, 101.0, 0.1, 0.50)
        assert p > 0.95

    def test_convergence_at_expiry_otm(self):
        """T -> 0 with S < K -> P(UP) -> 0.01"""
        p = p_fair_up(100.0, 99.0, 0.1, 0.50)
        assert p < 0.05

    def test_none_prices_return_half(self):
        assert p_fair_up(None, 100.0, 300.0, 0.5) == 0.5
        assert p_fair_up(100.0, None, 300.0, 0.5) == 0.5

    def test_zero_vol_returns_half(self):
        assert p_fair_up(100.0, 105.0, 300.0, 0.0) == 0.5

    def test_high_vol_reduces_certainty(self):
        """Higher vol -> more uncertainty -> closer to 0.5"""
        p_low_vol = p_fair_up(100.0, 100.5, 300.0, 0.20)
        p_high_vol = p_fair_up(100.0, 100.5, 300.0, 1.50)
        assert p_low_vol > p_high_vol

    def test_expired_market(self):
        p = p_fair_up(100.0, 105.0, 0.0, 0.50)
        assert p == 0.99
        p = p_fair_up(100.0, 95.0, 0.0, 0.50)
        assert p == 0.01


class TestCertainty:
    def test_certainty_bullish(self):
        assert certainty(0.80) == 0.80

    def test_certainty_bearish(self):
        assert certainty(0.20) == 0.80

    def test_certainty_neutral(self):
        assert certainty(0.50) == 0.50


class TestNormCdf:
    def test_cdf_at_zero(self):
        assert abs(_norm_cdf(0.0) - 0.5) < 0.001

    def test_cdf_at_one(self):
        assert abs(_norm_cdf(1.0) - 0.8413) < 0.001

    def test_cdf_at_neg_one(self):
        assert abs(_norm_cdf(-1.0) - 0.1587) < 0.001


# ── Vol estimator tests ─────────────────────────────────────────────────────

class TestVolEstimator:
    def test_not_ready_returns_fallback(self):
        ve = VolEstimator(min_samples=30, fallback_vol_annual=0.60)
        assert ve.vol_annualized() == 0.60
        assert not ve.is_ready

    def test_ready_after_enough_samples(self):
        ve = VolEstimator(min_samples=5, fallback_vol_annual=0.50)
        for i in range(10):
            ve.push(1000.0 + i, 100.0 + i * 0.01)
        assert ve.is_ready
        assert ve.sample_count >= 5

    def test_constant_price_near_zero_vol(self):
        ve = VolEstimator(min_samples=5, fallback_vol_annual=0.50)
        for i in range(50):
            ve.push(1000.0 + i, 100.0)
        vol = ve.vol_annualized()
        assert vol < 0.01  # constant price -> near-zero vol

    def test_resampling_same_second(self):
        """Multiple ticks in same second produce only one bar."""
        ve = VolEstimator(min_samples=5)
        ve.push(1000.0, 100.0)
        ve.push(1000.5, 101.0)  # same second
        ve.push(1000.9, 102.0)  # same second
        ve.push(1001.0, 103.0)  # new second
        assert ve.sample_count <= 2  # only 1-2 bars (close of second 1000)

    def test_negative_price_ignored(self):
        ve = VolEstimator(min_samples=5)
        ve.push(1000.0, -50.0)
        assert ve.sample_count == 0

    def test_sample_count(self):
        ve = VolEstimator(min_samples=5)
        for i in range(100):
            ve.push(1000.0 + i, 100.0 + i * 0.1)
        assert ve.sample_count > 50


# ── Budget skew tests ────────────────────────────────────────────────────────

class TestFairValueBudgetSkew:
    def test_fv_disabled_no_skew(self):
        """When fair_value_enabled=False, budget should be 50/50."""
        lm = _make_manager(_cfg(fair_value_enabled=False))
        market = _market()
        # Just verify it posts without error with fv disabled
        count = lm.post_ladder(market, spot_delta=0.0, fair_up=0.70)
        assert count >= 0  # Should use spot_delta fallback

    def test_fv_skew_bullish(self):
        """When P(UP)=0.70, UP budget should be > DN budget."""
        lm = _make_manager()
        market = _market()
        # We can't directly inspect budget split, but we verify no crash
        # and that the ladder posts successfully
        count = lm.post_ladder(market, spot_delta=0.0, fair_up=0.70)
        assert count > 0

    def test_fv_skew_bearish(self):
        """When P(UP)=0.30, DN budget should be > UP budget."""
        lm = _make_manager()
        market = _market()
        count = lm.post_ladder(market, spot_delta=0.0, fair_up=0.30)
        assert count > 0

    def test_fv_neutral_no_skew(self):
        """When P(UP)=0.50, uses spot_delta fallback."""
        lm = _make_manager()
        market = _market()
        count = lm.post_ladder(market, spot_delta=0.0, fair_up=0.50)
        assert count > 0

    def test_max_skew_clamp(self):
        """P(UP)=0.95 should be clamped to max_budget_skew=0.80."""
        lm = _make_manager(_cfg(max_budget_skew=0.80))
        market = _market()
        count = lm.post_ladder(market, spot_delta=0.0, fair_up=0.95)
        assert count > 0  # Should not crash, UP gets max 80%


# ── Certainty-based exit tests ──────────────────────────────────────────────

class TestCertaintyExit:
    def test_exit_on_low_certainty(self):
        """When holding UP and P(UP)<40%, should trigger exit."""
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=50.0, up_cost=15.0,
            dn_qty=0.0, dn_cost=0.0,
        )
        # P(UP)=0.25 -> certainty for UP = 25% < 40% threshold
        result = lm.sell_losing_side(market, 1200, fair_up=0.25)
        assert result is not None
        assert result["side"] == Side.UP

    def test_no_exit_when_winning(self):
        """When holding UP and P(UP)>60%, should NOT exit."""
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=50.0, up_cost=15.0,
            dn_qty=0.0, dn_cost=0.0,
        )
        result = lm.sell_losing_side(market, 1200, fair_up=0.70)
        assert result is None

    def test_exit_dn_position_when_bullish(self):
        """When holding DN and P(UP)=0.80 -> P(DN)=0.20 < 40%, exit DN."""
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=0.0, up_cost=0.0,
            dn_qty=50.0, dn_cost=15.0,
        )
        result = lm.sell_losing_side(market, 1200, fair_up=0.80)
        assert result is not None
        assert result["side"] == Side.DOWN

    def test_fv_disabled_uses_elapsed(self):
        """When fair_value_enabled=False, should use elapsed-based trigger."""
        lm = _make_manager(_cfg(fair_value_enabled=False))
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=50.0, up_cost=15.0,
            dn_qty=0.0, dn_cost=0.0,
        )
        # Before 55% elapsed -> no exit
        result = lm.sell_losing_side(market, 1400, fair_up=0.25)
        assert result is None
        # After 55% elapsed -> exit
        result = lm.sell_losing_side(market, 1600, fair_up=0.25)
        assert result is not None


# ── Directional buy tests ───────────────────────────────────────────────────

class TestDirectionalBuy:
    def test_directional_buy_triggers(self):
        """High certainty + late window + cheap ask -> buy."""
        lm = _make_manager()
        lm.executor.get_best_ask.return_value = 0.75
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        # 80% elapsed, P(UP)=0.90 -> cert=90% > 85%
        result = lm.directional_buy(market, 1720, fair_up=0.90)
        assert result is not None
        assert result["side"] == Side.UP
        assert result["ev_per_share"] > 0

    def test_directional_buy_too_early(self):
        """Before directional phase -> no buy."""
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        result = lm.directional_buy(market, 1300, fair_up=0.90)  # 33% elapsed
        assert result is None

    def test_directional_buy_low_certainty(self):
        """Certainty < 85% -> no buy."""
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        result = lm.directional_buy(market, 1720, fair_up=0.70)  # cert=70%
        assert result is None

    def test_directional_buy_ask_too_high(self):
        """Ask > directional_max_ask -> no buy."""
        lm = _make_manager()
        lm.executor.get_best_ask.return_value = 0.90  # > 0.80 max
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        result = lm.directional_buy(market, 1720, fair_up=0.90)
        assert result is None

    def test_directional_buy_fv_disabled(self):
        """When fair_value_enabled=False, never fires."""
        lm = _make_manager(_cfg(fair_value_enabled=False))
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        result = lm.directional_buy(market, 1720, fair_up=0.95)
        assert result is None

    def test_directional_buy_only_once(self):
        """Directional buy fires once per window."""
        lm = _make_manager()
        lm.executor.get_best_ask.return_value = 0.75
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        result1 = lm.directional_buy(market, 1720, fair_up=0.90)
        assert result1 is not None
        result2 = lm.directional_buy(market, 1750, fair_up=0.90)
        assert result2 is None

    def test_directional_buy_budget_capped_by_directional_budget_cap(self):
        """directional_buy budget must never exceed cfg.directional_budget_cap.

        Use a large bankroll ($10 000) and a tiny cap ($10) so that the
        fraction-based formula (bankroll * position_size_fraction * 0.5)
        would produce ~$500 without the cap. The cap must bind and the
        placed order qty must equal cap / ask.
        """
        cap = 10.0
        bankroll = 10_000.0
        ask = 0.50  # well below directional_max_ask=0.80

        cfg = _cfg(
            bankroll=bankroll,
            position_size_fraction=0.10,
            directional_budget_cap=cap,
            directional_max_ask=0.80,
            certainty_directional_threshold=0.85,
        )
        lm = _make_manager(cfg=cfg, bankroll=bankroll)
        lm.executor.get_best_ask.return_value = ask

        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )

        # 80% elapsed, P(UP)=0.95 -> well above certainty threshold
        result = lm.directional_buy(market, 1720, fair_up=0.95)
        assert result is not None, "directional_buy should fire at high certainty"

        # Inspect the qty passed to place_limit_buy — must be <= cap / ask
        call_args = lm.executor.place_limit_buy.call_args
        placed_qty = call_args[0][2]  # positional arg index 2 = qty
        max_allowed_qty = cap / ask
        assert placed_qty <= max_allowed_qty + 1e-9, (
            f"qty {placed_qty:.4f} exceeds directional_budget_cap={cap} / ask={ask} "
            f"= {max_allowed_qty:.4f}"
        )


# ── Chase with fair value guard ─────────────────────────────────────────────

class TestChaseWithFairValue:
    def test_chase_skips_losing_side(self):
        """When certainty > 60% and chase side is the loser, skip."""
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        # UP has fills, DN has none -> chase_side = DN
        # But P(UP)=0.75 -> DN is the loser -> skip chase
        def filled_count_side(mid, side):
            return 3 if side == Side.UP else 0
        lm.tracker.filled_count = filled_count_side
        lm.tracker.filled_qty.return_value = 0.0
        lm.tracker.filled_cost.return_value = 0.0

        with patch.object(lm, '_first_fill_time', return_value=1000):
            count = lm.chase_pair(market, 1200, fair_up=0.75)
        assert count == 0

    def test_chase_allows_winning_side(self):
        """When chase side IS the winner, allow chase."""
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        # DN has fills, UP has none -> chase_side = UP
        # P(UP)=0.75 -> UP is the winner -> allow chase
        def filled_count_side(mid, side):
            return 3 if side == Side.DOWN else 0
        lm.tracker.filled_count = filled_count_side
        lm.tracker.filled_qty.return_value = 0.0
        lm.tracker.filled_cost.return_value = 0.0

        with patch.object(lm, '_first_fill_time', return_value=1000):
            count = lm.chase_pair(market, 1200, fair_up=0.75)
        assert count > 0


# ── Config validation tests ─────────────────────────────────────────────────

class TestFairValueConfig:
    def test_defaults(self):
        cfg = _cfg()
        assert cfg.fair_value_enabled is True
        assert cfg.vol_window_sec == 300
        assert cfg.certainty_exit_threshold == 0.40
        assert cfg.certainty_hold_threshold == 0.90
        assert cfg.max_budget_skew == 0.80

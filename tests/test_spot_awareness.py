"""Tests for spot-aware trading: budget skew, boost gating, force-buy gating, loss cap tightening.

Covers:
- Config defaults and validation for spot-awareness parameters
- post_ladder with spot_delta: 50/50, reduced, skipped
- boost_light_side with spot gate
- try_complete_pair with spot gate
- check_loss_cap with window_open_prices tightening
"""

import logging
import pytest
from unittest.mock import MagicMock

from polybot.config import BotConfig, load_bot_config, validate_live_config
from polybot.order_executor import OrderExecutor
from polybot.strategy.ladder_manager import (
    LadderManager, LadderState, build_ladder_rungs, MIN_ORDER_SIZE,
)
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
        # Spot-awareness defaults
        spot_delta_reduce_threshold=0.0015,
        spot_delta_skip_threshold=0.005,
        spot_gate_force_buy_threshold=0.003,
        spot_loss_cap_multiplier=0.50,
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
        oid = f"fill-{side.value}-{i}"
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


# ─── Config defaults ──────────────────────────────────────────────────────


class TestSpotConfigDefaults:
    def test_config_defaults_exist(self):
        cfg = BotConfig()
        assert cfg.spot_delta_reduce_threshold == 0.0015
        assert cfg.spot_delta_skip_threshold == 0.005
        assert cfg.spot_gate_force_buy_threshold == 0.003
        assert cfg.spot_loss_cap_multiplier == 0.50

    def test_config_env_var_loading(self, monkeypatch):
        monkeypatch.setenv("SPOT_DELTA_REDUCE_THRESHOLD", "0.002")
        monkeypatch.setenv("SPOT_DELTA_SKIP_THRESHOLD", "0.01")
        monkeypatch.setenv("SPOT_GATE_FORCE_BUY_THRESHOLD", "0.004")
        monkeypatch.setenv("SPOT_LOSS_CAP_MULTIPLIER", "0.60")
        cfg = load_bot_config()
        assert cfg.spot_delta_reduce_threshold == 0.002
        assert cfg.spot_delta_skip_threshold == 0.01
        assert cfg.spot_gate_force_buy_threshold == 0.004
        assert cfg.spot_loss_cap_multiplier == 0.60

    def test_validation_reduce_must_be_positive(self):
        cfg = BotConfig(spot_delta_reduce_threshold=0.0)
        errors = validate_live_config(cfg)
        assert any("spot_delta_reduce_threshold" in e for e in errors)

    def test_validation_reduce_less_than_skip(self):
        cfg = BotConfig(spot_delta_reduce_threshold=0.01, spot_delta_skip_threshold=0.005)
        errors = validate_live_config(cfg)
        assert any("spot_delta_reduce_threshold" in e for e in errors)

    def test_validation_loss_cap_multiplier_bounds(self):
        cfg = BotConfig(spot_loss_cap_multiplier=0.0)
        errors = validate_live_config(cfg)
        assert any("spot_loss_cap_multiplier" in e for e in errors)

        cfg = BotConfig(spot_loss_cap_multiplier=1.5)
        errors = validate_live_config(cfg)
        assert any("spot_loss_cap_multiplier" in e for e in errors)

    def test_validation_loss_cap_multiplier_one_is_valid(self):
        cfg = BotConfig(spot_loss_cap_multiplier=1.0)
        errors = validate_live_config(cfg)
        spot_errors = [e for e in errors if "spot_loss_cap_multiplier" in e]
        assert spot_errors == []


# ─── post_ladder with spot_delta ──────────────────────────────────────────


class TestPostLadderSpotDelta:
    def test_delta_zero_gives_5050_split(self, cfg, mock_clob, market, caplog):
        """With delta=0, budget should be split 50/50."""
        mgr = _make_manager(cfg, mock_clob)
        with caplog.at_level(logging.INFO):
            count = mgr.post_ladder(market, spot_delta=0.0)
        assert count > 0
        # No SPOT SKIP or SPOT SKEW log
        assert "SPOT SKIP" not in caplog.text
        assert "SPOT SKEW" not in caplog.text
        # Verify equal budget split via log
        assert "LADDER POSTED" in caplog.text

    def test_delta_reduce_skews_budget(self, cfg, mock_clob, market, caplog):
        """With delta=0.002 (above reduce, below skip), budget should be skewed."""
        mgr = _make_manager(cfg, mock_clob)
        with caplog.at_level(logging.INFO):
            count = mgr.post_ladder(market, spot_delta=0.002)
        assert count > 0
        assert "SPOT SKEW" in caplog.text

    def test_delta_skip_eliminates_losing_side(self, cfg, mock_clob, market, caplog):
        """With delta=0.006 (above skip), losing side should be entirely skipped."""
        mgr = _make_manager(cfg, mock_clob)
        with caplog.at_level(logging.INFO):
            count = mgr.post_ladder(market, spot_delta=0.006)
        assert count > 0
        # DN side skipped when spot is up
        assert "SPOT SKIP" in caplog.text
        assert "DN" in caplog.text

    def test_negative_delta_skips_up_side(self, cfg, mock_clob, market, caplog):
        """With delta=-0.006 (BTC down), UP side should be skipped."""
        mgr = _make_manager(cfg, mock_clob)
        with caplog.at_level(logging.INFO):
            count = mgr.post_ladder(market, spot_delta=-0.006)
        assert count > 0
        assert "SPOT SKIP" in caplog.text
        assert "UP" in caplog.text

    def test_pre_open_passes_spot_delta(self, cfg, mock_clob, market, caplog):
        """post_ladder_pre_open should pass spot_delta through to post_ladder."""
        mgr = _make_manager(cfg, mock_clob)
        with caplog.at_level(logging.INFO):
            count = mgr.post_ladder_pre_open(market, spot_delta=0.006)
        assert count > 0
        assert "SPOT SKIP" in caplog.text


# ─── boost_light_side with spot gate ──────────────────────────────────────


class TestBoostSpotGate:
    def _setup_boost_conditions(self, mgr, market):
        """Set up all conditions for boost to fire."""
        mgr.post_ladder(market)
        # Add 3+ fills on UP side, 0 on DN
        _add_filled_orders(mgr.tracker, market.market_id, Side.UP, "tok_up", 4)

    def test_boost_fires_when_spot_toward_heavy(self, cfg, mock_clob, market, caplog):
        """Boost should fire when spot moves toward heavy side (UP heavy, delta > 0)."""
        mgr = _make_manager(cfg, mock_clob)
        self._setup_boost_conditions(mgr, market)
        now = market.open_epoch + 200  # 22% elapsed
        with caplog.at_level(logging.INFO):
            result = mgr.boost_light_side(market, now, spot_delta=0.002)
        assert result > 0
        assert "SPOT GATE BOOST" not in caplog.text

    def test_boost_blocked_when_spot_away_from_heavy(self, cfg, mock_clob, market, caplog):
        """Boost should return 0 when spot moves away from heavy side."""
        mgr = _make_manager(cfg, mock_clob)
        self._setup_boost_conditions(mgr, market)
        now = market.open_epoch + 200  # 22% elapsed
        with caplog.at_level(logging.INFO):
            # Heavy is UP, but spot is negative (away from heavy)
            result = mgr.boost_light_side(market, now, spot_delta=-0.003)
        assert result == 0
        assert "SPOT GATE BOOST" in caplog.text

    def test_boost_fires_with_zero_delta(self, cfg, mock_clob, market):
        """Boost should fire normally when spot_delta is 0 (below threshold)."""
        mgr = _make_manager(cfg, mock_clob)
        self._setup_boost_conditions(mgr, market)
        now = market.open_epoch + 200
        result = mgr.boost_light_side(market, now, spot_delta=0.0)
        assert result > 0


# ─── try_complete_pair with spot gate ─────────────────────────────────────


class TestTryCompletePairSpotGate:
    def _setup_force_buy_conditions(self, mgr, market):
        """Set up conditions for force-buy to fire."""
        mgr.post_ladder(market)
        # Add 4 fills on UP side (heavy), 0 on DN side
        _add_filled_orders(mgr.tracker, market.market_id, Side.UP, "tok_up", 4)
        # Credit to position manager
        mgr.positions.update_position(market.market_id, Side.UP, 40.0, 18.0)  # 40 shares @ 0.45

    def test_force_buy_proceeds_with_neutral_spot(self, cfg, mock_clob, market):
        """Force-buy should proceed when spot delta is near zero."""
        mgr = _make_manager(cfg, mock_clob)
        self._setup_force_buy_conditions(mgr, market)
        now = market.open_epoch + 700  # 78% elapsed
        result = mgr.try_complete_pair(market, now, spot_delta=0.0)
        assert result is not None
        assert "side" in result

    def test_force_buy_blocked_when_spot_against_heavy(self, cfg, mock_clob, market, caplog):
        """Force-buy should return None when spot moves against heavy side."""
        mgr = _make_manager(cfg, mock_clob)
        self._setup_force_buy_conditions(mgr, market)
        now = market.open_epoch + 700  # 78% elapsed
        with caplog.at_level(logging.INFO):
            # Heavy is UP, but spot is strongly negative (against UP)
            result = mgr.try_complete_pair(market, now, spot_delta=-0.004)
        assert result is None
        assert "SPOT GATE FORCE-BUY" in caplog.text

    def test_force_buy_proceeds_when_spot_with_heavy(self, cfg, mock_clob, market):
        """Force-buy should proceed when spot moves with heavy side."""
        mgr = _make_manager(cfg, mock_clob)
        self._setup_force_buy_conditions(mgr, market)
        now = market.open_epoch + 700
        # Heavy is UP, spot is positive (with heavy)
        result = mgr.try_complete_pair(market, now, spot_delta=0.004)
        assert result is not None

    def test_force_buy_proceeds_below_gate_threshold(self, cfg, mock_clob, market):
        """Force-buy should proceed when spot delta is below gate threshold."""
        mgr = _make_manager(cfg, mock_clob)
        self._setup_force_buy_conditions(mgr, market)
        now = market.open_epoch + 700
        # Heavy is UP, delta is negative but below threshold
        result = mgr.try_complete_pair(market, now, spot_delta=-0.002)
        assert result is not None


# ─── check_loss_cap with window_open_prices ───────────────────────────────


class TestCheckLossCapSpotAware:
    def test_loss_cap_tightened_when_spot_against(self, cfg, mock_clob, market):
        """Loss cap should be tighter when spot confirms losing direction."""
        # Use bankroll=100 so max_loss = max(5, 100*0.05) = $5
        # With multiplier 0.50, effective = $2.50
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)

        # Add one-sided UP fills: 1 fill at $3.50 cost (between $2.50 and $5)
        order = TrackedOrder(
            order_id="fill-up-0",
            market_id=market.market_id,
            token_id="tok_up",
            side=Side.UP,
            price=0.35,
            size=10.0,
            filled=10.0,
            status="filled",
            placed_at=1000.0,
            credited_to_pm=10.0,
        )
        mgr.tracker.add(order)

        # Without window_open_prices: cost $3.50 < max_loss $5 => NOT killed
        result = mgr.check_loss_cap({"BTC": 100.0})
        assert market.market_id not in result

        # Re-create since we need to test tightened cap
        mgr2 = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr2.post_ladder(market)
        order2 = TrackedOrder(
            order_id="fill-up-1",
            market_id=market.market_id,
            token_id="tok_up",
            side=Side.UP,
            price=0.35,
            size=10.0,
            filled=10.0,
            status="filled",
            placed_at=1000.0,
            credited_to_pm=10.0,
        )
        mgr2.tracker.add(order2)

        # With window_open_prices: BTC dropped (delta negative), UP position is losing
        # spot_delta = (99 - 100)/100 = -0.01 which is < -0.0015 => spot_against for UP
        # effective_max_loss = 5 * 0.50 = 2.50, cost = 3.50 > 2.50 => KILLED
        result2 = mgr2.check_loss_cap(
            {"BTC": 99.0},
            window_open_prices={market.market_id: 100.0},
        )
        assert market.market_id in result2

    def test_loss_cap_not_tightened_when_spot_with_position(self, cfg, mock_clob, market):
        """Loss cap should NOT be tightened when spot moves with the position."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)

        order = TrackedOrder(
            order_id="fill-up-0",
            market_id=market.market_id,
            token_id="tok_up",
            side=Side.UP,
            price=0.35,
            size=10.0,
            filled=10.0,
            status="filled",
            placed_at=1000.0,
            credited_to_pm=10.0,
        )
        mgr.tracker.add(order)

        # BTC is UP => UP position is winning => no tightening
        # cost $3.50 < max_loss $5 => NOT killed
        result = mgr.check_loss_cap(
            {"BTC": 101.0},
            window_open_prices={market.market_id: 100.0},
        )
        assert market.market_id not in result

    def test_loss_cap_tightened_for_dn_position_when_spot_up(self, cfg, mock_clob, market):
        """Loss cap should be tightened for DN-only position when spot goes up."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)

        order = TrackedOrder(
            order_id="fill-dn-0",
            market_id=market.market_id,
            token_id="tok_dn",
            side=Side.DOWN,
            price=0.35,
            size=10.0,
            filled=10.0,
            status="filled",
            placed_at=1000.0,
            credited_to_pm=10.0,
        )
        mgr.tracker.add(order)

        # BTC is up (101 vs 100) => DN position is losing
        # delta = (101-100)/100 = 0.01 > 0.0015, dn_qty > 0 and up_qty == 0
        # effective_max_loss = 5 * 0.50 = 2.50, cost = 3.50 > 2.50 => KILLED
        result = mgr.check_loss_cap(
            {"BTC": 101.0},
            window_open_prices={market.market_id: 100.0},
        )
        assert market.market_id in result

    def test_loss_cap_without_open_prices_uses_normal_cap(self, cfg, mock_clob, market):
        """Without window_open_prices, loss cap should use normal max_loss."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)

        # cost = 3.50 < normal max_loss 5.0 => NOT killed
        order = TrackedOrder(
            order_id="fill-up-0",
            market_id=market.market_id,
            token_id="tok_up",
            side=Side.UP,
            price=0.35,
            size=10.0,
            filled=10.0,
            status="filled",
            placed_at=1000.0,
            credited_to_pm=10.0,
        )
        mgr.tracker.add(order)

        result = mgr.check_loss_cap({"BTC": 99.0})  # no window_open_prices
        assert market.market_id not in result

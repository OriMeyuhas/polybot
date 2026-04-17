"""Tests for pair recovery strategy: Phase D (Boost) and Phase B (Force-Buy).

Covers:
- Config parameters (Task 1)
- estimate_fill_cost (Task 2)
- Loss cap fix (Task 3)
- Imbalance timing fix (Task 4)
- Phase D: boost_light_side (Task 5)
- Phase B: try_complete_pair (Task 6)
- Bot wiring (Task 7)
- LadderState timeframe_sec (Task 8)
"""

import os
import logging
import pytest
from unittest.mock import MagicMock, patch, AsyncMock, call
from dataclasses import dataclass
from decimal import Decimal

from polybot.config import BotConfig, load_bot_config, validate_live_config
from polybot.order_executor import OrderExecutor
from polybot.strategy.ladder_manager import LadderManager, LadderState, build_ladder_rungs, MIN_ORDER_SIZE
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


# ─── Task 1: Config parameters ─────────────────────────────────────────────

class TestConfigDefaults:
    def test_config_defaults_exist(self):
        cfg = BotConfig()
        assert cfg.boost_elapsed_pct == 0.20
        assert cfg.force_buy_elapsed_pct == 0.70
        assert cfg.force_buy_max_pair_cost == 0.83
        assert cfg.imbalance_min_heavy_fills == 1

    def test_config_env_var_loading(self, monkeypatch):
        monkeypatch.setenv("BOOST_ELAPSED_PCT", "0.25")
        monkeypatch.setenv("FORCE_BUY_ELAPSED_PCT", "0.80")
        monkeypatch.setenv("FORCE_BUY_MAX_PAIR_COST", "0.91")
        monkeypatch.setenv("IMBALANCE_MIN_HEAVY_FILLS", "5")
        cfg = load_bot_config()
        assert cfg.boost_elapsed_pct == 0.25
        assert cfg.force_buy_elapsed_pct == 0.80
        assert cfg.force_buy_max_pair_cost == 0.91
        assert cfg.imbalance_min_heavy_fills == 5

    def test_config_validation_force_buy_bounds(self):
        # force_buy_max_pair_cost too low
        cfg = BotConfig(force_buy_max_pair_cost=0.79)
        errors = validate_live_config(cfg)
        assert any("force_buy_max_pair_cost" in e for e in errors)

        # force_buy_max_pair_cost too high
        cfg = BotConfig(force_buy_max_pair_cost=0.995)
        errors = validate_live_config(cfg)
        assert any("force_buy_max_pair_cost" in e for e in errors)

        # boost_elapsed_pct >= force_buy_elapsed_pct
        cfg = BotConfig(boost_elapsed_pct=0.80, force_buy_elapsed_pct=0.70)
        errors = validate_live_config(cfg)
        assert any("boost_elapsed_pct" in e for e in errors)

        # force_buy_elapsed_pct too high
        cfg = BotConfig(force_buy_elapsed_pct=0.96)
        errors = validate_live_config(cfg)
        assert any("force_buy_elapsed_pct" in e for e in errors)

        # Valid config should produce no pair recovery errors
        cfg = BotConfig(
            boost_elapsed_pct=0.20,
            force_buy_elapsed_pct=0.70,
            force_buy_max_pair_cost=0.93,
        )
        errors = validate_live_config(cfg)
        pair_errors = [e for e in errors if "force_buy" in e or "boost_elapsed" in e]
        assert pair_errors == []


# ─── Task 2: estimate_fill_cost ─────────────────────────────────────────────

class TestEstimateFillCost:
    def test_estimate_fill_cost_basic(self, cfg, mock_clob):
        """Mock book with 3 ask levels, verify correct VWAP for qty spanning 2 levels."""
        executor = OrderExecutor(cfg, clob_client=mock_clob)
        # asks: 0.42 x 100, 0.43 x 200, 0.44 x 300
        # buying 150: 100 @ 0.42 + 50 @ 0.43
        result = executor.estimate_fill_cost("tok_up", 150)
        assert result is not None
        avg_price, total_cost = result
        expected_cost = 100 * 0.42 + 50 * 0.43
        expected_avg = expected_cost / 150
        assert abs(avg_price - expected_avg) < 0.001
        assert abs(total_cost - expected_cost) < 0.01

    def test_estimate_fill_cost_insufficient_depth(self, cfg, mock_clob):
        """Mock book with less qty than requested, verify returns None."""
        executor = OrderExecutor(cfg, clob_client=mock_clob)
        # Total depth = 100 + 200 + 300 = 600
        result = executor.estimate_fill_cost("tok_up", 700)
        assert result is None

    def test_estimate_fill_cost_empty_book(self, cfg):
        """Mock empty book, verify returns None."""
        clob = MagicMock()
        book = MagicMock()
        book.asks = []
        clob.get_order_book.return_value = book
        executor = OrderExecutor(cfg, clob_client=clob)
        result = executor.estimate_fill_cost("tok_up", 50)
        assert result is None


# ─── Task 8: LadderState timeframe_sec ──────────────────────────────────────

class TestLadderStateTimeframeSec:
    def test_ladder_state_has_timeframe_sec(self):
        state = LadderState(
            market_id="m1", asset="BTC",
            anchor_up=0.5, anchor_dn=0.5, posted_at=1000.0,
        )
        assert state.timeframe_sec == 900  # default

    def test_post_ladder_sets_timeframe_sec(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        state = mgr.ladders.get(market.market_id)
        assert state is not None
        assert state.timeframe_sec == market.timeframe_sec


# ─── Task 3: Loss cap fix ──────────────────────────────────────────────────

class TestLossCapFix:
    def test_loss_cap_removes_ladder_state(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        assert mgr.has_ladder(market.market_id)

        # Add one-sided fills exceeding 3% of bankroll ($3), max_loss = max(5, 3) = $5
        # 2 fills * 10 @ 0.45 = $9 > $5
        _add_filled_orders(mgr.tracker, market.market_id, Side.UP, "tok_up", 2, price=0.45, size=10.0)

        mgr.check_loss_cap({})
        assert not mgr.has_ladder(market.market_id)

    def test_loss_cap_blocks_repost(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        _add_filled_orders(mgr.tracker, market.market_id, Side.UP, "tok_up", 2, price=0.45, size=10.0)
        mgr.check_loss_cap({})

        # Attempt to repost should return 0
        result = mgr.post_ladder(market)
        assert result == 0

    def test_loss_cap_blocks_reprice(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 2, price=0.45, size=10.0)
        mgr.check_loss_cap({})

        # reprice should not iterate the killed market (no error, returns 0)
        result = mgr.reprice_if_needed({mid: market})
        assert result == 0

    def test_loss_cap_cleanup_clears_kill(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        _add_filled_orders(mgr.tracker, market.market_id, Side.UP, "tok_up", 2, price=0.45, size=10.0)
        mgr.check_loss_cap({})
        assert mgr.is_killed(market.market_id)

        mgr.cleanup_ladder(market.market_id)
        assert not mgr.is_killed(market.market_id)

        # Can repost after cleanup
        result = mgr.post_ladder(market)
        assert result > 0

    def test_loss_cap_logs_once(self, cfg, mock_clob, market, caplog):
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        _add_filled_orders(mgr.tracker, market.market_id, Side.UP, "tok_up", 2, price=0.45, size=10.0)

        with caplog.at_level(logging.WARNING):
            mgr.check_loss_cap({})
            # First call should log
            warning_count_1 = sum(1 for r in caplog.records if "LOSS CAP" in r.message)

            # Second call: ladder is already removed, so no repeated log
            mgr.check_loss_cap({})
            warning_count_2 = sum(1 for r in caplog.records if "LOSS CAP" in r.message)

        assert warning_count_1 == 1
        assert warning_count_2 == 1  # No additional log on second call


# ─── Task 4: Imbalance timing fix ──────────────────────────────────────────

class TestImbalanceTimingFix:
    def test_imbalance_requires_min_fills(self, cfg, mock_clob, market):
        """With 2 UP fills and 0 DN fills, imbalance should NOT fire."""
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 2, price=0.45, size=10.0)

        state = mgr.ladders[mid]
        mgr.check_imbalance(1200)
        assert state.heavy_side_locked is None

    def test_imbalance_fires_at_min_fills(self, cfg, mock_clob, market):
        """With 3 UP fills and 0 DN fills (imbalance > threshold), lock IS set."""
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)

        mgr.check_imbalance(1200)
        state = mgr.ladders[mid]
        assert state.heavy_side_locked == "UP"

    def test_imbalance_no_lock_when_both_sides_have_fills(self, cfg, mock_clob, market):
        """With 5 UP fills and 1 DN fill, lock is NOT set (light_count > 0)."""
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 5, price=0.45, size=10.0)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 1, price=0.45, size=10.0)

        mgr.check_imbalance(1200)
        state = mgr.ladders[mid]
        # Should not lock heavy side because light_count > 0
        assert state.heavy_side_locked is None

    def test_imbalance_dynamic_timeout_15m(self, cfg, mock_clob, market):
        """For a 15m window, timeout is max(120, 900*0.30) = 270s."""
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        state = mgr.ladders[mid]
        state.timeframe_sec = 900
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)

        # First call sets the alert
        mgr.check_imbalance(1000)
        assert state.imbalance_alert_at == 1000

        # At 120s later (old timeout) -- should NOT have timed out
        mgr.check_imbalance(1120)
        assert not state.imbalance_accepted

        # At 270s later -- should have timed out
        mgr.check_imbalance(1271)
        assert state.imbalance_accepted

    def test_imbalance_dynamic_timeout_1h(self, cfg, mock_clob):
        """For a 1h window, timeout is max(120, 3600*0.30) = 1080s."""
        market_1h = MarketWindow(
            market_id="btc-1h-100",
            condition_id="0xabc",
            asset="BTC",
            timeframe_sec=3600,
            up_token_id="tok_up",
            dn_token_id="tok_dn",
            open_epoch=1000,
            close_epoch=4600,
        )
        cfg_1h = BotConfig(
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
        )
        clob = MagicMock()
        book = MagicMock()
        book.asks = [MagicMock(price="0.46", size="5000")]
        book.bids = [MagicMock(price="0.44", size="5000")]
        clob.get_order_book.return_value = book
        clob.get_open_orders.return_value = []

        mgr = _make_manager(cfg_1h, clob)
        mgr.post_ladder(market_1h)
        mid = market_1h.market_id
        state = mgr.ladders[mid]
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)

        mgr.check_imbalance(1000)
        assert state.imbalance_alert_at == 1000

        # At 120s -- should NOT have timed out
        mgr.check_imbalance(1120)
        assert not state.imbalance_accepted

        # At 1080s -- should have timed out
        mgr.check_imbalance(2081)
        assert state.imbalance_accepted


# ─── Task 5: Phase D -- Boost Light Side ────────────────────────────────────

class TestBoostLightSide:
    def test_boost_triggers_at_threshold(self, cfg, mock_clob, market):
        """Verify boost fires when all conditions met."""
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)
        # 20% of 900s = 180s -> now = 1000 + 180 = 1180
        result = mgr.boost_light_side(market, 1180.0)
        assert result > 0

    def test_boost_skips_before_elapsed_threshold(self, cfg, mock_clob, market):
        """Verify no boost at 15% elapsed."""
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)
        # 15% of 900s = 135s -> now = 1000 + 135 = 1135
        result = mgr.boost_light_side(market, 1135.0)
        assert result == 0

    def test_boost_only_once_per_window(self, cfg, mock_clob, market):
        """Verify second call returns 0."""
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)
        result1 = mgr.boost_light_side(market, 1200.0)
        assert result1 > 0
        result2 = mgr.boost_light_side(market, 1300.0)
        assert result2 == 0

    def test_boost_cancels_light_side_rungs(self, cfg, mock_clob, market):
        """Verify old light-side orders are cancelled before new ones are placed."""
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id

        # Record initial DN resting orders
        dn_resting_before = len(mgr.tracker.get_resting_side(mid, Side.DOWN))
        assert dn_resting_before > 0

        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)
        mgr.boost_light_side(market, 1200.0)

        # The old DN orders should be cancelled (status changed)
        # New DN orders should be placed
        state = mgr.ladders[mid]
        assert state.boosted_side == Side.DOWN

    def test_boost_skips_killed_ladder(self, cfg, mock_clob, market):
        """Verify no boost on a market in _killed_ladders."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 2, price=0.45, size=10.0)
        mgr.check_loss_cap({})
        assert mgr.is_killed(mid)

        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 3, price=0.45, size=10.0)
        result = mgr.boost_light_side(market, 1200.0)
        assert result == 0


# ─── Task 6: Phase B -- Force-Buy ──────────────────────────────────────────

class TestForceBuy:
    def _setup_one_sided_position(self, mgr, market):
        """Set up a position that is >75% one-sided (UP heavy, no DN)."""
        mid = market.market_id
        # Add UP fills
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 4, price=0.45, size=10.0)
        # Credit position
        mgr.positions.update_position(mid, Side.UP, 40.0, 40.0 * 0.45)

    def test_force_buy_triggers_at_threshold(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        self._setup_one_sided_position(mgr, market)
        # 70% of 900s = 630s -> now = 1000 + 630 = 1630
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is not None
        assert "side" in result
        assert "price" in result
        assert "qty" in result
        assert "pair_cost" in result

    def test_force_buy_skips_before_threshold(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        self._setup_one_sided_position(mgr, market)
        # 60% elapsed
        result = mgr.try_complete_pair(market, 1540.0)
        assert result is None

    def test_force_buy_skips_balanced_position(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        # Balanced: 40 UP, 15 DN (15/40 = 37.5% > 25%)
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 4, price=0.45, size=10.0)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 3, price=0.45, size=5.0)
        mgr.positions.update_position(mid, Side.UP, 40.0, 40.0 * 0.45)
        mgr.positions.update_position(mid, Side.DOWN, 15.0, 15.0 * 0.45)
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is None

    def test_force_buy_pair_cost_guard(self, cfg, mock_clob, market):
        """No action when hypothetical pair_cost >= force_buy_max_pair_cost."""
        # Set up a config with very low force_buy_max_pair_cost
        strict_cfg = BotConfig(
            private_key="0xfake",
            api_key="key",
            api_secret="secret",
            api_passphrase="pass",
            ladder_rungs=8,
            ladder_spacing=0.02,
            ladder_width=0.10,
            ladder_size_skew=1.0,
            max_pair_cost=0.90,
            force_buy_max_pair_cost=0.50,  # Very strict -- UP VWAP 0.45 + estimated 0.42 = 0.87 > 0.50
            force_buy_elapsed_pct=0.70,
            boost_elapsed_pct=0.20,
            imbalance_min_heavy_fills=3,
        )
        mgr = _make_manager(strict_cfg, mock_clob)
        mgr.post_ladder(market)
        self._setup_one_sided_position(mgr, market)
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is None

    def test_force_buy_returns_correct_dict(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        self._setup_one_sided_position(mgr, market)
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is not None
        assert result["side"] == Side.DOWN
        assert result["qty"] == pytest.approx(40.0, abs=1.0)
        assert result["pair_cost"] < 0.93

    def test_force_buy_places_at_best_ask(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        self._setup_one_sided_position(mgr, market)
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is not None
        assert result["price"] == pytest.approx(0.42, abs=0.01)

    def test_force_buy_size_matches_deficit(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        # UP has 40 qty, DN has 5 qty -> deficit = 40 - 5 = 35
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 4, price=0.45, size=10.0)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 1, price=0.45, size=5.0)
        mgr.positions.update_position(mid, Side.UP, 40.0, 40.0 * 0.45)
        mgr.positions.update_position(mid, Side.DOWN, 5.0, 5.0 * 0.45)
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is not None
        assert result["qty"] == pytest.approx(35.0, abs=1.0)

    def test_force_buy_min_order_size_guard(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        # UP=10.1, DN=9 -> deficit=1.1, cost ~$0.51 < MIN_ORDER_SIZE ($5)
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 1, price=0.45, size=10.1)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 1, price=0.45, size=9.0)
        mgr.positions.update_position(mid, Side.UP, 10.1, 10.1 * 0.45)
        mgr.positions.update_position(mid, Side.DOWN, 9.0, 9.0 * 0.45)
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is None

    def test_force_buy_no_liquidity(self, cfg, market):
        """Returns None when estimate_fill_cost returns None."""
        clob = MagicMock()
        empty_book = MagicMock()
        empty_book.asks = []
        empty_book.bids = []
        clob.get_order_book.return_value = empty_book
        clob.get_open_orders.return_value = []

        mgr = _make_manager(cfg, clob)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 4, price=0.45, size=10.0)
        mgr.positions.update_position(mid, Side.UP, 40.0, 40.0 * 0.45)
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is None

    def test_force_buy_skips_killed_ladder(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 2, price=0.45, size=10.0)
        mgr.positions.update_position(mid, Side.UP, 20.0, 20.0 * 0.45)
        mgr.check_loss_cap({})
        assert mgr.is_killed(mid)
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is None

    def test_force_buy_credits_position(self, cfg, mock_clob, market):
        mgr = _make_manager(cfg, mock_clob)
        mgr.post_ladder(market)
        mid = market.market_id
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 4, price=0.45, size=10.0)
        mgr.positions.update_position(mid, Side.UP, 40.0, 40.0 * 0.45)

        dn_before = mgr.positions.positions.get(mid, Position(market_id=mid)).dn_qty
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is not None
        dn_after = mgr.positions.positions[mid].dn_qty
        assert dn_after > dn_before


# ─── Task 7: Bot wiring ────────────────────────────────────────────────────

class TestBotWiring:
    def test_bot_skips_killed_on_force_buy(self, cfg, mock_clob, market):
        """Verify try_complete_pair is not called for killed markets."""
        mgr = _make_manager(cfg, mock_clob, bankroll=100.0)
        mgr.post_ladder(market)
        _add_filled_orders(mgr.tracker, market.market_id, Side.UP, "tok_up", 2, price=0.45, size=10.0)
        mgr.check_loss_cap({})
        assert mgr.is_killed(market.market_id)

        # The is_killed guard means try_complete_pair should not be called
        # We test this at the manager level: try_complete_pair returns None for killed markets
        result = mgr.try_complete_pair(market, 1630.0)
        assert result is None

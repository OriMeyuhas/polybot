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


def _make_manager(cfg, mock_clob):
    executor = OrderExecutor(cfg, clob_client=mock_clob)
    tracker = OrderTracker()
    positions = PositionManager(cfg, bankroll=1000.0)
    risk = RiskManager(cfg, starting_bankroll=1000.0)
    return LadderManager(cfg, executor, tracker, positions, risk)


class TestBuildLadderRungs:
    def test_correct_number_of_rungs(self):
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=50.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
        )
        assert len(rungs) == 8

    def test_prices_ascending(self):
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=50.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
        )
        prices = [p for p, s in rungs]
        assert prices == sorted(prices)

    def test_sizes_ascending_with_skew(self):
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=50.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
        )
        sizes = [s for p, s in rungs]
        # Most expensive rung should be larger than cheapest
        assert sizes[-1] > sizes[0]

    def test_skew_ratio(self):
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=50.0, rungs=8,
            spacing=0.02, width=0.10, size_skew=3.0,
        )
        sizes = [s for p, s in rungs]
        ratio = sizes[-1] / sizes[0]
        # Should be close to the skew ratio (not exact due to price differences)
        assert ratio > 2.0

    def test_total_cost_matches_budget(self):
        budget = 50.0
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=budget, rungs=8,
            spacing=0.02, width=0.10, size_skew=2.0,
        )
        total_cost = sum(p * s for p, s in rungs)
        assert total_cost == pytest.approx(budget, rel=0.05)

    def test_anchor_clamped_to_min(self):
        rungs = build_ladder_rungs(
            best_ask=0.05, budget=50.0, rungs=4,
            spacing=0.01, width=0.10, size_skew=1.0,
        )
        prices = [p for p, s in rungs]
        assert all(p >= 0.01 for p in prices)


class TestPostLadder:
    def test_posts_orders_both_sides(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        count = mgr.post_ladder(market)
        assert count > 0
        assert mgr.has_ladder(market.market_id)

    def test_no_ladder_when_halted(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        mgr.risk.update_pnl(-100.0)  # trigger halt
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
        assert fills == 1
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
        assert fills == 0


class TestImbalance:
    def test_no_action_when_balanced(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        mgr.ladders[market.market_id] = MagicMock(imbalance_alert_at=None)
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
        # UP has 20 filled, DOWN has 5 filled -> imbalance = 15/20 = 75%
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
        # Add a resting UP order that should get cancelled
        mgr.tracker.add(TrackedOrder(
            order_id="o3", market_id=market.market_id,
            token_id="tok_up", side=Side.UP,
            price=0.50, size=10.0, placed_at=1000.0,
        ))
        mgr.tracker.update_fill("o1", 20.0)
        mgr.tracker.update_fill("o2", 5.0)
        acted = mgr.check_imbalance(now_epoch=2000)
        assert market.market_id in acted
        # o3 should be cancelled
        assert mgr.tracker.orders["o3"].status == "cancelled"


class TestEarlyExit:
    def test_early_exit_returns_empty(self, cfg, market, mock_clob):
        """Early exit was removed; check_early_exits always returns []."""
        mgr = _make_manager(cfg, mock_clob)
        market_map = {market.market_id: market}
        exits = mgr.check_early_exits(market_map)
        assert exits == []


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

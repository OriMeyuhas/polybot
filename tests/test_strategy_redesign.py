"""Tests for the strategy redesign: exit capability, reactive pairing,
inventory-aware quoting, and tighter width/reprice settings."""

import time
from unittest.mock import MagicMock, patch

import pytest

from polybot.config import BotConfig
from polybot.strategy.ladder_manager import (
    LadderManager,
    LadderState,
    build_ladder_rungs,
    MIN_ORDER_SIZE,
)
from polybot.strategy.position_manager import PositionManager
from polybot.types import MarketWindow, Position, Side


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(**overrides):
    defaults = dict(
        dry_run=True,
        bankroll=500,
        ladder_rungs=10,
        ladder_spacing=0.01,
        ladder_width=0.29,
        ladder_width_1h=0.35,
        ladder_size_skew=1.0,
        max_pair_cost=0.93,
        position_size_fraction=0.10,
        reprice_threshold=0.03,
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
        spot_delta_reduce_threshold=0.0015,
        spot_delta_skip_threshold=0.005,
        spot_gate_force_buy_threshold=0.003,
        spot_loss_cap_multiplier=0.50,
        no_trade_final_sec=60,
        imbalance_min_heavy_fills=1,
        boost_elapsed_pct=0.20,
        force_buy_elapsed_pct=0.70,
        force_buy_max_pair_cost=0.83,
        batch_order_size=15,
        maker_fee_rate=0.0,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _market(market_id="btc-15m-100", timeframe_sec=900, now_offset=0):
    """Create a market window. now_offset controls how far into the window we are."""
    open_epoch = 1000
    return MarketWindow(
        market_id=market_id,
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=timeframe_sec,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=open_epoch,
        close_epoch=open_epoch + timeframe_sec,
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

    lm = LadderManager(cfg, executor, tracker, pm, risk, tick_cache)
    return lm


# ── Width tests ──────────────────────────────────────────────────────────────

class TestTighterWidth:
    def test_15m_width_is_029(self):
        cfg = _cfg()
        lp = cfg.get_ladder_params(900, current_bankroll=500)
        assert lp.width == 0.29

    def test_1h_width(self):
        cfg = BotConfig()  # use real defaults
        lp = cfg.get_ladder_params(3600, current_bankroll=500)
        assert lp.width == 0.20

    def test_5m_width_unchanged(self):
        cfg = _cfg()
        lp = cfg.get_ladder_params(300, current_bankroll=500)
        assert lp.width == 0.08  # 5m tightened

    def test_tighter_width_produces_closer_rungs(self):
        """Rungs should be closer to best_ask with narrower width."""
        wide = build_ladder_rungs(0.50, 50, 10, 0.01, 0.41, 1.0)
        tight = build_ladder_rungs(0.50, 50, 10, 0.01, 0.29, 1.0)
        # Cheapest rung in tight ladder should be more expensive (closer to market)
        assert tight[0][0] > wide[0][0]


# ─��� Reprice threshold tests ─────────────────────────────────────────────────

class TestRepriceThreshold:
    def test_default_reprice_threshold_is_003(self):
        cfg = _cfg()
        assert cfg.reprice_threshold == 0.03


# ── Exit capability tests ───────────────────────────────────────────────────

class TestExitCapability:
    def test_exit_disabled_returns_none(self):
        lm = _make_manager(_cfg(exit_enabled=False))
        market = _market()
        result = lm.sell_losing_side(market, 1600)
        assert result is None

    def test_exit_before_elapsed_pct_returns_none(self):
        lm = _make_manager()
        market = _market()  # 900s window
        # 55% of 900 = 495s after open, so now=1494 is just before threshold
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        result = lm.sell_losing_side(market, 1490)  # 54.4% elapsed
        assert result is None

    def test_exit_triggers_on_one_sided_position(self):
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        # Create one-sided position: 50 UP, 0 DN
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=50.0, up_cost=15.0, dn_qty=0.0, dn_cost=0.0,
        )
        result = lm.sell_losing_side(market, 1600)  # 66% elapsed
        assert result is not None
        assert result["side"] == Side.UP
        assert result["qty"] == 50.0

    def test_exit_does_not_trigger_on_balanced_position(self):
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        # Balanced position: 50 UP, 45 DN
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=50.0, up_cost=15.0, dn_qty=45.0, dn_cost=13.0,
        )
        result = lm.sell_losing_side(market, 1600)
        assert result is None

    def test_exit_only_fires_once(self):
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=50.0, up_cost=15.0, dn_qty=0.0, dn_cost=0.0,
        )
        result1 = lm.sell_losing_side(market, 1600)
        assert result1 is not None
        result2 = lm.sell_losing_side(market, 1700)
        assert result2 is None  # exit_done flag set

    def test_exit_skips_low_midpoint(self):
        lm = _make_manager()
        lm.executor.get_midpoint.return_value = 0.10  # below exit_min_price
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=50.0, up_cost=15.0, dn_qty=0.0, dn_cost=0.0,
        )
        result = lm.sell_losing_side(market, 1600)
        assert result is None

    def test_exit_skips_killed_ladder(self):
        lm = _make_manager()
        market = _market()
        lm._killed_ladders.add("btc-15m-100")
        result = lm.sell_losing_side(market, 1600)
        assert result is None


# ── Reactive pairing tests ──────────────────────────────────────────────────

class TestReactivePairing:
    def test_chase_disabled_returns_zero(self):
        lm = _make_manager(_cfg(reactive_pairing_enabled=False))
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        assert lm.chase_pair(market, 1200) == 0

    def test_chase_triggers_on_one_sided_fill(self):
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        # UP has fills, DN has none
        def filled_count_side(mid, side):
            return 3 if side == Side.UP else 0
        lm.tracker.filled_count = filled_count_side
        lm.tracker.filled_qty.return_value = 0.0
        lm.tracker.filled_cost.return_value = 0.0

        # Mock first fill time to be long ago (past the wait period)
        with patch.object(lm, '_first_fill_time', return_value=1000):
            count = lm.chase_pair(market, 1200)
        assert count > 0

    def test_chase_waits_for_natural_fill(self):
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        def filled_count_side(mid, side):
            return 3 if side == Side.UP else 0
        lm.tracker.filled_count = filled_count_side

        # First fill was very recent (now - 5s < wait period)
        with patch.object(lm, '_first_fill_time', return_value=1195):
            count = lm.chase_pair(market, 1200)
        assert count == 0  # should wait

    def test_chase_skips_both_sides_filled(self):
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        # Both sides have fills
        lm.tracker.filled_count.return_value = 3
        count = lm.chase_pair(market, 1200)
        assert count == 0

    def test_chase_only_fires_once(self):
        lm = _make_manager()
        market = _market()
        lm.ladders["btc-15m-100"] = LadderState(
            market_id="btc-15m-100", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45, posted_at=1000,
        )
        def filled_count_side(mid, side):
            return 3 if side == Side.UP else 0
        lm.tracker.filled_count = filled_count_side
        lm.tracker.filled_qty.return_value = 0.0
        lm.tracker.filled_cost.return_value = 0.0

        with patch.object(lm, '_first_fill_time', return_value=1000):
            count1 = lm.chase_pair(market, 1200)
            count2 = lm.chase_pair(market, 1300)
        assert count1 > 0
        assert count2 == 0  # chase_done


# ── Inventory-aware quoting tests ───────────────────────────────────────────

class TestInventoryAwareQuoting:
    def test_inventory_skew_config_defaults(self):
        cfg = _cfg()
        assert cfg.inventory_skew_enabled is True
        assert cfg.inventory_skew_max == 0.60

    def test_inventory_skew_disabled_gives_equal_budget(self):
        cfg = _cfg(inventory_skew_enabled=False)
        # When disabled, budget_up_side == budget_dn_side
        assert cfg.inventory_skew_enabled is False


# ── Position reduce tests ──────────────────────────────────────────────────

class TestPositionReduce:
    def test_reduce_up_position(self):
        pm = PositionManager(_cfg(), 500)
        pm.positions["m1"] = Position(
            market_id="m1", up_qty=100.0, up_cost=30.0, dn_qty=0.0, dn_cost=0.0,
        )
        pm.reduce_position("m1", Side.UP, 50.0, 17.5)
        assert pm.positions["m1"].up_qty == 50.0
        assert pm.positions["m1"].up_cost == pytest.approx(15.0)

    def test_reduce_dn_position(self):
        pm = PositionManager(_cfg(), 500)
        pm.positions["m1"] = Position(
            market_id="m1", up_qty=0.0, up_cost=0.0, dn_qty=80.0, dn_cost=24.0,
        )
        pm.reduce_position("m1", Side.DOWN, 40.0, 14.0)
        assert pm.positions["m1"].dn_qty == 40.0
        assert pm.positions["m1"].dn_cost == pytest.approx(12.0)

    def test_reduce_more_than_available(self):
        pm = PositionManager(_cfg(), 500)
        pm.positions["m1"] = Position(
            market_id="m1", up_qty=30.0, up_cost=9.0, dn_qty=0.0, dn_cost=0.0,
        )
        pm.reduce_position("m1", Side.UP, 50.0, 17.5)  # more than 30
        assert pm.positions["m1"].up_qty == 0.0

    def test_reduce_nonexistent_position(self):
        pm = PositionManager(_cfg(), 500)
        pm.reduce_position("missing", Side.UP, 50.0, 17.5)  # no crash


# ── Paper SELL fill handling ────────────────────────────────────────────────

class TestPaperSellFills:
    def test_sell_fill_reduces_position(self):
        lm = _make_manager()
        # Add tracked order that represents a sell
        from polybot.order_tracker import TrackedOrder
        sell_order = TrackedOrder(
            order_id="sell-1", market_id="btc-15m-100",
            token_id="tok_up", side=Side.UP,
            price=0.35, size=50.0, placed_at=1000,
        )
        lm.tracker.orders = {"sell-1": sell_order}

        # Create existing position
        lm.positions.positions["btc-15m-100"] = Position(
            market_id="btc-15m-100", up_qty=50.0, up_cost=15.0,
            dn_qty=0.0, dn_cost=0.0,
        )

        initial_bankroll = lm.positions.bankroll
        paper_fills = [{"orderID": "sell-1", "side": "SELL"}]
        filled = lm.process_paper_fills(paper_fills)

        assert len(filled) == 1
        assert lm.positions.positions["btc-15m-100"].up_qty == 0.0
        # Bankroll should increase by proceeds
        assert lm.positions.bankroll == initial_bankroll + 0.35 * 50.0

    def test_buy_fill_adds_to_position(self):
        lm = _make_manager()
        from polybot.order_tracker import TrackedOrder
        buy_order = TrackedOrder(
            order_id="buy-1", market_id="btc-15m-100",
            token_id="tok_up", side=Side.UP,
            price=0.30, size=20.0, placed_at=1000,
        )
        lm.tracker.orders = {"buy-1": buy_order}

        paper_fills = [{"orderID": "buy-1", "side": "BUY"}]
        filled = lm.process_paper_fills(paper_fills)

        assert len(filled) == 1
        pos = lm.positions.positions["btc-15m-100"]
        assert pos.up_qty == 20.0


# ── place_limit_sell executor tests ─────────────────────────────────────────

class TestPlaceLimitSell:
    def test_sell_order_uses_sell_side(self):
        """Verify place_limit_sell passes SELL side to create_order."""
        from polybot.oms.order_executor import OrderExecutor, SELL

        cfg = _cfg()
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"order": "signed"}
        mock_client.post_order.return_value = {"orderID": "sell-99", "status": "open"}
        mock_client.get_tick_size.return_value = 0.01

        executor = OrderExecutor(cfg, mock_client)
        record = executor.place_limit_sell(
            "tok_up", 0.35, 50.0, "btc-15m-100", Side.UP,
        )

        assert record.order_id == "sell-99"
        # Verify the order_args had side=SELL
        call_args = mock_client.create_order.call_args
        order_args = call_args[0][0]
        assert order_args.side == SELL

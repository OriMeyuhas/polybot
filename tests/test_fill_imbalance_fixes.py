"""Tests for fill imbalance fixes (P0, P1, P2)."""
import pytest
from unittest.mock import MagicMock
from polybot.strategy.ladder_manager import LadderState, LadderManager, build_ladder_rungs
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.strategy.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.config import BotConfig, get_trading_rules
from polybot.order_executor import OrderExecutor
from polybot.types import MarketWindow, Side


def _cfg(**overrides):
    defaults = dict(
        private_key="0xfake", api_key="k", api_secret="s", api_passphrase="p",
        dry_run=True, bankroll=10000.0,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _market(market_id="btc-15m-100", timeframe_sec=900):
    return MarketWindow(
        market_id=market_id, condition_id="0xabc", asset="BTC",
        timeframe_sec=timeframe_sec, up_token_id="tok_up", dn_token_id="tok_dn",
        open_epoch=1000, close_epoch=1000 + timeframe_sec,
    )


def _make_manager(cfg=None, bankroll=10000.0):
    cfg = cfg or _cfg()
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
    positions = PositionManager(cfg, bankroll=bankroll)
    risk = RiskManager(cfg, starting_bankroll=bankroll)
    return LadderManager(cfg, executor, tracker, positions, risk)


# ---------- Task 1: heavy_side_locked field ----------

class TestHeavySideLocked:
    def test_ladder_state_has_heavy_side_locked(self):
        state = LadderState(
            market_id="m1", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45,
            posted_at=1000.0,
        )
        assert state.heavy_side_locked is None

    def test_heavy_side_locked_can_be_set(self):
        state = LadderState(
            market_id="m1", asset="BTC",
            anchor_up=0.45, anchor_dn=0.45,
            posted_at=1000.0,
        )
        state.heavy_side_locked = "UP"
        assert state.heavy_side_locked == "UP"


# ---------- Task 2: reprice preserves imbalance_accepted ----------

class TestRepricePreservesImbalanceState:
    def test_reprice_does_not_reset_imbalance_accepted(self):
        """reprice_if_needed must NOT reset imbalance_accepted to False."""
        cfg = _cfg(reprice_threshold=0.01)
        lm = _make_manager(cfg)
        market = _market()

        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.40, anchor_dn=0.40,
            posted_at=1000.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        state.imbalance_accepted = True
        lm.ladders[market.market_id] = state

        # Force reprice by moving beyond threshold
        lm.reprice_if_needed({market.market_id: market})

        # imbalance_accepted must still be True
        assert lm.ladders[market.market_id].imbalance_accepted is True


# ---------- Task 3: reprice respects heavy_side_locked ----------

class TestRepriceRespectsHeavySideLock:
    def test_reprice_skips_locked_up_side(self):
        """When heavy_side_locked='UP', reprice must not post new UP orders."""
        cfg = _cfg(reprice_threshold=0.01)
        lm = _make_manager(cfg)
        market = _market()

        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.40, anchor_dn=0.40,
            posted_at=1000.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        state.heavy_side_locked = "UP"
        lm.ladders[market.market_id] = state

        # Track what sides get orders placed
        placed_sides = []
        original_place = lm.executor.place_batch_limit_buys

        def tracking_place(orders):
            for o in orders:
                placed_sides.append(o["side"])
            return original_place(orders)

        lm.executor.place_batch_limit_buys = tracking_place

        lm.reprice_if_needed({market.market_id: market})

        # Should not have placed any UP orders
        assert Side.UP not in placed_sides
        # Anchor should still update so we don't re-trigger
        assert lm.ladders[market.market_id].anchor_up == 0.46  # mock best_ask

    def test_reprice_skips_locked_dn_side(self):
        """When heavy_side_locked='DOWN', reprice must not post new DN orders."""
        cfg = _cfg(reprice_threshold=0.01)
        lm = _make_manager(cfg)
        market = _market()

        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.40, anchor_dn=0.40,
            posted_at=1000.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        state.heavy_side_locked = "DOWN"
        lm.ladders[market.market_id] = state

        placed_sides = []
        original_place = lm.executor.place_batch_limit_buys

        def tracking_place(orders):
            for o in orders:
                placed_sides.append(o["side"])
            return original_place(orders)

        lm.executor.place_batch_limit_buys = tracking_place

        lm.reprice_if_needed({market.market_id: market})

        assert Side.DOWN not in placed_sides
        assert lm.ladders[market.market_id].anchor_dn == 0.46

    def test_reprice_allows_unlocked_side(self):
        """When heavy_side_locked is None, both sides reprice normally."""
        cfg = _cfg(reprice_threshold=0.01)
        lm = _make_manager(cfg)
        market = _market()

        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.40, anchor_dn=0.40,
            posted_at=1000.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        lm.ladders[market.market_id] = state

        placed_sides = []
        original_place = lm.executor.place_batch_limit_buys

        def tracking_place(orders):
            for o in orders:
                placed_sides.append(o["side"])
            return original_place(orders)

        lm.executor.place_batch_limit_buys = tracking_place

        lm.reprice_if_needed({market.market_id: market})

        assert Side.UP in placed_sides
        assert Side.DOWN in placed_sides


class TestImbalanceSetsLock:
    def test_severe_imbalance_sets_heavy_side_locked(self):
        """check_imbalance severe path must set heavy_side_locked on the state.

        Requires: heavy side has >= imbalance_min_heavy_fills (default 3) fills,
        AND light side has 0 fills (light_count == 0).
        """
        cfg = _cfg(max_imbalance_ratio=0.35, imbalance_min_heavy_fills=3)
        lm = _make_manager(cfg)
        market = _market()

        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45,
            posted_at=1000.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        lm.ladders[market.market_id] = state

        # Simulate 10 UP fills, 0 DN fills (fully one-sided severe imbalance)
        for i in range(10):
            lm.tracker.add(TrackedOrder(
                order_id=f"up_{i}", market_id=market.market_id,
                token_id="tok_up", side=Side.UP,
                price=0.45, size=1.0, placed_at=1000.0,
            ))
            lm.tracker.update_fill(f"up_{i}", 1.0)

        lm.executor.cancel_order = MagicMock()
        lm.check_imbalance(int(state.posted_at + 10))

        assert state.heavy_side_locked == "UP"


# ---------- Task 4: _check_one_side_cap ----------

class TestOneSideCap:
    def test_cancels_heavy_side_at_3to1_with_min_qty(self):
        """_check_one_side_cap cancels heavy side when ratio > 3:1 AND qty > 5."""
        cfg = _cfg()
        lm = _make_manager(cfg)
        market = _market()

        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45,
            posted_at=1000.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        lm.ladders[market.market_id] = state

        # 8 UP fills, 2 DN fills = 4:1 ratio, UP qty=8 > 5
        for i in range(8):
            lm.tracker.add(TrackedOrder(
                order_id=f"up_{i}", market_id=market.market_id,
                token_id="tok_up", side=Side.UP,
                price=0.45, size=1.0, placed_at=1000.0,
            ))
            lm.tracker.update_fill(f"up_{i}", 1.0)
        for i in range(2):
            lm.tracker.add(TrackedOrder(
                order_id=f"dn_{i}", market_id=market.market_id,
                token_id="tok_dn", side=Side.DOWN,
                price=0.45, size=1.0, placed_at=1000.0,
            ))
            lm.tracker.update_fill(f"dn_{i}", 1.0)

        # Add resting UP orders that should be cancelled
        for i in range(3):
            lm.tracker.add(TrackedOrder(
                order_id=f"up_rest_{i}", market_id=market.market_id,
                token_id="tok_up", side=Side.UP,
                price=0.44, size=1.0, placed_at=1000.0,
            ))

        lm.executor.cancel_order = MagicMock()

        lm._check_one_side_cap(market.market_id)

        # Should have cancelled 3 resting UP orders
        assert lm.executor.cancel_order.call_count == 3
        # heavy_side_locked should be set
        assert state.heavy_side_locked == "UP"

    def test_no_cancel_when_ratio_under_3to1(self):
        """_check_one_side_cap does nothing when ratio <= 3:1."""
        cfg = _cfg()
        lm = _make_manager(cfg)
        market = _market()

        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45,
            posted_at=1000.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        lm.ladders[market.market_id] = state

        # 6 UP, 3 DN = 2:1 ratio (not > 3:1)
        for i in range(6):
            lm.tracker.add(TrackedOrder(
                order_id=f"up_{i}", market_id=market.market_id,
                token_id="tok_up", side=Side.UP,
                price=0.45, size=1.0, placed_at=1000.0,
            ))
            lm.tracker.update_fill(f"up_{i}", 1.0)
        for i in range(3):
            lm.tracker.add(TrackedOrder(
                order_id=f"dn_{i}", market_id=market.market_id,
                token_id="tok_dn", side=Side.DOWN,
                price=0.45, size=1.0, placed_at=1000.0,
            ))
            lm.tracker.update_fill(f"dn_{i}", 1.0)

        lm.executor.cancel_order = MagicMock()
        lm._check_one_side_cap(market.market_id)
        assert lm.executor.cancel_order.call_count == 0

    def test_no_cancel_when_heavy_qty_under_5(self):
        """_check_one_side_cap does nothing when heavy side qty <= 5 even if ratio > 3:1."""
        cfg = _cfg()
        lm = _make_manager(cfg)
        market = _market()

        state = LadderState(
            market_id=market.market_id, asset="BTC",
            anchor_up=0.45, anchor_dn=0.45,
            posted_at=1000.0,
            up_token_id="tok_up", dn_token_id="tok_dn",
        )
        lm.ladders[market.market_id] = state

        # 4 UP, 1 DN = 4:1 ratio, but UP qty=4 <= 5
        for i in range(4):
            lm.tracker.add(TrackedOrder(
                order_id=f"up_{i}", market_id=market.market_id,
                token_id="tok_up", side=Side.UP,
                price=0.45, size=1.0, placed_at=1000.0,
            ))
            lm.tracker.update_fill(f"up_{i}", 1.0)
        lm.tracker.add(TrackedOrder(
            order_id="dn_0", market_id=market.market_id,
            token_id="tok_dn", side=Side.DOWN,
            price=0.45, size=1.0, placed_at=1000.0,
        ))
        lm.tracker.update_fill("dn_0", 1.0)

        lm.executor.cancel_order = MagicMock()
        lm._check_one_side_cap(market.market_id)
        assert lm.executor.cancel_order.call_count == 0


# ---------- Task 5: Disable 5m for bankroll < $2000 ----------

class TestDisable5mSmallBankroll:
    def test_small_tier_no_5m(self):
        """Small tier ($200-$500) must not include 300s (5m) timeframe."""
        rules = get_trading_rules(("BTC", "ETH"), bankroll=300.0)
        assert 300 not in rules.timeframes
        assert 900 in rules.timeframes

    def test_medium_tier_no_5m(self):
        """Medium tier ($500-$2000) must not include 300s (5m) timeframe."""
        rules = get_trading_rules(("BTC", "ETH"), bankroll=1000.0)
        assert 300 not in rules.timeframes
        assert 900 in rules.timeframes
        assert 3600 in rules.timeframes

    def test_standard_tier_has_5m(self):
        """Standard tier ($2000+) keeps 5m timeframe."""
        rules = get_trading_rules(("BTC", "ETH"), bankroll=3000.0)
        assert 300 in rules.timeframes

    def test_micro_tier_unchanged(self):
        """Micro tier (< $200) stays 15m only."""
        rules = get_trading_rules(("BTC",), bankroll=100.0)
        assert rules.timeframes == (900,)

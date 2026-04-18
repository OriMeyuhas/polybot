"""Tests for one-sided post sizing: 10% of bankroll (user directive 2026-04-18).

Rules:
- Paired two-sided posts: use get_ladder_params().position_size_fraction — UNCHANGED.
- directional_buy path (>=92% cert late-window): 10% of bankroll, clamped by available.
- FV-gate one-sided ladder: 10% of bankroll, clamped by available.

Bankroll selection rationale:
- directional_buy tests use $660 (Medium tier, position_fraction=0.10). OLD formula was
  lp.position_size_fraction * bankroll * 0.5 = 0.10 * 660 * 0.5 = $33. NEW = $66.
- FV gate test uses $100 (Micro tier, position_fraction=0.15). OLD = 0.15 * 100 = $15.
  NEW = 0.10 * 100 = $10. Clear difference.
- Paired regression test uses $660. Both old and new are identical (no change).
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from polybot.config import BotConfig
from polybot.order_executor import OrderExecutor
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.strategy.ladder_manager import LadderManager, MIN_ORDER_SIZE
from polybot.types import MarketWindow, Side


BANKROLL_MEDIUM = 660.0    # Medium tier: get_trading_rules gives position_fraction=0.10
BANKROLL_MICRO = 100.0     # Micro tier: get_trading_rules gives position_fraction=0.15


def _make_cfg(bankroll: float = BANKROLL_MEDIUM, **overrides) -> BotConfig:
    defaults = dict(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        ladder_rungs=8,
        ladder_spacing=0.01,
        ladder_width=0.15,
        ladder_size_skew=2.0,
        max_pair_cost=0.93,
        position_size_fraction=0.05,
        ladder_rungs_5m=8,
        ladder_spacing_5m=0.01,
        ladder_width_5m=0.10,
        max_pair_cost_5m=0.93,
        ladder_rungs_1h=8,
        ladder_spacing_1h=0.01,
        ladder_width_1h=0.15,
        max_pair_cost_1h=0.96,
        fair_value_enabled=True,
        fv_gate_enabled=False,
        book_mid_gate_enabled=False,
        skip_on_gate_miss=False,
        directional_budget_cap=500.0,   # high so it never binds in tests
        directional_phase_pct=0.70,
        certainty_directional_threshold=0.92,
        directional_max_ask=0.75,
        spot_delta_reduce_threshold=0.0015,
        spot_delta_skip_threshold=0.005,
        bankroll=bankroll,
        no_trade_final_sec=60,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _make_market(
    *,
    timeframe_sec: int = 900,
    elapsed_frac: float = 0.80,
    market_id: str = "btc-15m-test",
) -> MarketWindow:
    now = int(time.time())
    open_epoch = now - int(timeframe_sec * elapsed_frac)
    close_epoch = open_epoch + timeframe_sec
    return MarketWindow(
        market_id=market_id,
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=timeframe_sec,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=open_epoch,
        close_epoch=close_epoch,
    )


def _make_mock_clob(ask: float = 0.45) -> MagicMock:
    clob = MagicMock()
    clob.get_order_book.return_value = MagicMock(
        bids=[MagicMock(price=str(round(ask - 0.01, 4)), size="5000")],
        asks=[MagicMock(price=str(ask), size="5000")],
    )
    clob.create_order.return_value = {"signed": True}
    clob.post_order.return_value = {"orderID": "o1", "status": "resting"}
    clob.get_open_orders.return_value = []
    return clob


def _make_manager(cfg: BotConfig, clob: MagicMock, bankroll: float) -> LadderManager:
    executor = OrderExecutor(cfg, clob_client=clob)
    tracker = OrderTracker()
    positions = PositionManager(cfg, bankroll=bankroll)
    risk = RiskManager(cfg, starting_bankroll=bankroll)
    return LadderManager(cfg, executor, tracker, positions, risk)


def _add_ladder_state(mgr: LadderManager, market: MarketWindow) -> None:
    """Insert a LadderState so directional_buy guards pass."""
    from polybot.strategy.ladder_manager import LadderState
    mgr.ladders[market.market_id] = LadderState(
        market_id=market.market_id,
        asset=market.asset,
        anchor_up=0.45,
        anchor_dn=0.45,
        posted_at=float(market.open_epoch),
        up_token_id=market.up_token_id,
        dn_token_id=market.dn_token_id,
        timeframe_sec=market.timeframe_sec,
    )


# ===========================================================================
# Test 1: directional_buy budget == 10% of bankroll
# ===========================================================================

class TestDirectionalBuyBudget:
    """OLD: lp.position_size_fraction * bankroll * 0.5 = 0.10 * 660 * 0.5 = $33.
       NEW: 0.10 * bankroll = $66."""

    def test_directional_buy_budget_is_10pct_of_bankroll(self):
        """directional_buy must size to 10% of bankroll, not lp.position_size_fraction*0.5."""
        cfg = _make_cfg(
            bankroll=BANKROLL_MEDIUM,
            fair_value_enabled=True,
            certainty_directional_threshold=0.92,
            directional_max_ask=0.75,
            directional_phase_pct=0.70,
        )
        clob = _make_mock_clob(ask=0.40)
        mgr = _make_manager(cfg, clob, bankroll=BANKROLL_MEDIUM)

        market = _make_market(elapsed_frac=0.80)
        _add_ladder_state(mgr, market)

        # fair_up=0.98 → certainty >= 0.92; UP is winning side
        now = float(market.open_epoch + int(0.80 * market.timeframe_sec))

        captured_budgets: list[float] = []

        def spy_place(token_id, price, qty, *args, **kwargs):
            captured_budgets.append(qty * price)
            rec = MagicMock()
            rec.order_id = "ord-dir"
            rec.price = price
            rec.size = qty
            return rec

        mgr.executor.place_limit_buy = spy_place

        result = mgr.directional_buy(market, now=now, fair_up=0.98)

        assert result is not None, "directional_buy should have placed an order"
        assert len(captured_budgets) == 1
        expected = 0.10 * BANKROLL_MEDIUM   # $66.0
        assert captured_budgets[0] == pytest.approx(expected, rel=0.02), (
            f"budget was ${captured_budgets[0]:.2f}, expected ~${expected:.2f} "
            f"(10% of bankroll). OLD formula gives $33 (position_size_fraction*0.5)."
        )

    def test_directional_buy_respects_available_clamp(self):
        """When available < 10% of bankroll, budget must be clamped to available."""
        cfg = _make_cfg(
            bankroll=BANKROLL_MEDIUM,
            fair_value_enabled=True,
            certainty_directional_threshold=0.92,
            directional_max_ask=0.75,
            directional_phase_pct=0.70,
        )
        clob = _make_mock_clob(ask=0.40)
        mgr = _make_manager(cfg, clob, bankroll=BANKROLL_MEDIUM)

        # Force total_committed so available = 20.0 (< 10% of $660 = $66)
        small_available = 20.0
        mgr.total_committed = lambda: BANKROLL_MEDIUM - small_available

        market = _make_market(elapsed_frac=0.80)
        _add_ladder_state(mgr, market)

        now = float(market.open_epoch + int(0.80 * market.timeframe_sec))

        captured_budgets: list[float] = []

        def spy_place(token_id, price, qty, *args, **kwargs):
            captured_budgets.append(qty * price)
            rec = MagicMock()
            rec.order_id = "ord-dir-clamp"
            rec.price = price
            rec.size = qty
            return rec

        mgr.executor.place_limit_buy = spy_place

        result = mgr.directional_buy(market, now=now, fair_up=0.98)

        assert result is not None, "directional_buy should still place (available > MIN_ORDER)"
        assert len(captured_budgets) == 1
        # budget must not exceed available capital
        assert captured_budgets[0] <= small_available + 0.01, (
            f"budget ${captured_budgets[0]:.2f} exceeded available ${small_available:.2f}"
        )


# ===========================================================================
# Test 2: FV-gate one-sided ladder budget == 10% of bankroll
# ===========================================================================

class TestGateFiredOneSidedBudget:
    """FV gate at Micro ($100) bankroll:
       OLD: min(lp.position_size_fraction * bankroll, dir_cap) = min(0.15*100, 500) = $15.
       NEW: min(0.10 * bankroll, available) = $10.
    """

    def test_one_sided_gate_fired_ladder_budget_is_10pct(self):
        """When FV gate fires and skips the DOWN side, UP budget must be 10% of bankroll.

        Uses $100 (Micro tier) where OLD gives $15 (15% from get_trading_rules)
        and NEW should give $10 (10% flat).
        """
        bankroll = BANKROLL_MICRO   # $100, Micro tier, position_fraction=0.15
        cfg = _make_cfg(
            bankroll=bankroll,
            fv_gate_enabled=True,
            fair_value_enabled=True,
        )
        clob = _make_mock_clob(ask=0.45)
        mgr = _make_manager(cfg, clob, bankroll=bankroll)

        # Market with plenty of time left (not late-window skip)
        market = _make_market(elapsed_frac=0.10)

        up_order_costs: list[float] = []
        dn_order_costs: list[float] = []

        def spy_batch(order_dicts):
            if order_dicts:
                side = order_dicts[0].get("side")
                batch_cost = sum(d["price"] * d["size"] for d in order_dicts)
                if side == Side.UP:
                    up_order_costs.append(batch_cost)
                elif side == Side.DOWN:
                    dn_order_costs.append(batch_cost)
            records = []
            for i, d in enumerate(order_dicts):
                rec = MagicMock()
                rec.order_id = f"ord-gate-{i}"
                rec.price = d["price"]
                rec.size = d["size"]
                rec.status = "resting"
                records.append(rec)
            return records

        mgr.executor.place_batch_limit_buys = spy_batch

        # fair_up=0.98 → cert ~0.96 >= 0.80 → FV gate fires, skip DN, post UP only
        with patch("polybot.strategy.ladder_manager.fv_certainty", return_value=0.96):
            count = mgr.post_ladder(market, fair_up=0.98)

        assert count > 0, "Should have posted orders"
        # DOWN should be skipped entirely
        assert len(dn_order_costs) == 0, f"DN orders unexpectedly posted: ${sum(dn_order_costs):.2f}"

        up_total = sum(up_order_costs)
        expected = 0.10 * bankroll   # $10.0
        assert up_total == pytest.approx(expected, rel=0.05), (
            f"UP budget was ${up_total:.2f}, expected ~${expected:.2f} "
            f"(10% of ${bankroll:.0f} bankroll). "
            f"OLD formula gives ${0.15 * bankroll:.2f} (15% from Micro tier)."
        )


# ===========================================================================
# Test 3: Paired two-sided posts use position_size_fraction (regression guard)
# ===========================================================================

class TestPairedTwoSidedBudgetUnchanged:
    """Paired ladder (no gate skip) must continue using lp.position_size_fraction * bankroll."""

    def test_paired_two_sided_budget_unchanged(self):
        """Paired (no gate) ladders must still use the LadderParams position_size_fraction.

        With bankroll=$660, get_trading_rules gives position_fraction=0.10 for 15m.
        Total budget = 0.10 * 660 = $66, split roughly evenly across UP and DN.
        This must NOT become $66 per side (that would be 20% total, doubling paired spend).
        """
        bankroll = BANKROLL_MEDIUM
        cfg = _make_cfg(
            bankroll=bankroll,
            fv_gate_enabled=False,
            book_mid_gate_enabled=False,
            fair_value_enabled=False,   # cert=0, no gate
        )
        clob = _make_mock_clob(ask=0.45)
        mgr = _make_manager(cfg, clob, bankroll=bankroll)

        market = _make_market(elapsed_frac=0.10)

        side_budgets: dict[str, float] = {"UP": 0.0, "DN": 0.0}

        def spy_batch(order_dicts):
            if order_dicts:
                side = order_dicts[0].get("side")
                batch_cost = sum(d["price"] * d["size"] for d in order_dicts)
                if side == Side.UP:
                    side_budgets["UP"] += batch_cost
                elif side == Side.DOWN:
                    side_budgets["DN"] += batch_cost
            records = []
            for i, d in enumerate(order_dicts):
                rec = MagicMock()
                rec.order_id = f"ord-paired-{i}"
                rec.price = d["price"]
                rec.size = d["size"]
                rec.status = "resting"
                records.append(rec)
            return records

        mgr.executor.place_batch_limit_buys = spy_batch

        count = mgr.post_ladder(market, fair_up=0.5)

        assert count > 0, "Should have posted orders on both sides"
        assert side_budgets["UP"] > 0, "UP side should have spend"
        assert side_budgets["DN"] > 0, "DN side should have spend"

        total = side_budgets["UP"] + side_budgets["DN"]

        # Expected: lp.position_size_fraction * bankroll (the unchanged paired rule)
        lp = cfg.get_ladder_params(market.timeframe_sec, current_bankroll=bankroll)
        expected_total = lp.position_size_fraction * bankroll
        assert total == pytest.approx(expected_total, rel=0.05), (
            f"Paired total budget ${total:.2f} != expected ${expected_total:.2f}. "
            f"Paired sizing must remain UNCHANGED."
        )
        # Verify symmetric split (no spot skew, fair_up=0.5)
        assert side_budgets["UP"] == pytest.approx(side_budgets["DN"], rel=0.10), (
            f"UP=${side_budgets['UP']:.2f} and DN=${side_budgets['DN']:.2f} should be ~equal"
        )

"""TDD tests for the paired imbalance throttle.

When both sides have fills but the ratio is skewed > 30:70 (imbalance > 0.30),
the heavy side's budget should be halved for subsequent ladder posts.
This slows accumulation without killing the ladder entirely.

Scenario that motivated this feature:
- 172 UP vs 77 DN = imbalance = (172-77)/172 = 0.55 (> 0.30)
- Both sides had fills (light_count > 0), so old heavy-side lock did NOT fire
- Result: 42.83 excess drag on what should have been a +$8.96 pair win
"""

import logging
import pytest
from unittest.mock import MagicMock

from polybot.config import BotConfig
from polybot.order_executor import OrderExecutor
from polybot.strategy.ladder_manager import LadderManager, LadderState
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.types import MarketWindow, Side


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
        imbalance_min_heavy_fills=1,
    )


@pytest.fixture
def market():
    return MarketWindow(
        market_id="btc-1h-42",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=3600,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=4600,
    )


@pytest.fixture
def mock_clob():
    clob = MagicMock()
    book = MagicMock()
    book.asks = [MagicMock(price="0.45", size="500")]
    book.bids = [MagicMock(price="0.44", size="5000")]
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
        oid = f"fill-{side.value}-{market_id}-{i}"
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


# ─── Test 1: throttle_heavy_side field exists on LadderState ────────────────

class TestLadderStateThrottleField:
    def test_ladder_state_has_throttle_heavy_side_field(self):
        """LadderState must have a throttle_heavy_side field defaulting to None."""
        state = LadderState(
            market_id="test",
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
        )
        assert hasattr(state, "throttle_heavy_side"), (
            "LadderState is missing 'throttle_heavy_side' field"
        )
        assert state.throttle_heavy_side is None

    def test_ladder_state_throttle_heavy_side_can_be_set(self):
        """throttle_heavy_side can be set to Side.UP or Side.DOWN."""
        state = LadderState(
            market_id="test",
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
        )
        state.throttle_heavy_side = Side.UP
        assert state.throttle_heavy_side == Side.UP

        state.throttle_heavy_side = Side.DOWN
        assert state.throttle_heavy_side == Side.DOWN

        state.throttle_heavy_side = None
        assert state.throttle_heavy_side is None


# ─── Test 2: check_imbalance sets throttle when both sides have fills ────────

class TestCheckImbalanceSetsThrottle:
    def test_throttle_set_when_imbalance_exceeds_0_30_with_both_sides_filled(
        self, cfg, mock_clob, market
    ):
        """When UP=172, DN=77 (imbalance=0.55) and both sides have fills,
        check_imbalance should set throttle_heavy_side = Side.UP."""
        mgr = _make_manager(cfg, mock_clob)
        mid = market.market_id

        # Manually create ladder state
        mgr.ladders[mid] = LadderState(
            market_id=mid,
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
            timeframe_sec=3600,
        )

        # Simulate: 18 UP fills (172 total qty) and 8 DN fills (77 total qty)
        # imbalance = (172 - 77) / 172 ≈ 0.552  (> 0.30)
        # Both light_count > 0 (8 DN fills) — the old guard would NOT fire
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 18, price=0.45, size=9.56)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 8, price=0.55, size=9.625)

        mgr.check_imbalance(now_epoch=2000)

        state = mgr.ladders[mid]
        assert state.throttle_heavy_side == Side.UP, (
            f"Expected throttle_heavy_side=Side.UP but got {state.throttle_heavy_side}"
        )

    def test_throttle_set_on_dn_heavy_side(self, cfg, mock_clob, market):
        """When DN is heavier (DN=172, UP=77), throttle should be set to DN."""
        mgr = _make_manager(cfg, mock_clob)
        mid = market.market_id

        mgr.ladders[mid] = LadderState(
            market_id=mid,
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
            timeframe_sec=3600,
        )

        # DN fills are heavier: 18 DN vs 8 UP
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 18, price=0.55, size=9.56)
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 8, price=0.45, size=9.625)

        mgr.check_imbalance(now_epoch=2000)

        state = mgr.ladders[mid]
        assert state.throttle_heavy_side == Side.DOWN, (
            f"Expected throttle_heavy_side=Side.DOWN but got {state.throttle_heavy_side}"
        )

    def test_throttle_not_set_when_imbalance_below_0_30(self, cfg, mock_clob, market):
        """When imbalance is < 0.30 (e.g. UP=110, DN=90), no throttle should fire."""
        mgr = _make_manager(cfg, mock_clob)
        mid = market.market_id

        mgr.ladders[mid] = LadderState(
            market_id=mid,
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
            timeframe_sec=3600,
        )

        # imbalance = (110-90) / 110 ≈ 0.18 < 0.30
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 11, price=0.45, size=10.0)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 9, price=0.55, size=10.0)

        mgr.check_imbalance(now_epoch=2000)

        state = mgr.ladders[mid]
        assert state.throttle_heavy_side is None, (
            f"Expected no throttle but got {state.throttle_heavy_side}"
        )

    def test_throttle_not_set_when_light_count_is_zero(self, cfg, mock_clob, market):
        """When light_count == 0, the existing mechanisms handle it (heavy-side lock or
        ONE-SIDED ABORT kill). The throttle guard should NOT fire in this case —
        it only fires when both sides have fills (light_count > 0).

        We use a fresh market with a short window so the ONE-SIDED ABORT does NOT
        fire (posted_at is recent, elapsed < 25%), and verify throttle stays None.
        """
        mgr = _make_manager(cfg, mock_clob)
        mid = market.market_id

        # Use now_epoch close to posted_at so the 25%-elapsed abort doesn't fire
        now_epoch = 1100  # only 100s elapsed on 3600s window (2.8% < 25%)
        mgr.ladders[mid] = LadderState(
            market_id=mid,
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,  # just posted
            timeframe_sec=3600,
        )

        # 10 UP fills, 0 DN fills — light_count == 0
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 10, price=0.45, size=10.0)

        mgr.check_imbalance(now_epoch=now_epoch)

        # Market should still exist (no abort yet — too early in window)
        assert mid in mgr.ladders, "Ladder should not be killed this early in window"
        state = mgr.ladders[mid]
        # Throttle should NOT be set (the existing mechanism handles this case)
        assert state.throttle_heavy_side is None, (
            "throttle_heavy_side should not be set when light_count == 0 (old guard handles it)"
        )

    def test_throttle_logs_when_set(self, cfg, mock_clob, market, caplog):
        """check_imbalance should log when the throttle fires."""
        mgr = _make_manager(cfg, mock_clob)
        mid = market.market_id

        mgr.ladders[mid] = LadderState(
            market_id=mid,
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
            timeframe_sec=3600,
        )

        # Simulate the real scenario: 172 UP vs 77 DN
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 18, price=0.45, size=9.56)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 8, price=0.55, size=9.625)

        with caplog.at_level(logging.INFO):
            mgr.check_imbalance(now_epoch=2000)

        assert any("THROTTLE" in r.message for r in caplog.records), (
            "Expected a log message containing 'THROTTLE' when throttle fires"
        )


# ─── Test 3: throttle clears with hysteresis ────────────────────────────────

class TestThrottleHysteresis:
    def test_throttle_clears_when_imbalance_drops_below_0_25(
        self, cfg, mock_clob, market
    ):
        """Once throttle is set, it should clear when imbalance drops below 0.25."""
        mgr = _make_manager(cfg, mock_clob)
        mid = market.market_id

        mgr.ladders[mid] = LadderState(
            market_id=mid,
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
            timeframe_sec=3600,
        )

        # First: set the throttle with 55% imbalance
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 18, price=0.45, size=9.56)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 8, price=0.55, size=9.625)
        mgr.check_imbalance(now_epoch=2000)
        assert mgr.ladders[mid].throttle_heavy_side == Side.UP

        # Now simulate recovery: add more DN fills so imbalance drops below 0.25
        # UP total: ~172, DN needs to be > 172 * 0.75 = 129
        # Already at 77, add 60 more => 137 DN
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 6, price=0.55, size=10.0)

        mgr.check_imbalance(now_epoch=2100)

        state = mgr.ladders[mid]
        assert state.throttle_heavy_side is None, (
            f"Expected throttle cleared after recovery, but got {state.throttle_heavy_side}"
        )

    def test_throttle_stays_set_when_imbalance_between_0_25_and_0_30(
        self, cfg, mock_clob, market
    ):
        """Hysteresis zone: throttle was set, imbalance is 0.27 (between 0.25 and 0.30).
        The throttle should REMAIN set (hysteresis prevents toggling)."""
        mgr = _make_manager(cfg, mock_clob)
        mid = market.market_id

        mgr.ladders[mid] = LadderState(
            market_id=mid,
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
            timeframe_sec=3600,
            throttle_heavy_side=Side.UP,  # pre-set throttle
        )

        # Imbalance: UP=100, DN=73 => imbalance = 27/100 = 0.27 (between 0.25 and 0.30)
        # Use 10 UP fills * 10.0 = 100 UP qty
        # Use 8 DN fills * 9.125 = 73 DN qty
        _add_filled_orders(mgr.tracker, mid, Side.UP, "tok_up", 10, price=0.45, size=10.0)
        _add_filled_orders(mgr.tracker, mid, Side.DOWN, "tok_dn", 8, price=0.55, size=9.125)

        mgr.check_imbalance(now_epoch=2000)

        state = mgr.ladders[mid]
        assert state.throttle_heavy_side == Side.UP, (
            "Throttle should stay set in hysteresis zone (0.25 < imbalance < 0.30)"
        )


# ─── Test 4: post_ladder halves heavy-side budget when throttle is active ───

def _capture_post_budgets(mgr, mkt):
    """Post a ladder and capture the UP/DN budgets from the LADDER POSTED log line.

    Returns (budget_up, budget_dn) or (None, None) if the log couldn't be parsed.
    """
    import io, re, logging as _logging

    logger_name = "polybot.strategy.ladder_manager"
    buf = io.StringIO()
    handler = _logging.StreamHandler(buf)
    handler.setLevel(_logging.DEBUG)
    log = _logging.getLogger(logger_name)
    saved_level = log.level
    log.setLevel(_logging.DEBUG)
    log.addHandler(handler)
    try:
        mgr.post_ladder(mkt)
    finally:
        log.removeHandler(handler)
        log.setLevel(saved_level)

    output = buf.getvalue()
    # Format: "LADDER POSTED: ... (UP=$200 DN=$200)"
    m = re.search(r"\(UP=\$?(\d+(?:\.\d+)?)\s+DN=\$?(\d+(?:\.\d+)?)\)", output)
    if m is None:
        return None, None, output
    return float(m.group(1)), float(m.group(2)), output


class TestPostLadderThrottledBudget:
    def test_post_ladder_halves_up_budget_when_up_throttled(
        self, cfg, mock_clob, market
    ):
        """When throttle_heavy_side=UP, post_ladder should use half the budget for UP.

        We pre-inject a LadderState with throttle_heavy_side=UP before calling
        post_ladder on the same market. post_ladder reads the existing state's
        throttle_heavy_side field before computing rungs.
        """
        # --- Normal post (no throttle) for baseline ---
        mgr_normal = _make_manager(cfg, mock_clob, bankroll=10000.0)
        mkt_normal = MarketWindow(
            market_id="btc-1h-baseline",
            condition_id="0xbase",
            asset="BTC",
            timeframe_sec=3600,
            up_token_id="base_up",
            dn_token_id="base_dn",
            open_epoch=1000,
            close_epoch=4600,
        )
        budget_up_normal, budget_dn_normal, log_normal = _capture_post_budgets(
            mgr_normal, mkt_normal
        )
        assert budget_up_normal is not None, f"No budget in log: {log_normal!r}"

        # --- Throttled post: pre-inject state with throttle_heavy_side=UP ---
        mgr_throttled = _make_manager(cfg, mock_clob, bankroll=10000.0)
        mkt_throttled = MarketWindow(
            market_id="btc-1h-throttled",
            condition_id="0xthr",
            asset="BTC",
            timeframe_sec=3600,
            up_token_id="thr_up",
            dn_token_id="thr_dn",
            open_epoch=1000,
            close_epoch=4600,
        )
        # Inject a LadderState with throttle set BEFORE post_ladder runs
        mgr_throttled.ladders["btc-1h-throttled"] = LadderState(
            market_id="btc-1h-throttled",
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
            timeframe_sec=3600,
            throttle_heavy_side=Side.UP,
            up_token_id="thr_up",
            dn_token_id="thr_dn",
        )
        # post_ladder skips if position has fills — we haven't set any, so it runs
        # But post_ladder also skips if position has fills (pos.up_qty > 0 or pos.dn_qty > 0)
        # We just need to ensure no position fills are present, which is the default

        budget_up_throttled, budget_dn_throttled, log_throttled = _capture_post_budgets(
            mgr_throttled, mkt_throttled
        )
        assert budget_up_throttled is not None, f"No budget in throttled log: {log_throttled!r}"

        # UP budget should be approximately halved (within 15% tolerance for rounding)
        expected_up = budget_up_normal * 0.5
        assert abs(budget_up_throttled - expected_up) / max(expected_up, 1.0) < 0.15, (
            f"Expected UP budget ~${expected_up:.1f} (half of ${budget_up_normal:.1f}) "
            f"but got ${budget_up_throttled:.1f} in throttled mode"
        )

        # DN budget should be roughly unchanged — throttle only halves heavy (UP) side
        assert budget_dn_throttled >= budget_dn_normal * 0.70, (
            f"Expected DN budget >= ${budget_dn_normal * 0.70:.1f} "
            f"but got ${budget_dn_throttled:.1f} (throttle should not reduce DN)"
        )

    def test_post_ladder_halves_dn_budget_when_dn_throttled(
        self, cfg, mock_clob, market
    ):
        """When throttle_heavy_side=DOWN, post_ladder should use half the budget for DN."""
        # Baseline (no throttle)
        mgr_normal = _make_manager(cfg, mock_clob, bankroll=10000.0)
        mkt_normal = MarketWindow(
            market_id="btc-1h-baseline2",
            condition_id="0xbase2",
            asset="BTC",
            timeframe_sec=3600,
            up_token_id="base2_up",
            dn_token_id="base2_dn",
            open_epoch=1000,
            close_epoch=4600,
        )
        budget_up_normal, budget_dn_normal, log_normal = _capture_post_budgets(
            mgr_normal, mkt_normal
        )
        assert budget_dn_normal is not None, f"No budget in log: {log_normal!r}"

        # Throttled: DN is heavy
        mgr_throttled = _make_manager(cfg, mock_clob, bankroll=10000.0)
        mkt_throttled = MarketWindow(
            market_id="btc-1h-thr2",
            condition_id="0xthr2",
            asset="BTC",
            timeframe_sec=3600,
            up_token_id="thr2_up",
            dn_token_id="thr2_dn",
            open_epoch=1000,
            close_epoch=4600,
        )
        mgr_throttled.ladders["btc-1h-thr2"] = LadderState(
            market_id="btc-1h-thr2",
            asset="BTC",
            anchor_up=0.45,
            anchor_dn=0.55,
            posted_at=1000.0,
            timeframe_sec=3600,
            throttle_heavy_side=Side.DOWN,
            up_token_id="thr2_up",
            dn_token_id="thr2_dn",
        )

        budget_up_throttled, budget_dn_throttled, log_throttled = _capture_post_budgets(
            mgr_throttled, mkt_throttled
        )
        assert budget_dn_throttled is not None, f"No budget in throttled log: {log_throttled!r}"

        # DN budget should be approximately halved
        expected_dn = budget_dn_normal * 0.5
        assert abs(budget_dn_throttled - expected_dn) / max(expected_dn, 1.0) < 0.15, (
            f"Expected DN budget ~${expected_dn:.1f} (half of ${budget_dn_normal:.1f}) "
            f"but got ${budget_dn_throttled:.1f} in throttled mode"
        )

        # UP budget should be roughly unchanged
        assert budget_up_throttled >= budget_up_normal * 0.70, (
            f"Expected UP budget >= ${budget_up_normal * 0.70:.1f} "
            f"but got ${budget_up_throttled:.1f} (throttle should not reduce UP)"
        )

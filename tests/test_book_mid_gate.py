"""Regression tests for the book-mid entry gate (Improvement #1).

The gate reads CLOB book midpoint at window open; when both sides have
liquid/tight books AND the normalized mid-based certainty >= threshold, it
skips the losing side and caps the winning-side budget at directional_budget_cap.

Orthogonal to fv_gate_enabled (that gate uses Binance-derived fair_up).
Holdout-validated 2026-04-17 on 212-market out-of-sample Dome dataset.
"""
import logging
import time
from unittest.mock import MagicMock

from polybot.config import BotConfig
from polybot.strategy.ladder_manager import LadderManager
from polybot.strategy.position_manager import PositionManager
from polybot.types import MarketWindow


def _cfg(**overrides):
    defaults = dict(
        dry_run=True, bankroll=500,
        ladder_rungs=10, ladder_spacing=0.01, ladder_width=0.20,
        ladder_size_skew=1.0,
        # Relax pair-cost guard so it doesn't interfere with the gate signal —
        # these tests use extreme mids (e.g. 0.85/0.15) to isolate the gate.
        max_pair_cost=1.05, max_pair_cost_1h=1.05,
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
        fv_gate_enabled=False,  # Binance-FV gate OFF — isolate book-mid
        directional_budget_cap=20.0,
        book_mid_gate_enabled=True,
        book_mid_gate_certainty_threshold=0.65,
        book_mid_gate_max_spread=0.05,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _market():
    now = int(time.time())
    return MarketWindow(
        market_id="btc-15m-bmg", condition_id="0xabc", asset="BTC",
        timeframe_sec=900, up_token_id="tok_up", dn_token_id="tok_dn",
        open_epoch=now - 60, close_epoch=now + 840,
    )


def _make_manager(cfg=None, up_mid=0.50, dn_mid=0.50,
                  up_bid=0.49, up_ask=0.51,
                  dn_bid=0.49, dn_ask=0.51):
    if cfg is None:
        cfg = _cfg()
    executor = MagicMock()

    def _mid(token_id):
        if token_id == "tok_up":
            return up_mid
        if token_id == "tok_dn":
            return dn_mid
        return None

    def _bid(token_id):
        if token_id == "tok_up":
            return up_bid
        if token_id == "tok_dn":
            return dn_bid
        return None

    def _ask(token_id):
        if token_id == "tok_up":
            return up_ask
        if token_id == "tok_dn":
            return dn_ask
        return None

    executor.get_midpoint.side_effect = _mid
    executor.get_best_bid.side_effect = _bid
    executor.get_best_ask.side_effect = _ask
    executor.get_open_orders.return_value = []
    executor.place_batch_limit_buys.return_value = []
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

    pm = PositionManager(cfg, cfg.bankroll)
    risk = MagicMock()
    risk.is_halted.return_value = False
    risk.can_open_position.return_value = True
    risk.exposure_factor.return_value = 1.0

    tick_cache = MagicMock()
    tick_cache.get_tick_size.return_value = 0.01

    return LadderManager(cfg, executor, tracker, pm, risk, tick_cache)


def _posted_tokens(lm):
    tokens = []
    for call in lm.executor.place_batch_limit_buys.call_args_list:
        orders = call.args[0] if call.args else call.kwargs.get("orders", [])
        for o in orders:
            tokens.append(o.get("token_id"))
    return tokens


class TestBookMidGate:

    def test_gate_fires_above_threshold(self):
        """up_mid=0.85 dn_mid=0.15 -> normalized=0.85 -> cert=0.70 >= 0.65 -> MUST fire.
        DN side must not be posted."""
        lm = _make_manager(up_mid=0.85, dn_mid=0.15,
                           up_bid=0.84, up_ask=0.86,
                           dn_bid=0.14, dn_ask=0.16)
        lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)
        tokens = _posted_tokens(lm)
        assert "tok_dn" not in tokens, "DN must be skipped when book-mid gate fires UP"

    def test_gate_does_not_fire_below_threshold(self):
        """up_mid=0.70 dn_mid=0.30 -> normalized=0.70 -> cert=0.40 < 0.65 -> no fire.
        Both sides must post."""
        lm = _make_manager(up_mid=0.70, dn_mid=0.30,
                           up_bid=0.69, up_ask=0.71,
                           dn_bid=0.29, dn_ask=0.31)
        lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)
        tokens = _posted_tokens(lm)
        assert "tok_up" in tokens and "tok_dn" in tokens

    def test_gate_blocks_when_spread_too_wide(self):
        """Even with high certainty, if UP spread > 0.05 the gate must not fire."""
        lm = _make_manager(up_mid=0.90, dn_mid=0.10,
                           up_bid=0.85, up_ask=0.95,  # spread = 0.10
                           dn_bid=0.09, dn_ask=0.11)
        lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)
        tokens = _posted_tokens(lm)
        assert "tok_up" in tokens and "tok_dn" in tokens, \
            "Wide UP spread must suppress gate — both sides still post"

    def test_gate_blocks_when_midpoint_none(self):
        """If get_midpoint returns None for either side, gate must not fire."""
        cfg = _cfg()
        lm = _make_manager(cfg)
        # Override to return None for UP while keeping valid DN data
        lm.executor.get_midpoint.side_effect = lambda t: None if t == "tok_up" else 0.10

        lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)
        tokens = _posted_tokens(lm)
        assert "tok_up" in tokens and "tok_dn" in tokens

    def test_gate_disabled_never_fires(self):
        """When book_mid_gate_enabled=False, high certainty must NOT skip any side."""
        lm = _make_manager(_cfg(book_mid_gate_enabled=False),
                           up_mid=0.95, dn_mid=0.05,
                           up_bid=0.94, up_ask=0.96,
                           dn_bid=0.04, dn_ask=0.06)
        lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)
        tokens = _posted_tokens(lm)
        assert "tok_up" in tokens and "tok_dn" in tokens, \
            "Gate disabled => both sides post regardless of certainty"

    def test_gate_caps_directional_budget(self):
        """When gate fires, UP budget is capped at directional_budget_cap ($20).
        Use position_size_fraction=0.40 on $500 bankroll so uncapped budget ~=$200."""
        lm = _make_manager(
            _cfg(directional_budget_cap=20.0, position_size_fraction=0.40),
            up_mid=0.90, dn_mid=0.10,
            up_bid=0.89, up_ask=0.91,
            dn_bid=0.09, dn_ask=0.11,
        )
        lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)
        calls = lm.executor.place_batch_limit_buys.call_args_list
        # Sum of size*price for all UP-token orders must not exceed the cap
        up_notional = 0.0
        for call in calls:
            orders = call.args[0] if call.args else call.kwargs.get("orders", [])
            for o in orders:
                if o.get("token_id") == "tok_up":
                    up_notional += float(o.get("price", 0.0)) * float(o.get("size", 0.0))
        # Ladder builder rounds sizes to integer shares, so notional may be slightly
        # above budget. Allow a small tolerance (5%) — the key check is that we
        # are nowhere near the uncapped $200 budget from position_size_fraction=0.40.
        assert up_notional <= 20.0 * 1.05, \
            f"UP notional ${up_notional:.2f} exceeded directional cap $20 (+5% tol)"


class TestBookMidGateInstrumentation:
    """Cycle 19: verify non-fires are categorized into 3 distinguishable tags."""

    LOGGER_NAME = "polybot.strategy.ladder_manager"

    def test_non_fire_missing_bid_ask_logged(self, caplog):
        """Missing bid on UP -> reason=missing_bid_ask."""
        lm = _make_manager(up_mid=0.85, dn_mid=0.15,
                           up_bid=0.84, up_ask=0.86,
                           dn_bid=0.14, dn_ask=0.16)
        # Override UP bid to None
        lm.executor.get_best_bid.side_effect = lambda t: None if t == "tok_up" else 0.14

        with caplog.at_level(logging.DEBUG, logger=self.LOGGER_NAME):
            lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)

        skip_msgs = [r.getMessage() for r in caplog.records if "BOOK MID GATE SKIP" in r.getMessage()]
        assert any("reason=missing_bid_ask" in m for m in skip_msgs), \
            f"Expected reason=missing_bid_ask in logs, got: {skip_msgs}"

    def test_non_fire_spread_too_wide_logged(self, caplog):
        """Wide UP spread + high certainty -> reason=spread_too_wide."""
        lm = _make_manager(up_mid=0.90, dn_mid=0.10,
                           up_bid=0.85, up_ask=0.95,  # spread=0.10 > 0.05
                           dn_bid=0.09, dn_ask=0.11)

        with caplog.at_level(logging.DEBUG, logger=self.LOGGER_NAME):
            lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)

        skip_msgs = [r.getMessage() for r in caplog.records if "BOOK MID GATE SKIP" in r.getMessage()]
        assert any("reason=spread_too_wide" in m for m in skip_msgs), \
            f"Expected reason=spread_too_wide in logs, got: {skip_msgs}"

    def test_non_fire_certainty_too_low_logged(self, caplog):
        """Good data, tight spreads, but cert=0.20 < 0.65 -> reason=certainty_too_low."""
        # up_mid=0.60, dn_mid=0.40 -> book_mid_up=0.60 -> cert = 2*|0.60-0.50| = 0.20
        lm = _make_manager(up_mid=0.60, dn_mid=0.40,
                           up_bid=0.59, up_ask=0.61,
                           dn_bid=0.39, dn_ask=0.41)

        with caplog.at_level(logging.DEBUG, logger=self.LOGGER_NAME):
            lm.post_ladder(_market(), spot_delta=0.0, fair_up=0.50)

        skip_msgs = [r.getMessage() for r in caplog.records if "BOOK MID GATE SKIP" in r.getMessage()]
        assert any("reason=certainty_too_low" in m for m in skip_msgs), \
            f"Expected reason=certainty_too_low in logs, got: {skip_msgs}"
        # Ensure cert value is present (not "None")
        cert_msg = [m for m in skip_msgs if "reason=certainty_too_low" in m][0]
        assert "cert=0.2000" in cert_msg, f"Expected cert=0.2000 in: {cert_msg}"

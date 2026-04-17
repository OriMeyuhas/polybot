"""Tests for live-mode startup safety guards.

Covers:
- Fix A: MAX_PAIR_COST > 1.00 must raise in live mode (paper mode unaffected).
- Fix B: Live mode sources bankroll from on-chain USDC balance; paper mode keeps .env.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from polybot.config import BotConfig, validate_live_config


# ── Fix A: MAX_PAIR_COST live-mode guard ─────────────────────────────────────


class TestMaxPairCostLiveGuard:
    """validate_live_config() must refuse MAX_PAIR_COST > 1.00 in live mode."""

    def _base_cfg(self, **overrides) -> BotConfig:
        """A BotConfig that passes every OTHER live validation, so tests isolate pair-cost."""
        base = dict(
            # Credentials aren't checked by validate_live_config itself (that's run_bot.py),
            # so we only need values that pass the bound checks.
            position_size_fraction=0.05,
            max_daily_drawdown_pct=0.05,
            max_concurrent_positions=8,
            bankroll=500.0,
            batch_order_size=15,
            maker_fee_rate=0.0,
            force_buy_max_pair_cost=0.83,
            boost_elapsed_pct=0.20,
            force_buy_elapsed_pct=0.70,
            skew_phase_pct=0.30,
            directional_phase_pct=0.70,
            certainty_exit_threshold=0.30,
            certainty_hold_threshold=0.95,
            max_budget_skew=0.80,
            spot_delta_reduce_threshold=0.0015,
            spot_delta_skip_threshold=0.005,
            spot_loss_cap_multiplier=0.50,
        )
        base.update(overrides)
        return BotConfig(**base)

    def test_refuses_live_with_max_pair_cost_above_1(self):
        """Live mode must reject MAX_PAIR_COST > 1.00 — guaranteed loss per pair."""
        cfg = self._base_cfg(max_pair_cost=1.05)
        errors = validate_live_config(cfg)
        assert any("MAX_PAIR_COST" in e and "guaranteed loss" in e for e in errors), (
            f"Expected guaranteed-loss error for max_pair_cost=1.05, got: {errors}"
        )

    def test_refuses_live_with_max_pair_cost_just_above_1(self):
        """Even a tiny overshoot (1.001) must be rejected — pair EV is negative."""
        cfg = self._base_cfg(max_pair_cost=1.001)
        errors = validate_live_config(cfg)
        assert any("MAX_PAIR_COST" in e for e in errors), (
            f"Expected MAX_PAIR_COST error at 1.001, got: {errors}"
        )

    def test_allows_live_with_max_pair_cost_at_or_below_1(self):
        """MAX_PAIR_COST=1.00 is allowed (boundary). 0.98 is the shakedown value."""
        cfg_1_00 = self._base_cfg(max_pair_cost=1.00)
        cfg_0_98 = self._base_cfg(max_pair_cost=0.98)
        assert not any("MAX_PAIR_COST" in e for e in validate_live_config(cfg_1_00))
        assert not any("MAX_PAIR_COST" in e for e in validate_live_config(cfg_0_98))

    def test_paper_mode_unaffected_by_high_max_pair_cost(self):
        """Paper mode (dry_run=True) never runs validate_live_config — so backtest
        comparisons using max_pair_cost > 1.00 remain possible."""
        # The guard lives INSIDE validate_live_config; paper never calls it from
        # run_bot.py. This test pins that contract: BotConfig itself accepts >1.00
        # without raising, so paper instantiation is unaffected.
        cfg = self._base_cfg(max_pair_cost=1.05, dry_run=True)
        assert cfg.max_pair_cost == pytest.approx(1.05)
        # And calling validate_live_config IS still the guard, regardless of dry_run:
        # run_bot.py is responsible for only invoking it in the live path.
        errors = validate_live_config(cfg)
        assert any("MAX_PAIR_COST" in e for e in errors)


# ── Fix B: Live-mode bankroll sourced from on-chain USDC balance ─────────────


class _FakePaperBankroll:
    """Marker class to simulate PaperClobClient so _fetch_live_balance takes the
    paper branch. We only care that paper mode does NOT override bankroll."""
    _resting: dict = {}

    def get_balance_allowance(self):
        # If this ever runs it would mean paper mode hit the live-bankroll path.
        # Return a sentinel that would be visibly wrong if used.
        return {"balance": str(int(9_999_999 * 1e6))}


class TestLiveBankrollFromOnChain:
    """In live mode, start() must (1) query USDC balance, (2) override .env BANKROLL,
    (3) log the override, and (4) refuse to start if the query fails or returns 0.
    Paper mode must continue to use .env BANKROLL."""

    def _make_live_bot(self, env_bankroll: float = 500.0):
        """Build a Bot in live mode with all I/O subsystems stubbed.
        Patches create_clob_client so no real CLOB connection is attempted."""
        from polybot.bot import Bot

        cfg = BotConfig(
            dry_run=False,
            bankroll=env_bankroll,
            private_key="0x" + "0" * 64,
            api_key="key",
            api_secret="secret",
            api_passphrase="passphrase",
        )

        # Patch every heavy subsystem that Bot.__init__ builds so we can instantiate
        # cheaply and just exercise the balance-fetch branch inside start().
        with patch("polybot.bot.create_clob_client") as mk_clob, \
             patch("polybot.bot.MultiAssetPriceFeed"), \
             patch("polybot.bot.ClobMidpointPoller"), \
             patch("polybot.bot.MarketWSClient"), \
             patch("polybot.bot.BookManager"), \
             patch("polybot.bot.DataRecorder"), \
             patch("polybot.bot.RTDSChainlinkPriceFeed"), \
             patch("polybot.bot.OrderExecutor") as mk_ox, \
             patch("polybot.bot.Heartbeat"), \
             patch("polybot.bot.TickSizeCache"), \
             patch("polybot.bot.LadderManager"), \
             patch("polybot.bot.Redeemer"):
            # Make clob client a plain mock (no _resting attr → live branch in _fetch_live_balance)
            fake_client = MagicMock()
            if hasattr(fake_client, "_resting"):
                del fake_client._resting
            mk_clob.return_value = fake_client
            mk_ox.return_value = MagicMock()

            bot = Bot(cfg)
            # Make order_executor.cancel_all / get_recent_matched_orders safe to call
            bot.order_executor.cancel_all = MagicMock(return_value=None)
            bot.order_executor.get_recent_matched_orders = MagicMock(return_value=[])
            return bot

    def test_live_uses_onchain_balance_and_ignores_env(self):
        """Successful balance fetch → position_manager.bankroll becomes on-chain value,
        NOT the stale .env BANKROLL."""
        bot = self._make_live_bot(env_bankroll=500.0)  # stale .env value
        onchain_balance_usdc = 137.42  # real wallet has much less than .env says
        raw_micro_usdc = int(onchain_balance_usdc * 1e6)

        with patch.object(bot, "_fetch_live_balance", return_value={"balance": str(raw_micro_usdc)}):
            asyncio.run(bot.start())

        assert bot.position_manager.bankroll == pytest.approx(onchain_balance_usdc), (
            f"Expected bankroll=${onchain_balance_usdc}, got ${bot.position_manager.bankroll}"
        )
        assert bot._wallet_balance == pytest.approx(onchain_balance_usdc)
        # risk manager baseline should also track on-chain, else drawdown math is wrong
        assert bot.risk.starting_bankroll == pytest.approx(onchain_balance_usdc)

    def test_live_refuses_start_when_balance_query_fails(self):
        """If the balance query raises, start() must raise — not silently use .env."""
        bot = self._make_live_bot(env_bankroll=500.0)

        with patch.object(bot, "_fetch_live_balance", side_effect=RuntimeError("CLOB unreachable")):
            with pytest.raises(RuntimeError, match="balance fetch failed"):
                asyncio.run(bot.start())

    def test_live_refuses_start_when_balance_is_zero(self):
        """$0 on-chain balance → wallet not funded → refuse to proceed."""
        bot = self._make_live_bot(env_bankroll=500.0)

        with patch.object(bot, "_fetch_live_balance", return_value={"balance": "0"}):
            with pytest.raises(RuntimeError, match="wallet not funded"):
                asyncio.run(bot.start())

    def test_live_refuses_start_when_balance_malformed(self):
        """Missing 'balance' key → malformed response → refuse to proceed."""
        bot = self._make_live_bot(env_bankroll=500.0)

        with patch.object(bot, "_fetch_live_balance", return_value={"unexpected": "shape"}):
            with pytest.raises(RuntimeError, match="malformed"):
                asyncio.run(bot.start())

    def test_paper_mode_uses_env_bankroll_unchanged(self):
        """Paper mode must NOT touch the on-chain path — .env BANKROLL is authoritative."""
        from polybot.bot import Bot

        env_bankroll = 500.0
        cfg = BotConfig(dry_run=True, bankroll=env_bankroll)

        with patch("polybot.bot.create_clob_client"), \
             patch("polybot.bot.MultiAssetPriceFeed"), \
             patch("polybot.bot.ClobMidpointPoller"), \
             patch("polybot.bot.MarketWSClient"), \
             patch("polybot.bot.BookManager"), \
             patch("polybot.bot.DataRecorder"), \
             patch("polybot.bot.RTDSChainlinkPriceFeed"), \
             patch("polybot.bot.OrderExecutor"), \
             patch("polybot.bot.Heartbeat"), \
             patch("polybot.bot.TickSizeCache"), \
             patch("polybot.bot.LadderManager"), \
             patch("polybot.bot.Redeemer"):
            bot = Bot(cfg)

        # Patch fetch_live_balance to a honeypot — if paper ever calls it we'll see
        # the bankroll mutate to this impossible value.
        trap = MagicMock(return_value={"balance": str(int(9_999_999 * 1e6))})
        with patch.object(bot, "_fetch_live_balance", trap):
            asyncio.run(bot.start())

        assert trap.call_count == 0, "Paper mode must not call _fetch_live_balance"
        assert bot.position_manager.bankroll == pytest.approx(env_bankroll), (
            f"Paper bankroll should stay at .env={env_bankroll}, got {bot.position_manager.bankroll}"
        )

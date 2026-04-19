"""Tests for graceful live-mode startup degradation.

Prior behavior: `run_bot.py` called `sys.exit(1)` whenever live credentials were
missing or `validate_live_config` rejected a config field. That killed the
process before the dashboard ever came up — the user's restart just reported
"same error again" with no UI to fix anything.

Current behavior: on any live-mode failure during startup (missing creds,
invalid creds, bad config, client construction exception), `_attempt_live_startup`
builds the bot with a PAPER CLOB client so the dashboard stays up with data
feeds running. A banner in `gui_state.stale_order_alert` tells the user what
went wrong. Strict behavior is preserved for `ui_start_full()` (the explicit
"start trading" intent).
"""

from __future__ import annotations

import dataclasses
import logging
from unittest.mock import patch

import pytest

from polybot.config import BotConfig, load_bot_config
from run_bot import _attempt_live_startup


@pytest.fixture
def live_cfg_base():
    """A BotConfig with dry_run=False, passing validate_live_config but with
    placeholder credentials — individual tests override fields as needed."""
    return BotConfig(
        dry_run=False,
        private_key="0x" + "a" * 64,
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        max_daily_drawdown_pct=0.05,  # within live bounds
        bankroll=100.0,
        # V2 collateral addresses — non-zero so validate_live_config passes
        pusd_address="0x" + "a" * 40,
        usdc_address="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        collateral_onramp_address="0x" + "c" * 40,
    )


def _silent_log() -> logging.Logger:
    log = logging.getLogger("test-live-degraded")
    log.setLevel(logging.CRITICAL)  # suppress warnings in test output
    return log


class TestMissingCredentials:
    def test_missing_private_key_degrades_not_exits(self, live_cfg_base):
        cfg = dataclasses.replace(live_cfg_base, private_key="")
        bot, reason = _attempt_live_startup(cfg, _silent_log())
        assert reason.startswith("credentials_missing")
        assert "PRIVATE_KEY" in reason
        # Bot was built with paper client so dashboard can come up.
        assert hasattr(bot.clob_client, "_resting"), "expected paper CLOB client fallback"
        # Mode display stays "live" so the user sees why they're degraded.
        assert bot.mode == "live"

    def test_missing_api_key_degrades(self, live_cfg_base):
        cfg = dataclasses.replace(live_cfg_base, api_key="")
        bot, reason = _attempt_live_startup(cfg, _silent_log())
        assert "API_KEY" in reason
        assert hasattr(bot.clob_client, "_resting")


class TestConfigValidationFailure:
    def test_max_daily_drawdown_over_limit_degrades(self, live_cfg_base):
        # 0.40 > 0.20 safety ceiling — this is exactly the user's bug A trigger.
        cfg = dataclasses.replace(live_cfg_base, max_daily_drawdown_pct=0.40)
        bot, reason = _attempt_live_startup(cfg, _silent_log())
        assert reason.startswith("config_invalid")
        assert "max_daily_drawdown_pct" in reason
        assert hasattr(bot.clob_client, "_resting")

    def test_position_fraction_over_limit_degrades(self, live_cfg_base):
        cfg = dataclasses.replace(live_cfg_base, position_size_fraction=0.50)
        bot, reason = _attempt_live_startup(cfg, _silent_log())
        assert reason.startswith("config_invalid")
        assert hasattr(bot.clob_client, "_resting")


class TestClientConstructionFailure:
    def test_malformed_private_key_degrades(self, live_cfg_base):
        # A truthy-but-malformed key passes the "missing" check and validate,
        # but blows up inside ClobClient.__init__.
        cfg = dataclasses.replace(live_cfg_base, private_key="not-hex")
        bot, reason = _attempt_live_startup(cfg, _silent_log())
        assert reason.startswith("credentials_invalid")
        assert hasattr(bot.clob_client, "_resting")
        assert bot.mode == "live"


class TestHappyPathReturnsNoReason:
    def test_successful_live_build_returns_empty_reason(self, live_cfg_base):
        # Stub create_clob_client to pretend live construction succeeded.
        sentinel = object()

        def fake_create(cfg, book_manager=None):
            # Return a live-looking object (no `_resting` attr) so the Bot
            # treats it as live for attribute checks.
            class _FakeLive:
                def get_balance_allowance(self, *a, **kw):
                    return {"balance": "100000000", "allowance": "100000000"}
                def get_open_orders(self):
                    return []
                def cancel_all(self):
                    return {"cancelled": 0}
            return _FakeLive()

        with patch("polybot.bot.create_clob_client", side_effect=fake_create):
            bot, reason = _attempt_live_startup(live_cfg_base, _silent_log())
        assert reason == ""
        assert bot.mode == "live"


class TestLoadBotConfigDryRunBankrollField:
    def test_default_dry_run_bankroll(self, monkeypatch):
        # Clear DRY_RUN_BANKROLL so the default fires.
        monkeypatch.delenv("DRY_RUN_BANKROLL", raising=False)
        cfg = load_bot_config()
        assert cfg.dry_run_bankroll == 10000.0

    def test_env_dry_run_bankroll_loaded(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN_BANKROLL", "25000")
        cfg = load_bot_config()
        assert cfg.dry_run_bankroll == 25000.0

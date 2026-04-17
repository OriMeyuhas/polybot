"""Tests for live-mode credential handling on startup.

UX contract (updated 2026-04-17): missing or invalid credentials must NOT
crash the process at startup. The dashboard stays up with data feeds running
and a banner explains what needs fixing. The strict "refuse to trade"
behavior still applies when the user clicks Start in the UI — see
`Bot.ui_start_full()` and test_live_mode_hardening.py.

These tests drive the unit-level helper `_attempt_live_startup` so they don't
need subprocess / stdin tricks. A separate test_dry_run_skips_credential_check
still runs the real process to confirm paper mode is unaffected.
"""

import dataclasses
import logging
import subprocess
import sys
import os

import pytest

from polybot.config import BotConfig
from run_bot import _attempt_live_startup


_FAKE_KEY = "0x" + "ab" * 32
_RUN_BOT = os.path.join(os.path.dirname(__file__), "..", "run_bot.py")


def _silent_log() -> logging.Logger:
    log = logging.getLogger("test-cred-validation")
    log.setLevel(logging.CRITICAL)
    return log


def _live_cfg(**overrides) -> BotConfig:
    base = BotConfig(
        dry_run=False,
        private_key=_FAKE_KEY,
        api_key="test-key",
        api_secret="test-secret",
        api_passphrase="test-pass",
        max_daily_drawdown_pct=0.05,
        bankroll=100.0,
    )
    return dataclasses.replace(base, **overrides)


def test_all_credentials_present_builds_bot():
    """All 4 credentials set and valid-hex — live startup should succeed or
    at worst fail at the SDK boundary, NOT at the missing-cred check."""
    cfg = _live_cfg()
    # We don't actually talk to Polymarket here; the ClobClient constructor
    # does some hex validation that will pass with our 0xab... key. If it
    # accepts the cfg we expect reason="" (happy path). If the SDK rejects
    # for any other reason, we expect credentials_invalid — NOT
    # credentials_missing (the bug this test pinned down).
    _bot, reason = _attempt_live_startup(cfg, _silent_log())
    assert not reason.startswith("credentials_missing"), (
        f"all creds present but got {reason!r}"
    )


def test_missing_api_key_degrades_with_named_cred():
    """Missing API_KEY — banner reason lists API_KEY so the user knows what
    to fix. Process stays alive; bot falls back to paper client."""
    cfg = _live_cfg(api_key="")
    bot, reason = _attempt_live_startup(cfg, _silent_log())
    assert reason.startswith("credentials_missing")
    assert "API_KEY" in reason
    assert hasattr(bot.clob_client, "_resting"), "expected paper fallback client"


def test_missing_api_secret_degrades_with_named_cred():
    cfg = _live_cfg(api_secret="")
    _bot, reason = _attempt_live_startup(cfg, _silent_log())
    assert reason.startswith("credentials_missing")
    assert "API_SECRET" in reason


def test_missing_multiple_credentials_lists_all():
    cfg = _live_cfg(api_key="", api_passphrase="")
    _bot, reason = _attempt_live_startup(cfg, _silent_log())
    assert reason.startswith("credentials_missing")
    assert "API_KEY" in reason
    assert "API_PASSPHRASE" in reason


def test_dry_run_skips_credential_check():
    """No credential check in paper mode — even with missing keys.

    In dry-run mode the bot starts the event loop (no CONFIRM prompt),
    so we just check stderr for the credential error within a short timeout.
    """
    env = os.environ.copy()
    env["DRY_RUN"] = "true"
    env["API_KEY"] = ""
    env["API_SECRET"] = ""
    env["API_PASSPHRASE"] = ""
    env["PRIVATE_KEY"] = ""
    try:
        result = subprocess.run(
            [sys.executable, _RUN_BOT],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        # If it exits, should NOT have credential errors
        assert "Missing credentials" not in result.stderr
    except subprocess.TimeoutExpired:
        # Bot started successfully (didn't exit with credential error) — that's the desired behavior
        pass

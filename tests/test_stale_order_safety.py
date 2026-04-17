"""Tests for stale order safety: pre-flight audit, cancel-all gate, standby heartbeat."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.bot import Bot
from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.oms.clob_client import PaperClobClient
from polybot.oms.order_executor import OrderExecutor


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def paper_cfg():
    return BotConfig(dry_run=True, start_paused=True, bankroll=1000.0)


@pytest.fixture
def live_cfg():
    return BotConfig(dry_run=False, start_paused=True, bankroll=1000.0)


def _make_bot(cfg, mock_clob=None):
    bot = Bot(cfg)
    if mock_clob is not None:
        bot.clob_client = mock_clob
        bot.order_executor.client = mock_clob
    return bot


# ── T1: Stale fills block trading ─────────────────────────────────────


def _live_mock_clob(**overrides):
    """Create a MagicMock that does NOT look like PaperClobClient (no _resting attr)."""
    mock = MagicMock()
    # Remove auto-created _resting so hasattr check identifies it as live client
    del mock._resting
    mock.cancel_all.return_value = {"cancelled": 0}
    mock.get_balance_allowance.return_value = {"balance": "1000000000"}
    mock.get_orders.return_value = []
    for k, v in overrides.items():
        setattr(mock, k, v) if not callable(v) else None
    return mock


@pytest.mark.asyncio
async def test_stale_fill_blocks_trading(live_cfg):
    mock_clob = _live_mock_clob()
    mock_clob.get_orders.return_value = [
        {"orderID": "o1", "status": "MATCHED"},
        {"orderID": "o2", "status": "FILLED"},
    ]
    bot = _make_bot(live_cfg, mock_clob)
    await bot.ui_start_full()
    assert bot._cancel_only_mode is True
    assert bot._cancel_only_reason.startswith("stale_fills")


# ── T2: No stale fills allows trading ────────────────────────────────


@pytest.mark.asyncio
async def test_no_stale_fills_allows_trading(live_cfg):
    mock_clob = _live_mock_clob()
    bot = _make_bot(live_cfg, mock_clob)
    await bot.ui_start_full()
    assert bot._cancel_only_mode is False


# ── T3: cancel_all failure blocks trading ────────────────────────────


@pytest.mark.asyncio
async def test_cancel_all_failure_blocks_trading(live_cfg):
    mock_clob = _live_mock_clob()
    mock_clob.cancel_all.side_effect = ClobApiError("connection refused")
    bot = _make_bot(live_cfg, mock_clob)
    await bot.ui_start_full()
    assert bot._cancel_only_mode is True
    assert bot._cancel_only_reason == "cancel_all_failed"


# ── T4: cancel_all success proceeds ─────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_all_success_proceeds(live_cfg):
    mock_clob = _live_mock_clob()
    bot = _make_bot(live_cfg, mock_clob)
    await bot.ui_start_full()
    assert bot._cancel_only_mode is False


# ── T5: Paper mode skips audit ───────────────────────────────────────


@pytest.mark.asyncio
async def test_paper_mode_skips_audit(paper_cfg):
    bot = _make_bot(paper_cfg)
    # Patch get_recent_matched_orders to track if it's called
    bot.order_executor.get_recent_matched_orders = MagicMock(
        side_effect=AssertionError("should not be called in paper mode")
    )
    await bot.ui_start_full()
    assert bot._cancel_only_mode is False
    bot.order_executor.get_recent_matched_orders.assert_not_called()


# ── T6: Standby starts heartbeat ────────────────────────────────────


@pytest.mark.asyncio
async def test_standby_starts_heartbeat(paper_cfg):
    bot = _make_bot(paper_cfg)

    created_coros = []
    original_create_task = asyncio.create_task

    def tracking_create_task(coro):
        created_coros.append(coro)
        task = original_create_task(coro)
        # Cancel immediately so we don't actually run
        task.cancel()
        return task

    with patch("polybot.bot.asyncio.create_task", side_effect=tracking_create_task):
        try:
            await bot.run_standby()
        except asyncio.CancelledError:
            pass

    # Check that heartbeat.run was among the created coroutines
    coro_names = [c.__qualname__ for c in created_coros if hasattr(c, "__qualname__")]
    assert any("heartbeat" in n.lower() or "Heartbeat" in n for n in coro_names), (
        f"heartbeat.run not found among created tasks: {coro_names}"
    )


# ── T7: Stale alert cleared on retry ────────────────────────────────


@pytest.mark.asyncio
async def test_stale_alert_cleared_on_retry(live_cfg):
    mock_clob = _live_mock_clob()
    bot = _make_bot(live_cfg, mock_clob)
    # Simulate a previous stale alert
    bot.gui_state.update(stale_order_alert="2 stale fills from previous session")
    await bot.ui_start_full()
    state = bot.gui_state.get()
    assert state.get("stale_order_alert") == ""


# ── T8: Paper client returns empty matched orders ────────────────────


def test_get_recent_matched_orders_paper_returns_empty(paper_cfg):
    client = PaperClobClient()
    executor = OrderExecutor(paper_cfg, client)
    result = executor.get_recent_matched_orders()
    assert result == []


# ── T9: get_recent_matched_orders filters correctly ──────────────────


def test_get_recent_matched_orders_filters_correctly():
    mock_client = MagicMock()
    del mock_client._resting  # Ensure it's treated as a live client
    mock_client.get_orders.return_value = [
        {"orderID": "o1", "status": "LIVE"},
        {"orderID": "o2", "status": "MATCHED"},
        {"orderID": "o3", "status": "CANCELLED"},
        {"orderID": "o4", "status": "FILLED"},
        {"orderID": "o5", "status": "matched"},  # lowercase
    ]
    cfg = BotConfig(dry_run=False, bankroll=1000.0)
    executor = OrderExecutor(cfg, mock_client)
    result = executor.get_recent_matched_orders()
    ids = [o["orderID"] for o in result]
    assert "o2" in ids
    assert "o4" in ids
    assert "o5" in ids  # case-insensitive
    assert "o1" not in ids
    assert "o3" not in ids

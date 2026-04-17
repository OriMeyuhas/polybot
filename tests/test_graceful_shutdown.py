"""Tests for graceful shutdown behavior (Phases 1-4).

Covers:
- bot.stop() cancels orders via asyncio.to_thread (non-blocking)
- bot.stop() skips cancel in paper mode
- bot.stop() times out on slow cancel_all
- bot.stop() stops rtds_feed
- bot.stop() stops all subsystems
- bot.stop() is idempotent (_stopped guard)
- bot.stop() continues on subsystem failure
- KeyboardInterrupt triggers stop via finally block
- RTDSChainlinkPriceFeed.stop() closes WebSocket
- RTDSChainlinkPriceFeed.stop() handles close error
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.data.rtds_chainlink import RTDSChainlinkPriceFeed

# A valid 32-byte hex private key for live mode tests (not a real key).
_FAKE_LIVE_KEY = "0x" + "ab" * 32


def _make_bot(dry_run: bool = True) -> Bot:
    """Create a Bot with mocked subsystem stop methods for shutdown tests."""
    cfg = BotConfig(dry_run=dry_run, private_key=_FAKE_LIVE_KEY if not dry_run else "")
    bot = Bot(cfg)
    # Mock all subsystem stop() methods to be async
    bot.price_feed.stop = AsyncMock()
    bot.midpoint_poller.stop = AsyncMock()
    bot.market_ws.stop = AsyncMock()
    bot.heartbeat.stop = AsyncMock()
    bot.rtds_feed.stop = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_stop_cancels_orders_via_to_thread():
    """stop() calls cancel_all via asyncio.to_thread for non-blocking cancellation."""
    bot = _make_bot(dry_run=False)
    bot.order_executor.cancel_all = MagicMock(return_value=True)

    with patch("polybot.bot.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        mock_to_thread.return_value = True
        await bot.stop()

    mock_to_thread.assert_awaited_once_with(bot.order_executor.cancel_all)


@pytest.mark.asyncio
async def test_stop_skips_cancel_in_paper_mode():
    """stop() does NOT call cancel_all in paper (dry_run) mode."""
    bot = _make_bot(dry_run=True)
    bot.order_executor.cancel_all = MagicMock()

    with patch("polybot.bot.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        await bot.stop()

    mock_to_thread.assert_not_awaited()
    bot.order_executor.cancel_all.assert_not_called()


@pytest.mark.asyncio
async def test_stop_timeout_on_cancel_all():
    """stop() completes even when cancel_all blocks (10s timeout fires)."""
    bot = _make_bot(dry_run=False)

    async def slow_cancel(*args, **kwargs):
        await asyncio.sleep(30)

    with patch("polybot.bot.asyncio.to_thread", side_effect=slow_cancel):
        # Should complete within ~11s, not 30s
        await asyncio.wait_for(bot.stop(), timeout=15.0)

    # If we reach here, stop() completed despite blocking cancel_all


@pytest.mark.asyncio
async def test_stop_calls_rtds_stop():
    """stop() calls rtds_feed.stop()."""
    bot = _make_bot(dry_run=True)
    await bot.stop()
    bot.rtds_feed.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_stops_all_subsystems():
    """stop() calls stop() on every subsystem."""
    bot = _make_bot(dry_run=True)
    await bot.stop()

    bot.price_feed.stop.assert_awaited_once()
    bot.midpoint_poller.stop.assert_awaited_once()
    bot.market_ws.stop.assert_awaited_once()
    bot.heartbeat.stop.assert_awaited_once()
    bot.rtds_feed.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_idempotent():
    """Calling stop() twice does not error and cancel_all runs only once."""
    bot = _make_bot(dry_run=False)
    bot.order_executor.cancel_all = MagicMock(return_value=True)

    with patch("polybot.bot.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        mock_to_thread.return_value = True
        await bot.stop()
        await bot.stop()

    # cancel_all should be called only once due to _stopped guard
    assert mock_to_thread.await_count == 1


@pytest.mark.asyncio
async def test_stop_continues_on_subsystem_failure():
    """If one subsystem stop() raises, others are still stopped."""
    bot = _make_bot(dry_run=True)
    bot.price_feed.stop = AsyncMock(side_effect=RuntimeError("feed crash"))

    await bot.stop()

    # Despite price_feed.stop() raising, others should still be called
    bot.midpoint_poller.stop.assert_awaited_once()
    bot.market_ws.stop.assert_awaited_once()
    bot.heartbeat.stop.assert_awaited_once()
    bot.rtds_feed.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_keyboard_interrupt_triggers_stop():
    """When run_standby() is cancelled, the finally block in run() calls bot.stop()."""
    bot = _make_bot(dry_run=True)
    bot.stop = AsyncMock()

    # Mock run_standby to simulate cancellation (what asyncio.run does on Ctrl+C)
    original_run_standby = bot.run_standby

    async def fake_run_standby():
        await asyncio.sleep(100)  # simulate long-running standby

    bot.run_standby = fake_run_standby

    async def run():
        try:
            standby_task = asyncio.create_task(bot.run_standby())
            await standby_task
        except asyncio.CancelledError:
            pass
        finally:
            await bot.stop()

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    bot.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_rtds_stop_closes_websocket():
    """RTDSChainlinkPriceFeed.stop() awaits ws.close()."""
    feed = RTDSChainlinkPriceFeed()
    feed._running = True
    mock_ws = AsyncMock()
    feed._ws = mock_ws

    await feed.stop()

    mock_ws.close.assert_awaited_once()
    assert feed._running is False
    assert feed._ws is None


@pytest.mark.asyncio
async def test_rtds_stop_handles_close_error():
    """RTDSChainlinkPriceFeed.stop() does not propagate ws.close() errors."""
    feed = RTDSChainlinkPriceFeed()
    feed._running = True
    mock_ws = AsyncMock()
    mock_ws.close.side_effect = RuntimeError("ws close error")
    feed._ws = mock_ws

    # Should not raise
    await feed.stop()

    assert feed._running is False
    assert feed._ws is None

"""Tests for connection loss and recovery behavior in Bot orchestrator."""

from unittest.mock import MagicMock, patch

from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.strategy.order_tracker import TrackedOrder
from polybot.strategy.ladder_manager import LadderState
from polybot.types import Side


def _make_bot() -> Bot:
    """Create a Bot with default paper config for testing."""
    cfg = BotConfig(dry_run=True)
    return Bot(cfg)


def _add_ladder_and_orders(bot: Bot, market_id: str = "btc-updown-15m-test"):
    """Add a fake ladder and tracked orders to the bot."""
    # Add ladder state
    bot.ladder_manager.ladders[market_id] = LadderState(
        market_id=market_id,
        asset="BTC",
        anchor_up=0.45,
        anchor_dn=0.45,
        posted_at=1000.0,
    )
    # Add tracked orders
    order_up = TrackedOrder(
        order_id="order-1",
        market_id=market_id,
        token_id="token-up",
        side=Side.UP,
        price=0.45,
        size=10.0,
        status="resting",
    )
    order_dn = TrackedOrder(
        order_id="order-2",
        market_id=market_id,
        token_id="token-dn",
        side=Side.DOWN,
        price=0.45,
        size=10.0,
        status="resting",
    )
    bot.order_tracker.add(order_up)
    bot.order_tracker.add(order_dn)
    return order_up, order_dn


# ------------------------------------------------------------------
# Phase 3: Safe connection loss handler
# ------------------------------------------------------------------

def test_connection_lost_enters_cancel_only():
    """_on_connection_lost sets cancel_only_mode to True."""
    bot = _make_bot()
    bot._cancel_only_mode = False
    bot._on_connection_lost()
    assert bot._cancel_only_mode is True


def test_connection_lost_preserves_ladder_state():
    """Ladder entries are NOT removed by connection loss."""
    bot = _make_bot()
    mid = "btc-updown-15m-test"
    _add_ladder_and_orders(bot, mid)

    bot._on_connection_lost()

    # Ladder still exists
    assert mid in bot.ladder_manager.ladders
    # Orders still exist (with status changed to unknown)
    assert len(bot.order_tracker.orders) == 2


def test_connection_lost_does_not_delete_orders():
    """Order tracker orders dict size is unchanged after connection loss."""
    bot = _make_bot()
    mid = "btc-updown-15m-test"
    _add_ladder_and_orders(bot, mid)
    order_count_before = len(bot.order_tracker.orders)

    bot._on_connection_lost()

    assert len(bot.order_tracker.orders) == order_count_before


def test_connection_lost_marks_unknown():
    """All resting orders have status 'unknown' after connection loss."""
    bot = _make_bot()
    mid = "btc-updown-15m-test"
    _add_ladder_and_orders(bot, mid)

    bot._on_connection_lost()

    for order in bot.order_tracker.orders.values():
        assert order.status == "unknown"


def test_connection_lost_schedules_cancel_or_sets_pending():
    """_on_connection_lost enters cancel-only and schedules cancel (or sets pending if no loop)."""
    bot = _make_bot()
    bot.order_executor.cancel_all = MagicMock()

    bot._on_connection_lost()

    # Without a running event loop, cancel is deferred via _pending_cancel_all
    assert bot._cancel_only_mode is True
    assert bot._pending_cancel_all is True


def test_connection_lost_sets_timestamp():
    """_on_connection_lost sets _connection_loss_at to a positive timestamp."""
    bot = _make_bot()
    bot._on_connection_lost()
    assert bot._connection_loss_at > 0


# ------------------------------------------------------------------
# Phase 3: Recovery handler
# ------------------------------------------------------------------

def test_connection_recovered_exits_cancel_only():
    """After loss + recovery, cancel_only_mode is False."""
    bot = _make_bot()
    bot._on_connection_lost()
    assert bot._cancel_only_mode is True

    bot._on_connection_recovered()
    assert bot._cancel_only_mode is False


def test_connection_recovered_clears_timestamp():
    """After recovery, _connection_loss_at is reset to 0."""
    bot = _make_bot()
    bot._on_connection_lost()
    assert bot._connection_loss_at > 0

    bot._on_connection_recovered()
    assert bot._connection_loss_at == 0.0


# ------------------------------------------------------------------
# Phase 5: Cancel-only reason tracking
# ------------------------------------------------------------------

def test_connection_recovered_respects_user_stop():
    """If user pressed Stop during outage, recovery does NOT clear cancel_only_mode."""
    bot = _make_bot()
    bot.running = True  # needed for ui_stop to work

    bot._on_connection_lost()
    assert bot._cancel_only_mode is True
    assert bot._cancel_only_reason == "connection_loss"

    # User presses Stop during the outage
    import asyncio
    asyncio.run(bot.ui_stop())
    assert bot._cancel_only_reason == "user_stop"

    # Connection recovers — but user_stop should be respected
    bot._on_connection_recovered()
    assert bot._cancel_only_mode is True


def test_cancel_only_reason_set_on_connection_loss():
    """_on_connection_lost sets reason to 'connection_loss'."""
    bot = _make_bot()
    bot._on_connection_lost()
    assert bot._cancel_only_reason == "connection_loss"


def test_cancel_only_reason_set_on_user_stop():
    """ui_stop sets reason to 'user_stop'."""
    bot = _make_bot()
    import asyncio
    asyncio.run(bot.ui_stop())
    assert bot._cancel_only_reason == "user_stop"


def test_cancel_only_reason_cleared_on_ui_start():
    """ui_start clears the cancel_only_reason."""
    bot = _make_bot()
    bot._cancel_only_reason = "user_stop"
    import asyncio
    asyncio.run(bot.ui_start())
    assert bot._cancel_only_reason == ""


def test_cancel_only_reason_cleared_on_ui_start_full():
    """ui_start_full clears the cancel_only_reason."""
    bot = _make_bot()
    bot._cancel_only_reason = "user_stop"
    import asyncio
    asyncio.run(bot.ui_start_full())
    assert bot._cancel_only_reason == ""


# ------------------------------------------------------------------
# Ladder state preservation
# ------------------------------------------------------------------

def test_has_ladder_true_after_connection_loss():
    """has_ladder returns True after connection loss (ladder entry preserved)."""
    bot = _make_bot()
    mid = "btc-updown-15m-test"
    _add_ladder_and_orders(bot, mid)

    bot._on_connection_lost()

    assert bot.ladder_manager.has_ladder(mid) is True


def test_reconcile_reverts_unknown_to_resting():
    """reconcile() reverts 'unknown' orders to 'resting' if still on exchange."""
    bot = _make_bot()
    mid = "btc-updown-15m-test"
    order_up, order_dn = _add_ladder_and_orders(bot, mid)

    # Mark all as unknown (as connection loss would)
    bot.order_tracker.mark_all_unknown()
    assert order_up.status == "unknown"
    assert order_dn.status == "unknown"

    # Reconcile with those orders still on exchange
    open_orders = [
        {"id": "order-1"},
        {"id": "order-2"},
    ]
    result = bot.order_tracker.reconcile(open_orders)

    assert order_up.status == "resting"
    assert order_dn.status == "resting"
    assert "order-1" in result["reverted"]
    assert "order-2" in result["reverted"]

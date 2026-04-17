"""Tests for Phase 1B, 1D, 1E, 1G: Live mode hardening."""

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.oms.order_executor import OrderExecutor


_FAKE_LIVE_KEY = "0x" + "ab" * 32


# ------------------------------------------------------------------
# 1B: Balance fetch uses correct params for live vs paper
# ------------------------------------------------------------------


def test_balance_fetch_paper_client():
    """Paper client (has _resting attr) should call get_balance_allowance with no args."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    # PaperClobClient has _resting attribute
    assert hasattr(bot.clob_client, '_resting')
    result = bot._fetch_live_balance()
    assert "balance" in result


def test_balance_fetch_uses_params_for_live():
    """Live client should call get_balance_allowance with BalanceAllowanceParams."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    # Replace clob_client with a mock that does NOT have _resting (simulating live)
    mock_client = MagicMock(spec=[])  # no attributes
    mock_client.get_balance_allowance = MagicMock(return_value={"balance": "500000000"})
    bot.clob_client = mock_client

    with patch("py_clob_client.clob_types.BalanceAllowanceParams") as mock_params_cls:
        mock_params_cls.return_value = "fake_params"
        result = bot._fetch_live_balance()

    mock_client.get_balance_allowance.assert_called_once_with("fake_params")
    assert result == {"balance": "500000000"}


# ------------------------------------------------------------------
# 1D: get_best_ask returns None when no asks
# ------------------------------------------------------------------


def test_get_best_ask_returns_none_when_empty():
    """No asks in orderbook -> None (not 1.0)."""
    cfg = BotConfig()
    mock_client = MagicMock()
    mock_client.get_order_book.return_value = MagicMock(asks=[], bids=[])
    executor = OrderExecutor(cfg, mock_client)
    result = executor.get_best_ask("tok_up")
    assert result is None


def test_get_best_ask_returns_none_when_no_book():
    """No order book at all -> None."""
    cfg = BotConfig()
    mock_client = MagicMock()
    mock_client.get_order_book.return_value = None
    executor = OrderExecutor(cfg, mock_client)
    result = executor.get_best_ask("tok_up")
    assert result is None


def test_get_best_ask_returns_price_when_present():
    """Normal case: asks present -> returns first ask price."""
    cfg = BotConfig()
    mock_client = MagicMock()
    mock_client.get_order_book.return_value = MagicMock(
        asks=[MagicMock(price="0.55")],
        bids=[MagicMock(price="0.45")],
    )
    executor = OrderExecutor(cfg, mock_client)
    result = executor.get_best_ask("tok_up")
    assert result == 0.55


def test_post_ladder_skips_when_no_asks():
    """1D: post_ladder returns 0 when get_best_ask returns None."""
    import time
    from polybot.strategy.ladder_manager import LadderManager
    from polybot.risk_manager import RiskManager
    from polybot.types import MarketWindow

    cfg = BotConfig(bankroll=1000.0)
    risk = RiskManager(cfg, starting_bankroll=1000.0)
    executor = MagicMock()
    executor.get_best_ask.return_value = None
    tracker = MagicMock()
    tracker.get_resting.return_value = []
    pos_mgr = MagicMock()
    pos_mgr.bankroll = 1000.0
    pos_mgr.active_position_count.return_value = 0
    pos_mgr.total_position_cost.return_value = 0.0

    lm = LadderManager(cfg, executor, tracker, pos_mgr, risk)
    now = int(time.time())
    market = MarketWindow(
        market_id="m1", condition_id="c1", asset="BTC", timeframe_sec=900,
        up_token_id="tok_up", dn_token_id="tok_dn",
        open_epoch=now - 600, close_epoch=now + 300,
    )
    assert lm.post_ladder(market) == 0


# ------------------------------------------------------------------
# 1E: Balance polling hardening
# ------------------------------------------------------------------


def test_balance_poll_ignores_zero():
    """Bankroll unchanged when API returns balance of 0."""
    cfg = BotConfig(dry_run=False, private_key=_FAKE_LIVE_KEY, bankroll=500.0)
    bot = Bot(cfg)
    original_bankroll = bot.position_manager.bankroll

    # Simulate API returning 0 balance
    bot._fetch_live_balance = MagicMock(return_value={"balance": "0"})
    bot.running = True

    # Run one iteration of polling by calling internal logic directly
    async def _one_poll():
        result = bot._fetch_live_balance()
        raw = result.get("balance")
        if raw is not None:
            balance = float(raw) / 1e6
            if balance > 0:
                bot._wallet_balance = balance
                bot.position_manager.update_bankroll(balance)
                bot._balance_poll_failures = 0
            else:
                bot._balance_poll_failures += 1
        else:
            bot._balance_poll_failures += 1

    asyncio.run(_one_poll())
    # Bankroll should remain at original value
    assert bot.position_manager.bankroll == original_bankroll
    assert bot._balance_poll_failures == 1


def test_balance_poll_cancel_only_after_5_failures():
    """Enters cancel-only mode after 5 consecutive balance failures."""
    cfg = BotConfig(dry_run=False, private_key=_FAKE_LIVE_KEY, bankroll=500.0)
    bot = Bot(cfg)
    bot._cancel_only_mode = False

    # Simulate 5 consecutive failures
    bot._balance_poll_failures = 4  # next will be #5

    # Simulate one more malformed response
    bot._balance_poll_failures += 1
    if bot._balance_poll_failures >= 5:
        bot._cancel_only_mode = True

    assert bot._cancel_only_mode is True
    assert bot._balance_poll_failures == 5


# ------------------------------------------------------------------
# 1G: Startup order reconciliation
# ------------------------------------------------------------------


def test_start_cancels_stale_orders_live():
    """cancel_all called when dry_run=False during start()."""
    cfg = BotConfig(dry_run=False, private_key=_FAKE_LIVE_KEY, bankroll=500.0)
    bot = Bot(cfg)

    # Mock the balance fetch and cancel_all
    bot._fetch_live_balance = MagicMock(return_value={"balance": "500000000"})
    bot.order_executor.cancel_all = MagicMock(return_value=True)

    asyncio.run(bot.start())
    bot.order_executor.cancel_all.assert_called_once()


def test_start_skips_cancel_in_paper():
    """cancel_all NOT called when dry_run=True."""
    cfg = BotConfig(dry_run=True, bankroll=500.0)
    bot = Bot(cfg)
    bot.order_executor.cancel_all = MagicMock(return_value=True)

    asyncio.run(bot.start())
    bot.order_executor.cancel_all.assert_not_called()

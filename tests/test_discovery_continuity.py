import asyncio
from unittest.mock import patch, AsyncMock
from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.types import MarketWindow


def test_preserve_markets_on_empty_discovery():
    """If discovery returns 0 markets, _active_markets should be preserved."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)

    fake = MarketWindow(
        market_id="btc-updown-5m-123",
        condition_id="cond_123",
        asset="BTC",
        timeframe_sec=300,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=100,
        close_epoch=400,
    )
    bot._active_markets = {"btc-updown-5m-123": fake}

    with patch(
        "polybot.bot.discover_crypto_updown_markets",
        new_callable=AsyncMock,
        return_value=[],
    ):
        asyncio.run(bot._discover_markets())

    assert len(bot._active_markets) == 1
    assert "btc-updown-5m-123" in bot._active_markets

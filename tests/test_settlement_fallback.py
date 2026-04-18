"""Tests for settlement fallback fixes: separate try/except, outcome validation, condition_id backprop."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from polybot.settlement import try_resolve_once, resolve_via_clob, resolve_via_gamma
from polybot.types import MarketWindow, Position


def _make_response(json_data, status_code=200):
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://fake"),
    )


# =========================================================================
# Bug 1: CLOB error must not skip Gamma fallback
# =========================================================================


class TestClobErrorFallback:
    @pytest.mark.asyncio
    async def test_clob_error_does_not_skip_gamma(self):
        """If resolve_via_clob raises, Gamma fallback should still run."""
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("polybot.settlement.resolve_via_clob", new_callable=AsyncMock) as mock_clob, \
             patch("polybot.settlement.resolve_via_gamma", new_callable=AsyncMock) as mock_gamma, \
             patch("polybot.settlement.fetch_condition_id", new_callable=AsyncMock) as mock_fetch:

            mock_clob.side_effect = httpx.HTTPStatusError(
                "503 Service Unavailable",
                request=httpx.Request("GET", "https://fake"),
                response=httpx.Response(503, request=httpx.Request("GET", "https://fake")),
            )
            mock_gamma.return_value = {"outcome": "UP", "settlement_price": 1.0}
            mock_fetch.return_value = ""

            result = await try_resolve_once(client, "https://clob.example.com", "test-slug", "0xabc123")

        assert result is not None
        assert result["outcome"] == "UP"
        mock_gamma.assert_called_once()

    @pytest.mark.asyncio
    async def test_both_apis_fail_returns_none(self):
        """If both CLOB and Gamma raise, try_resolve_once returns None."""
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("polybot.settlement.resolve_via_clob", new_callable=AsyncMock) as mock_clob, \
             patch("polybot.settlement.resolve_via_gamma", new_callable=AsyncMock) as mock_gamma, \
             patch("polybot.settlement.fetch_condition_id", new_callable=AsyncMock) as mock_fetch:

            mock_clob.side_effect = Exception("CLOB down")
            mock_gamma.side_effect = Exception("Gamma down")
            mock_fetch.return_value = ""

            result = await try_resolve_once(client, "https://clob.example.com", "test-slug", "0xabc123")

        assert result is None

    @pytest.mark.asyncio
    async def test_clob_success_skips_gamma(self):
        """If CLOB succeeds, Gamma should not be called."""
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("polybot.settlement.resolve_via_clob", new_callable=AsyncMock) as mock_clob, \
             patch("polybot.settlement.resolve_via_gamma", new_callable=AsyncMock) as mock_gamma, \
             patch("polybot.settlement.fetch_condition_id", new_callable=AsyncMock) as mock_fetch:

            mock_clob.return_value = {"outcome": "DOWN", "settlement_price": 1.0}
            mock_fetch.return_value = ""

            result = await try_resolve_once(client, "https://clob.example.com", "test-slug", "0xabc123")

        assert result is not None
        assert result["outcome"] == "DOWN"
        mock_gamma.assert_not_called()


# =========================================================================
# Bug 3: condition_id returned in result dict
# =========================================================================


class TestConditionIdInResult:
    @pytest.mark.asyncio
    async def test_fetched_condition_id_in_result(self):
        """When fetch_condition_id finds a real id, it should be in the result dict."""
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("polybot.settlement.resolve_via_clob", new_callable=AsyncMock) as mock_clob, \
             patch("polybot.settlement.resolve_via_gamma", new_callable=AsyncMock) as mock_gamma, \
             patch("polybot.settlement.fetch_condition_id", new_callable=AsyncMock) as mock_fetch:

            mock_fetch.return_value = "0xabc123"
            mock_clob.return_value = {"outcome": "UP", "settlement_price": 1.0}

            result = await try_resolve_once(client, "https://clob.example.com", "test-slug", "")

        assert result is not None
        assert result.get("condition_id") == "0xabc123"

    @pytest.mark.asyncio
    async def test_existing_condition_id_in_result(self):
        """When condition_id is already valid, it should be in the result dict."""
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("polybot.settlement.resolve_via_clob", new_callable=AsyncMock) as mock_clob, \
             patch("polybot.settlement.resolve_via_gamma", new_callable=AsyncMock) as mock_gamma, \
             patch("polybot.settlement.fetch_condition_id", new_callable=AsyncMock) as mock_fetch:

            mock_clob.return_value = {"outcome": "DOWN", "settlement_price": 1.0}

            result = await try_resolve_once(client, "https://clob.example.com", "test-slug", "0xexisting")

        assert result is not None
        assert result.get("condition_id") == "0xexisting"
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_gamma_fallback_includes_condition_id(self):
        """When CLOB fails and Gamma resolves, fetched condition_id is still in result."""
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("polybot.settlement.resolve_via_clob", new_callable=AsyncMock) as mock_clob, \
             patch("polybot.settlement.resolve_via_gamma", new_callable=AsyncMock) as mock_gamma, \
             patch("polybot.settlement.fetch_condition_id", new_callable=AsyncMock) as mock_fetch:

            mock_fetch.return_value = "0xfetched"
            mock_clob.side_effect = Exception("CLOB error")
            mock_gamma.return_value = {"outcome": "UP", "settlement_price": 1.0}

            result = await try_resolve_once(client, "https://clob.example.com", "test-slug", "")

        assert result is not None
        assert result.get("condition_id") == "0xfetched"


# =========================================================================
# Bug 2: Invalid outcome validation in _settle_position
# =========================================================================


def _make_bot_with_position(mid="m1", outcome=None):
    """Create a minimal mock bot with a position for testing _settle_position."""
    from polybot.bot import Bot
    from polybot.config import BotConfig

    cfg = BotConfig(dry_run=True)
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.running = False

    # Minimal mocks for dependencies
    bot.risk = MagicMock()
    bot._realized_pnl = 0.0
    bot._settled_wins = 0
    bot._settled_losses = 0
    bot._settled_pair_costs = []
    bot._activity_log = []
    bot._settled_markets = set()
    bot._expired_market_cache = {}
    bot.redeemer = MagicMock()
    bot.redeemer.queue_redemption = MagicMock()

    # Ladder manager mock: _realized_in_window must be a real dict so pop() works
    bot.ladder_manager = MagicMock()
    bot.ladder_manager._realized_in_window = {}

    # Position manager with a real position
    bot.position_manager = MagicMock()
    pos = Position(market_id=mid, up_qty=10, up_cost=4.0, dn_qty=10, dn_cost=5.0)
    bot.position_manager.positions = {mid: pos}
    bot.position_manager.bankroll = 1000.0

    market = MarketWindow(
        market_id=mid,
        condition_id="0xcond",
        asset="BTC",
        timeframe_sec=300,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1300,
    )

    return bot, market, pos


class TestSettlePositionValidation:
    def test_rejects_invalid_outcome(self, caplog):
        """_settle_position should reject garbage outcomes and NOT settle."""
        bot, market, pos = _make_bot_with_position()

        with caplog.at_level(logging.CRITICAL):
            bot._settle_position("m1", market, "GARBAGE")

        # Position should NOT be removed (complete_settlement not called)
        bot.position_manager.complete_settlement.assert_not_called()
        bot.position_manager.remove_position.assert_not_called()
        # Should NOT be in settled markets (stays pending for retry)
        assert "m1" not in bot._settled_markets
        # CRITICAL log emitted
        assert any("INVALID OUTCOME" in r.message for r in caplog.records)

    def test_rejects_empty_outcome(self, caplog):
        """Empty string outcome should be rejected."""
        bot, market, pos = _make_bot_with_position()

        with caplog.at_level(logging.CRITICAL):
            bot._settle_position("m1", market, "")

        bot.position_manager.complete_settlement.assert_not_called()
        assert "m1" not in bot._settled_markets

    def test_rejects_na_outcome(self, caplog):
        """'N/A' outcome should be rejected."""
        bot, market, pos = _make_bot_with_position()

        with caplog.at_level(logging.CRITICAL):
            bot._settle_position("m1", market, "N/A")

        bot.position_manager.complete_settlement.assert_not_called()
        assert "m1" not in bot._settled_markets


class TestSettlePositionNormalization:
    def test_normalizes_yes_to_up(self):
        """'YES' outcome should be treated as UP for PnL calculation."""
        bot, market, pos = _make_bot_with_position()
        expected_pnl = pos.profit_if_up()

        bot._settle_position("m1", market, "YES")

        bot.risk.update_pnl.assert_called_once_with(expected_pnl)
        bot.position_manager.complete_settlement.assert_called_once_with("m1")
        assert "m1" in bot._settled_markets

    def test_normalizes_no_to_down(self):
        """'NO' outcome should be treated as DOWN for PnL calculation."""
        bot, market, pos = _make_bot_with_position()
        expected_pnl = pos.profit_if_down()

        bot._settle_position("m1", market, "NO")

        bot.risk.update_pnl.assert_called_once_with(expected_pnl)
        bot.position_manager.complete_settlement.assert_called_once_with("m1")
        assert "m1" in bot._settled_markets

    def test_up_outcome_works(self):
        """Standard 'UP' outcome still works correctly."""
        bot, market, pos = _make_bot_with_position()
        expected_pnl = pos.profit_if_up()

        bot._settle_position("m1", market, "UP")

        bot.risk.update_pnl.assert_called_once_with(expected_pnl)
        assert "m1" in bot._settled_markets

    def test_down_outcome_works(self):
        """Standard 'DOWN' outcome still works correctly."""
        bot, market, pos = _make_bot_with_position()
        expected_pnl = pos.profit_if_down()

        bot._settle_position("m1", market, "DOWN")

        bot.risk.update_pnl.assert_called_once_with(expected_pnl)
        assert "m1" in bot._settled_markets


# =========================================================================
# Bug 3: condition_id back-propagation in settlement poller
# =========================================================================


class TestConditionIdBackpropagation:
    @pytest.mark.asyncio
    async def test_condition_id_backpropagated_in_poller(self):
        """When try_resolve_once returns a condition_id, it should be written back to the market."""
        bot, market, pos = _make_bot_with_position()
        market.condition_id = ""  # empty — needs backprop

        # Set up bot for poller iteration
        bot.running = True
        bot.cfg = MagicMock()
        bot.cfg.dry_run = False
        bot.cfg.polymarket_host = "https://clob.example.com"
        bot.cfg.bot_settlement_give_up_sec = 999999

        bot.position_manager.get_pending_settlements.return_value = ["m1"]
        bot._find_market = MagicMock(return_value=market)

        resolve_result = {"outcome": "UP", "settlement_price": 1.0, "condition_id": "0xreal"}

        with patch("polybot.settlement.try_resolve_once", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = resolve_result

            # We need to run just one iteration, not the full loop.
            # Patch to stop after first iteration.
            call_count = 0

            async def one_iteration_sleep(_):
                nonlocal call_count
                call_count += 1
                bot.running = False

            with patch("asyncio.sleep", side_effect=one_iteration_sleep):
                import time
                with patch("time.time", return_value=market.close_epoch + 10):
                    await bot._run_settlement_poller()

        assert market.condition_id == "0xreal"

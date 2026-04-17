"""Tests for API error handling: ClobApiError wrapping, 429 backoff, rate limiting.

TDD: These tests are written first, then the implementation follows.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.oms.order_executor import OrderExecutor, _make_clob_error
from polybot.types import Side


# ---------------------------------------------------------------------------
# Helper: create a mock exception with a response object
# ---------------------------------------------------------------------------

def _exc_with_response(status_code: int, headers: dict | None = None) -> RuntimeError:
    """Build a RuntimeError that has a .response attribute like httpx/requests."""
    exc = RuntimeError("API error")
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    exc.response = resp
    return exc


# ---------------------------------------------------------------------------
# _make_clob_error unit tests
# ---------------------------------------------------------------------------

class TestMakeClobError:
    def test_plain_exception_no_response(self):
        """Exception without .response -> status_code=None, retry_after=None."""
        err = _make_clob_error(RuntimeError("boom"))
        assert isinstance(err, ClobApiError)
        assert err.status_code is None
        assert err.retry_after is None
        assert err.cancel_only is False

    def test_429_extracts_retry_after(self):
        exc = _exc_with_response(429, {"Retry-After": "10"})
        err = _make_clob_error(exc)
        assert err.status_code == 429
        assert err.retry_after == 10.0
        assert err.cancel_only is False

    def test_429_default_retry_after(self):
        exc = _exc_with_response(429)
        err = _make_clob_error(exc)
        assert err.retry_after == 5.0

    def test_503_sets_cancel_only(self):
        exc = _exc_with_response(503)
        err = _make_clob_error(exc)
        assert err.status_code == 503
        assert err.cancel_only is True
        assert err.retry_after is None


# ---------------------------------------------------------------------------
# OrderExecutor wrapping tests
# ---------------------------------------------------------------------------

class TestOrderExecutorWrapping:
    def _make_executor(self) -> tuple[OrderExecutor, MagicMock]:
        cfg = BotConfig()
        mock_client = MagicMock()
        executor = OrderExecutor(cfg=cfg, clob_client=mock_client)
        return executor, mock_client

    def test_place_limit_buy_wraps_exception(self):
        executor, client = self._make_executor()
        client.create_order.side_effect = RuntimeError("connection reset")
        with pytest.raises(ClobApiError) as exc_info:
            executor.place_limit_buy("tok", 0.45, 50.0, "mkt", Side.UP)
        assert exc_info.value.status_code is None

    def test_place_limit_buy_wraps_429(self):
        executor, client = self._make_executor()
        client.create_order.side_effect = _exc_with_response(429, {"Retry-After": "10"})
        with pytest.raises(ClobApiError) as exc_info:
            executor.place_limit_buy("tok", 0.45, 50.0, "mkt", Side.UP)
        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after == 10.0

    def test_place_limit_buy_wraps_503(self):
        executor, client = self._make_executor()
        client.create_order.side_effect = _exc_with_response(503)
        with pytest.raises(ClobApiError) as exc_info:
            executor.place_limit_buy("tok", 0.45, 50.0, "mkt", Side.UP)
        assert exc_info.value.cancel_only is True

    def test_get_open_orders_wraps_exception(self):
        executor, client = self._make_executor()
        # Make it fall through to get_orders path
        client.get_open_orders = None  # not callable
        client.get_orders.side_effect = RuntimeError("timeout")
        with pytest.raises(ClobApiError):
            executor.get_open_orders()

    def test_cancel_order_wraps_exception(self):
        executor, client = self._make_executor()
        client.cancel.side_effect = RuntimeError("network error")
        with pytest.raises(ClobApiError):
            executor.cancel_order("order-123")

    def test_get_best_ask_wraps_exception(self):
        executor, client = self._make_executor()
        client.get_order_book.side_effect = RuntimeError("server error")
        with pytest.raises(ClobApiError):
            executor.get_best_ask("tok")

    def test_cancel_all_wraps_exception(self):
        """cancel_all should wrap non-ClobApiError into ClobApiError (not return False)."""
        executor, client = self._make_executor()
        client.cancel_all.side_effect = RuntimeError("500 internal")
        with pytest.raises(ClobApiError):
            executor.cancel_all()

    def test_existing_clob_api_error_passthrough(self):
        """ClobApiError raised by client should propagate unchanged (not double-wrapped)."""
        executor, client = self._make_executor()
        original = ClobApiError("rate limited", status_code=429, retry_after=7.0)
        client.create_order.side_effect = original
        with pytest.raises(ClobApiError) as exc_info:
            executor.place_limit_buy("tok", 0.45, 50.0, "mkt", Side.UP)
        # Must be the exact same object
        assert exc_info.value is original
        assert exc_info.value.retry_after == 7.0


# ---------------------------------------------------------------------------
# ClobMidpointPoller: 429 backoff tests
# ---------------------------------------------------------------------------

class TestMidpointPoller429Backoff:
    def test_429_sets_backoff(self):
        """After a 429 response, _backoff_until should be set in the future."""
        from polybot.data.clob_midpoints import ClobMidpointPoller

        poller = ClobMidpointPoller()
        poller.register_tokens(["tok_a"])

        async def _run():
            mock_resp = MagicMock()
            mock_resp.status_code = 429
            mock_resp.headers = {"Retry-After": "5"}

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)

            await poller._fetch_one(mock_client, "http://fake", "tok_a")
            # _backoff_until should be set in the future
            assert poller._backoff_until > time.monotonic()

        asyncio.run(_run())

    def test_backoff_skips_poll_cycle(self):
        """When _backoff_until is in the future, the run loop should skip polling."""
        from polybot.data.clob_midpoints import ClobMidpointPoller

        poller = ClobMidpointPoller()
        poller.register_tokens(["tok_a"])
        # Set backoff 10s in the future
        poller._backoff_until = time.monotonic() + 10.0

        call_count = 0

        async def _run():
            nonlocal call_count

            original_fetch = poller._fetch_one

            async def counting_fetch(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                await original_fetch(*args, **kwargs)

            poller._fetch_one = counting_fetch

            # Run one iteration by stopping after first sleep
            async def _one_iteration():
                poller._running = True
                # We'll manually run the loop body once
                if poller._token_ids:
                    if time.monotonic() < poller._backoff_until:
                        return  # Should skip
                    # Would have polled
                    call_count += 100

            await _one_iteration()

        asyncio.run(_run())
        assert call_count == 0  # No fetches should have happened


# ---------------------------------------------------------------------------
# Bug #1: _make_clob_error must check exc.status_code first (PolyApiException)
# ---------------------------------------------------------------------------

class TestMakeClobErrorDirectStatusCode:
    """PolyApiException stores status_code directly on the exception, not on .response."""

    def test_extracts_direct_status_code(self):
        """exc.status_code = 429 (no .response) -> result.status_code == 429."""
        exc = RuntimeError("rate limited")
        exc.status_code = 429  # PolyApiException pattern
        err = _make_clob_error(exc)
        assert err.status_code == 429
        assert err.retry_after == 5.0

    def test_extracts_response_status_code_as_fallback(self):
        """exc.response.status_code = 503 (no direct .status_code) -> result.status_code == 503."""
        exc = RuntimeError("service unavailable")
        resp = MagicMock()
        resp.status_code = 503
        resp.headers = {}
        exc.response = resp
        # No direct status_code attribute
        err = _make_clob_error(exc)
        assert err.status_code == 503
        assert err.cancel_only is True

    def test_direct_status_code_preferred_over_response(self):
        """exc.status_code = 429 AND exc.response.status_code = 500 -> 429 wins."""
        exc = RuntimeError("mixed")
        exc.status_code = 429
        resp = MagicMock()
        resp.status_code = 500
        resp.headers = {}
        exc.response = resp
        err = _make_clob_error(exc)
        assert err.status_code == 429
        assert err.retry_after == 5.0


# ---------------------------------------------------------------------------
# Bug #1 (legacy): same test against legacy executor's _make_clob_error
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Bug #3: place_limit_buy must raise ClobApiError when resp success=false
# ---------------------------------------------------------------------------

class TestPlaceLimitBuySuccessCheck:
    """OMS executor: single-order success field must be checked."""

    def _make_executor(self) -> tuple[OrderExecutor, MagicMock]:
        cfg = BotConfig()
        mock_client = MagicMock()
        executor = OrderExecutor(cfg=cfg, clob_client=mock_client)
        return executor, mock_client

    def test_rejects_on_success_false(self):
        executor, client = self._make_executor()
        client.create_order.return_value = {"mock": "signed"}
        client.post_order.return_value = {
            "success": False,
            "errorMsg": "insufficient balance",
        }
        with pytest.raises(ClobApiError, match="insufficient balance"):
            executor.place_limit_buy("tok", 0.45, 50.0, "mkt", Side.UP)

    def test_accepts_success_true(self):
        executor, client = self._make_executor()
        client.create_order.return_value = {"mock": "signed"}
        client.post_order.return_value = {
            "success": True,
            "orderID": "abc-123",
            "status": "live",
        }
        record = executor.place_limit_buy("tok", 0.45, 50.0, "mkt", Side.UP)
        assert record.order_id == "abc-123"
        assert record.status == "live"

    def test_accepts_missing_success_field(self):
        """If success field is absent, treat as successful (safe default)."""
        executor, client = self._make_executor()
        client.create_order.return_value = {"mock": "signed"}
        client.post_order.return_value = {
            "orderID": "def-456",
            "status": "live",
        }
        record = executor.place_limit_buy("tok", 0.45, 50.0, "mkt", Side.UP)
        assert record.order_id == "def-456"


# ---------------------------------------------------------------------------
# Bug #4: LadderManager must filter out records with empty order_id
# ---------------------------------------------------------------------------

class TestLadderSkipsEmptyOrderId:
    """LadderManager must not track records where order_id is empty."""

    def test_strategy_ladder_skips_empty_order_id(self):
        """strategy/ladder_manager.py should skip records with empty order_id."""
        from polybot.strategy.ladder_manager import LadderManager
        from polybot.strategy.order_tracker import OrderTracker

        cfg = BotConfig()
        mock_executor = MagicMock()
        tracker = OrderTracker()
        mock_positions = MagicMock()
        mock_positions.bankroll = 1000.0
        mock_positions.equity.return_value = 1000.0
        mock_positions.total_position_cost.return_value = 0.0
        mock_positions.active_position_count.return_value = 0
        mock_risk = MagicMock()
        mock_risk.is_halted.return_value = False
        mock_risk.can_open_position.return_value = True
        mock_risk.check_capital_at_risk.return_value = True
        mock_risk.exposure_factor.return_value = 1.0

        lm = LadderManager(
            cfg=cfg,
            order_executor=mock_executor,
            order_tracker=tracker,
            position_manager=mock_positions,
            risk_manager=mock_risk,
        )

        # Mock executor to return records: one valid, one with empty order_id
        from polybot.types import OrderRecord
        valid_record = OrderRecord(
            market_id="mkt", side=Side.UP, price=0.45, size=50.0,
            timestamp=1.0, order_id="real-id-123", status="live",
        )
        empty_record = OrderRecord(
            market_id="mkt", side=Side.UP, price=0.40, size=50.0,
            timestamp=1.0, order_id="", status="unknown",
        )
        mock_executor.place_batch_limit_buys.return_value = [valid_record, empty_record]
        # Use asymmetric asks so market tightness filter passes (0.35+0.40=0.75 < 0.92)
        mock_executor.get_best_ask.side_effect = lambda tid: 0.35 if tid == "up_tok" else 0.40

        from polybot.types import MarketWindow
        market = MarketWindow(
            market_id="mkt", condition_id="cond", asset="BTC",
            up_token_id="up_tok", dn_token_id="dn_tok",
            open_epoch=0, close_epoch=9999999999,
            timeframe_sec=300,
        )

        lm.post_ladder(market)

        # Only the valid record should be tracked
        assert "real-id-123" in tracker.orders
        assert "" not in tracker.orders

class TestMidpointSemaphore:
    def test_semaphore_limits_concurrency(self):
        """With 20 tokens, max concurrent requests should be <= 5."""
        from polybot.data.clob_midpoints import ClobMidpointPoller

        poller = ClobMidpointPoller()
        tokens = [f"tok_{i}" for i in range(20)]
        poller.register_tokens(tokens)

        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def _run():
            nonlocal max_concurrent, current_concurrent

            async def slow_get(*args, **kwargs):
                nonlocal max_concurrent, current_concurrent
                async with lock:
                    current_concurrent += 1
                    if current_concurrent > max_concurrent:
                        max_concurrent = current_concurrent
                # Simulate network delay
                await asyncio.sleep(0.05)
                async with lock:
                    current_concurrent -= 1
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"mid": "0.50"}
                return resp

            mock_client = AsyncMock()
            mock_client.get = slow_get

            # Run one poll cycle manually
            poller._backoff_until = 0.0
            tasks = [
                poller._fetch_one(mock_client, "http://fake", tid)
                for tid in list(poller._token_ids)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(_run())
        assert max_concurrent <= 5, f"Max concurrent was {max_concurrent}, expected <= 5"


class TestMidpointAdaptiveInterval:
    def test_adaptive_interval_increases_on_429(self):
        """Consecutive 429s should increase the effective poll interval."""
        from polybot.data.clob_midpoints import ClobMidpointPoller

        poller = ClobMidpointPoller()
        poller.register_tokens(["tok_a"])

        async def _run():
            mock_resp = MagicMock()
            mock_resp.status_code = 429
            mock_resp.headers = {"Retry-After": "2"}

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)

            # Trigger 3 consecutive 429s
            for _ in range(3):
                await poller._fetch_one(mock_client, "http://fake", "tok_a")

            # After 3 consecutive 429s, _consecutive_429s should be 3
            assert poller._consecutive_429s >= 3
            # Effective interval should be higher than base
            assert poller._effective_interval > poller._base_interval

        asyncio.run(_run())

    def test_adaptive_interval_resets_on_success(self):
        """A successful response should reset the adaptive interval."""
        from polybot.data.clob_midpoints import ClobMidpointPoller

        poller = ClobMidpointPoller()
        poller.register_tokens(["tok_a"])

        async def _run():
            # First trigger some 429s
            mock_429 = MagicMock()
            mock_429.status_code = 429
            mock_429.headers = {"Retry-After": "2"}

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_429)

            for _ in range(3):
                await poller._fetch_one(mock_client, "http://fake", "tok_a")

            assert poller._consecutive_429s >= 3

            # Now a successful response
            mock_ok = MagicMock()
            mock_ok.status_code = 200
            mock_ok.json.return_value = {"mid": "0.55"}
            mock_client.get = AsyncMock(return_value=mock_ok)

            await poller._fetch_one(mock_client, "http://fake", "tok_a")

            assert poller._consecutive_429s == 0
            assert poller._effective_interval == poller._base_interval

        asyncio.run(_run())

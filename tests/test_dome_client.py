"""
Tests for tools/dome_client.py

Uses httpx transport mocking where available, falls back to unittest.mock.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch, call

import pytest

# Make sure tools/ is importable
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "tools"))

from dome_client import DomeClient, DomeAPIError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_with_mock(responses: list[tuple[int, dict]], *, api_key: str = "test-key") -> tuple[DomeClient, MagicMock]:
    """Return (client, mock_http_get) where _http_get yields successive responses."""
    client = DomeClient(api_key=api_key, min_interval_sec=0)

    side_effects = [(status, json.dumps(body), {}) for status, body in responses]
    mock = MagicMock(side_effect=side_effects)
    client._http_get = mock
    return client, mock


# ---------------------------------------------------------------------------
# API key loading
# ---------------------------------------------------------------------------

class TestApiKeyLoading:
    def test_explicit_key_used(self):
        client = DomeClient(api_key="my-key", min_interval_sec=0)
        assert client._api_key == "my-key"

    def test_env_var_loaded(self, monkeypatch):
        monkeypatch.setenv("DOME_API_KEY", "env-key")
        # Don't pass api_key — should pick up from env
        client = DomeClient(min_interval_sec=0)
        assert client._api_key == "env-key"

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("DOME_API_KEY", raising=False)
        with pytest.raises(ValueError, match="DOME_API_KEY"):
            DomeClient(api_key=None)

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("DOME_API_KEY", "env-key")
        client = DomeClient(api_key="explicit-key", min_interval_sec=0)
        assert client._api_key == "explicit-key"


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------

class TestTimestampConversion:
    """Verify that endpoints requiring milliseconds get seconds * 1000."""

    def test_orderbook_uses_milliseconds(self):
        client, mock = _client_with_mock([(200, {"snapshots": []})])
        client.get_orderbook_snapshots("tok123", start_sec=1_000_000, end_sec=1_000_900)
        url = mock.call_args[0][0]
        assert "start_time=1000000000" in url
        assert "end_time=1000900000" in url

    def test_binance_uses_milliseconds(self):
        client, mock = _client_with_mock([(200, {"prices": []})])
        client.get_binance_prices("btcusdt", start_sec=1_000_000, end_sec=1_000_060)
        url = mock.call_args[0][0]
        assert "start_time=1000000000" in url
        assert "end_time=1000060000" in url

    def test_chainlink_uses_milliseconds(self):
        client, mock = _client_with_mock([(200, {"prices": []})])
        client.get_chainlink_prices("btc/usd", start_sec=1_000_000, end_sec=1_000_060)
        url = mock.call_args[0][0]
        assert "start_time=1000000000" in url
        assert "end_time=1000060000" in url

    def test_candlesticks_uses_seconds(self):
        """Candlestick endpoint takes epoch seconds — NOT multiplied by 1000."""
        raw = {"candlesticks": [[[ {"end_period_ts": 1000060} ]]]}
        client, mock = _client_with_mock([(200, raw)])
        client.get_candlesticks("cid1", start_sec=1_000_000, end_sec=1_000_900)
        url = mock.call_args[0][0]
        # Should contain seconds, NOT milliseconds
        assert "start_time=1000000&" in url or "start_time=1000000" in url
        assert "1000000000" not in url

    def test_wallet_pnl_uses_milliseconds_when_provided(self):
        client, mock = _client_with_mock([(200, {})])
        client.get_wallet_pnl("0xabc", start_sec=1_000_000, end_sec=1_001_000)
        url = mock.call_args[0][0]
        assert "start_time=1000000000" in url
        assert "end_time=1001000000" in url


# ---------------------------------------------------------------------------
# Retry on 429 and 5xx
# ---------------------------------------------------------------------------

class TestRetry:
    def test_retries_on_429(self):
        """After two 429s, the third attempt should succeed."""
        responses = [
            (429, {"error": "rate limited"}),
            (429, {"error": "rate limited"}),
            (200, {"prices": []}),
        ]
        client, mock = _client_with_mock(responses)
        # Patch time.sleep to avoid waiting
        with patch("dome_client.time.sleep"):
            result = client.get_binance_prices("btcusdt", 1000, 2000)
        assert result == []
        assert mock.call_count == 3

    def test_retries_on_500(self):
        responses = [
            (500, {"error": "server error"}),
            (200, {"snapshots": []}),
        ]
        client, mock = _client_with_mock(responses)
        with patch("dome_client.time.sleep"):
            result = client.get_orderbook_snapshots("tok", 1000, 2000)
        assert result == []
        assert mock.call_count == 2

    def test_raises_after_max_retries(self):
        """After 3 retries (4 total attempts) still 429 → DomeAPIError."""
        responses = [(429, {"error": "limited"})] * 4
        client, mock = _client_with_mock(responses)
        with patch("dome_client.time.sleep"):
            with pytest.raises(DomeAPIError) as exc_info:
                client.get_binance_prices("btcusdt", 1000, 2000)
        assert exc_info.value.status_code == 429
        assert mock.call_count == 4  # 1 initial + 3 retries

    def test_no_retry_on_404(self):
        """4xx (other than 429) should raise immediately without retry."""
        responses = [(404, {"error": "not found"})]
        client, mock = _client_with_mock(responses)
        with pytest.raises(DomeAPIError) as exc_info:
            client.get_market("nonexistent-slug")
        assert exc_info.value.status_code == 404
        assert mock.call_count == 1


# ---------------------------------------------------------------------------
# Error extraction
# ---------------------------------------------------------------------------

class TestErrorExtraction:
    def test_error_has_status_and_body(self):
        body = {"error": "bad request", "detail": "missing param"}
        client, _ = _client_with_mock([(400, body)])
        with pytest.raises(DomeAPIError) as exc_info:
            client.get_market("some-slug")
        err = exc_info.value
        assert err.status_code == 400
        assert "bad request" in err.body

    def test_error_message_includes_url(self):
        client, _ = _client_with_mock([(403, {"error": "forbidden"})])
        with pytest.raises(DomeAPIError) as exc_info:
            client.get_market("slug")
        assert "polymarket/markets" in exc_info.value.url


# ---------------------------------------------------------------------------
# Candlestick flattening
# ---------------------------------------------------------------------------

class TestCandlestickParsing:
    def test_returns_flat_list_of_candles(self):
        c1 = {"end_period_ts": 100, "volume": 10}
        c2 = {"end_period_ts": 160, "volume": 20}
        raw = {"candlesticks": [[[c1, c2]]]}
        client, _ = _client_with_mock([(200, raw)])
        result = client.get_candlesticks("cid", 0, 300)
        assert result == [c1, c2]

    def test_empty_candlesticks(self):
        client, _ = _client_with_mock([(200, {"candlesticks": []})])
        assert client.get_candlesticks("cid", 0, 300) == []


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

class TestDiskCache:
    def test_second_call_hits_cache(self, tmp_path):
        client, mock = _client_with_mock(
            [(200, {"prices": [{"value": 70000}]})],
            api_key="test-key",
        )
        client._cache_dir = tmp_path
        client._cache_ttl_sec = 3600

        # First call — should hit network
        result1 = client.get_binance_prices("btcusdt", 1000, 2000)
        assert mock.call_count == 1

        # Second call — should use cache
        result2 = client.get_binance_prices("btcusdt", 1000, 2000)
        assert mock.call_count == 1  # no new network call
        assert result1 == result2

    def test_expired_cache_triggers_new_request(self, tmp_path):
        responses = [
            (200, {"prices": [{"value": 70000}]}),
            (200, {"prices": [{"value": 71000}]}),
        ]
        client, mock = _client_with_mock(responses, api_key="test-key")
        client._cache_dir = tmp_path
        client._cache_ttl_sec = 0  # expire immediately

        client.get_binance_prices("btcusdt", 1000, 2000)
        # Manually age the cache by setting _saved_at to past
        for f in tmp_path.iterdir():
            data = json.loads(f.read_text())
            data["_saved_at"] = 0
            f.write_text(json.dumps(data))

        client.get_binance_prices("btcusdt", 1000, 2000)
        assert mock.call_count == 2


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class TestContextManager:
    def test_enter_exit(self):
        with DomeClient(api_key="k", min_interval_sec=0) as client:
            assert client._api_key == "k"
        # No exception = pass

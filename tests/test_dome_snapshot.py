"""
Tests for tools/dome_snapshot.py
"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys
from unittest.mock import MagicMock, patch, call

import pytest

# Make tools/ importable
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "tools"))

from dome_snapshot import (
    _15m_windows_for_date,
    _market_slug,
    fetch_market_snapshot,
    run,
)


# ---------------------------------------------------------------------------
# Window generation
# ---------------------------------------------------------------------------

class TestWindowGeneration:
    def test_1h_produces_4_windows(self):
        date = datetime.date(2026, 4, 10)
        windows = _15m_windows_for_date(date, hours=1)
        assert len(windows) == 4

    def test_2h_produces_8_windows(self):
        date = datetime.date(2026, 4, 10)
        windows = _15m_windows_for_date(date, hours=2)
        assert len(windows) == 8

    def test_windows_are_consecutive_900s_apart(self):
        date = datetime.date(2026, 4, 10)
        windows = _15m_windows_for_date(date, hours=1)
        for i in range(len(windows) - 1):
            assert windows[i + 1][0] - windows[i][0] == 900

    def test_first_window_starts_at_midnight_utc(self):
        date = datetime.date(2026, 4, 10)
        windows = _15m_windows_for_date(date, hours=1)
        import datetime as dt
        midnight = int(dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc).timestamp())
        assert windows[0][0] == midnight

    def test_window_duration_is_900s(self):
        date = datetime.date(2026, 4, 10)
        windows = _15m_windows_for_date(date, hours=1)
        for start, end in windows:
            assert end - start == 900


class TestMarketSlug:
    def test_btc_15m_slug(self):
        assert _market_slug("BTC", "15m", 1775924100) == "btc-updown-15m-1775924100"

    def test_lowercase_asset(self):
        assert _market_slug("ETH", "15m", 1000000) == "eth-updown-15m-1000000"


# ---------------------------------------------------------------------------
# Fetch market snapshot
# ---------------------------------------------------------------------------

def _make_client_mock() -> MagicMock:
    """Create a DomeClient mock with sensible defaults."""
    client = MagicMock()
    client.get_market.return_value = {
        "condition_id": "cond123",
        "token_ids": ["tok_yes", "tok_no"],
    }
    client.get_candlesticks.return_value = [
        {"end_period_ts": 1000060, "volume": 10},
    ]
    client.get_orderbook_snapshots.return_value = [
        {"asks": [{"price": "0.55", "size": "100"}], "bids": []},
    ]
    client.get_binance_prices.return_value = [
        {"symbol": "btcusdt", "value": 70000.0, "timestamp": 1000000000},
    ]
    client.get_chainlink_prices.return_value = [
        {"symbol": "btcusd", "value": 69990.0, "timestamp": 1000000000},
    ]
    return client


class TestFetchMarketSnapshot:
    def test_returns_header_as_first_line(self):
        client = _make_client_mock()
        lines = fetch_market_snapshot(client, "btc-updown-15m-1000000", 1000000, 1000900, "btcusdt", "btc/usd")
        assert lines[0]["type"] == "header"
        assert lines[0]["market_slug"] == "btc-updown-15m-1000000"
        assert lines[0]["condition_id"] == "cond123"

    def test_calls_candlesticks_with_condition_id(self):
        client = _make_client_mock()
        fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        client.get_candlesticks.assert_called_once_with("cond123", 1000, 1900, interval="1m")

    def test_calls_orderbook_with_up_token(self):
        client = _make_client_mock()
        fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        client.get_orderbook_snapshots.assert_called_once_with("tok_yes", 1000, 1900)

    def test_calls_binance_with_correct_currency(self):
        client = _make_client_mock()
        fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        client.get_binance_prices.assert_called_once_with("btcusdt", 1000, 1900)

    def test_calls_chainlink_with_correct_currency(self):
        client = _make_client_mock()
        fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        client.get_chainlink_prices.assert_called_once_with("btc/usd", 1000, 1900)

    def test_all_data_types_present(self):
        client = _make_client_mock()
        lines = fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        types = {ln["type"] for ln in lines}
        assert types == {"header", "candle", "orderbook", "binance", "chainlink"}

    def test_market_wrapped_in_market_key(self):
        """Handle response shape {"market": {...}}."""
        client = _make_client_mock()
        client.get_market.return_value = {
            "market": {
                "condition_id": "wrapped_cid",
                "token_ids": ["yes_tok"],
            }
        }
        lines = fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        assert lines[0]["condition_id"] == "wrapped_cid"

    def test_market_as_list(self):
        """Handle response shape [market_obj, ...]."""
        client = _make_client_mock()
        client.get_market.return_value = [
            {"condition_id": "list_cid", "token_ids": ["yes_tok"]}
        ]
        lines = fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        assert lines[0]["condition_id"] == "list_cid"

    def test_market_wrapped_in_markets_key(self):
        """Handle real Dome response shape {"markets": [...], "pagination": {...}}."""
        client = _make_client_mock()
        client.get_market.return_value = {
            "markets": [
                {"condition_id": "real_cid", "token_ids": ["yes_tok", "no_tok"]}
            ],
            "pagination": {"total": 1},
        }
        lines = fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        assert lines[0]["condition_id"] == "real_cid"
        assert lines[0]["up_token_id"] == "yes_tok"

    def test_market_with_side_a_side_b_token_ids(self):
        """Handle actual Dome shape where token IDs live in side_a.id / side_b.id."""
        client = _make_client_mock()
        client.get_market.return_value = {
            "markets": [
                {
                    "condition_id": "dome_cid",
                    "side_a": {"id": "36281616", "label": "Up"},
                    "side_b": {"id": "96090662", "label": "Down"},
                }
            ],
            "pagination": {"total": 1},
        }
        lines = fetch_market_snapshot(client, "slug", 1000, 1900, "btcusdt", "btc/usd")
        assert lines[0]["condition_id"] == "dome_cid"
        assert lines[0]["up_token_id"] == "36281616"
        assert lines[0]["token_ids"] == ["36281616", "96090662"]
        # Should call orderbook with side_a's token ID
        client.get_orderbook_snapshots.assert_called_once_with("36281616", 1000, 1900)


# ---------------------------------------------------------------------------
# Run — skip-if-exists + force
# ---------------------------------------------------------------------------

class TestRunIdempotency:
    def _minimal_run(self, tmp_path, *, force=False, extra_setup=None):
        """Call run() for 2026-04-10 BTC 15m, 1 hour, patching DomeClient."""
        client_mock = _make_client_mock()

        with patch("dome_snapshot.DomeClient") as ClientClass:
            # Make DomeClient() return our mock and support context manager
            instance = client_mock
            instance.__enter__ = MagicMock(return_value=instance)
            instance.__exit__ = MagicMock(return_value=False)
            ClientClass.return_value = instance

            if extra_setup:
                extra_setup(tmp_path)

            run(
                date=datetime.date(2026, 4, 10),
                asset="BTC",
                timeframe="15m",
                out_dir=tmp_path,
                hours=1,
                force=force,
            )
            return instance

    def test_creates_4_files_for_1h(self, tmp_path):
        self._minimal_run(tmp_path)
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 4

    def test_files_are_valid_jsonl(self, tmp_path):
        self._minimal_run(tmp_path)
        for f in tmp_path.glob("*.jsonl"):
            for line in f.read_text().splitlines():
                obj = json.loads(line)
                assert isinstance(obj, dict)

    def test_skip_existing_nonempty_file(self, tmp_path):
        """If a file already exists and is non-empty, skip it without API calls."""
        # Pre-create all 4 files
        date = datetime.date(2026, 4, 10)
        windows = _15m_windows_for_date(date, hours=1)
        for w_start, _ in windows:
            slug = _market_slug("BTC", "15m", w_start)
            f = tmp_path / f"{slug}.jsonl"
            f.write_text('{"type":"header","market_slug":"' + slug + '"}\n')

        client_mock = _make_client_mock()
        with patch("dome_snapshot.DomeClient") as ClientClass:
            instance = client_mock
            instance.__enter__ = MagicMock(return_value=instance)
            instance.__exit__ = MagicMock(return_value=False)
            ClientClass.return_value = instance

            run(
                date=date,
                asset="BTC",
                timeframe="15m",
                out_dir=tmp_path,
                hours=1,
                force=False,
            )
        # No API calls should have been made
        instance.get_market.assert_not_called()

    def test_force_refetches_existing_files(self, tmp_path):
        """With --force, even existing files should be re-fetched."""
        date = datetime.date(2026, 4, 10)
        windows = _15m_windows_for_date(date, hours=1)
        for w_start, _ in windows:
            slug = _market_slug("BTC", "15m", w_start)
            f = tmp_path / f"{slug}.jsonl"
            f.write_text('{"type":"header","market_slug":"' + slug + '"}\n')

        client_mock = _make_client_mock()
        with patch("dome_snapshot.DomeClient") as ClientClass:
            instance = client_mock
            instance.__enter__ = MagicMock(return_value=instance)
            instance.__exit__ = MagicMock(return_value=False)
            ClientClass.return_value = instance

            run(
                date=date,
                asset="BTC",
                timeframe="15m",
                out_dir=tmp_path,
                hours=1,
                force=True,
            )
        # Should have fetched all 4 markets → 4 get_market calls
        assert instance.get_market.call_count == 4

    def test_correct_slugs_requested(self, tmp_path):
        """Slugs passed to get_market match btc-updown-15m-<epoch_sec>."""
        import datetime as dt
        midnight = int(dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc).timestamp())
        expected_slugs = [
            f"btc-updown-15m-{midnight + i * 900}" for i in range(4)
        ]
        client_mock = _make_client_mock()
        with patch("dome_snapshot.DomeClient") as ClientClass:
            instance = client_mock
            instance.__enter__ = MagicMock(return_value=instance)
            instance.__exit__ = MagicMock(return_value=False)
            ClientClass.return_value = instance

            run(
                date=datetime.date(2026, 4, 10),
                asset="BTC",
                timeframe="15m",
                out_dir=tmp_path,
                hours=1,
                force=False,
            )
        actual_slugs = [c.args[0] for c in instance.get_market.call_args_list]
        assert actual_slugs == expected_slugs

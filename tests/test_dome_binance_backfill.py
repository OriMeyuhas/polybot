"""Tests for tools/dome_binance_backfill.py.

Covers:
 (a) Correct URL construction for a given market
 (b) Correct output file format (list of {ts_ms, close_price})
 (c) Idempotent skip behavior (output file already exists → no HTTP call)
 (d) Sanity assert on ts_ms alignment (ts_ms must be within request range)

All HTTP calls are mocked — no real network calls.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import tools.dome_binance_backfill as backfill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kline_response(start_ms: int, count: int) -> list:
    """Build a minimal Binance klines response (list of arrays).

    Each kline array: [open_time, open, high, low, close, ...]
    We only use open_time (index 0) and close (index 4).
    """
    rows = []
    for i in range(count):
        ts = start_ms + i * 1000  # 1s interval
        rows.append([
            ts,       # open_time_ms
            "83000",  # open
            "83010",  # high
            "82990",  # low
            f"{83000 + i}",  # close (unique per candle)
            "1.5",    # volume
            ts + 999, # close_time
            "125000", # quote_asset_volume
            100,      # number_of_trades
            "0.75",   # taker_buy_base_volume
            "62500",  # taker_buy_quote_volume
            "0",      # ignore
        ])
    return rows


def _make_dome_file(tmp_path: pathlib.Path, market_id: str, window_start: int) -> pathlib.Path:
    """Create a minimal dome snapshot file with the given market_id and window_start."""
    dome_dir = tmp_path / "dome_snapshots"
    dome_dir.mkdir(parents=True, exist_ok=True)
    file_path = dome_dir / f"{market_id}.jsonl"
    header = {
        "type": "header",
        "market_slug": market_id,
        "condition_id": "0xdeadbeef",
        "up_token_id": "up_tok_123",
        "dn_token_id": "dn_tok_456",
        "window_start": window_start,
        "window_end": window_start + 900,
        "fetched_at": window_start + 10000,
        "raw_market": {"winning_side": {"label": "Up"}, "extra_fields": {}},
    }
    file_path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    return file_path


# ---------------------------------------------------------------------------
# (a) Correct URL construction
# ---------------------------------------------------------------------------

class TestUrlConstruction:
    """Assert the correct Binance klines URL is built for a given market."""

    def test_url_contains_correct_symbol_and_interval(self, tmp_path):
        """URL must use BTCUSDT and 1s interval."""
        window_start = 1_774_742_400  # epoch seconds

        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_kline_response(
            start_ms=(window_start - 120) * 1000,
            count=150,
        )
        mock_resp.raise_for_status.return_value = None

        captured_urls = []

        def fake_get(url, params=None, timeout=None):
            captured_urls.append((url, params))
            return mock_resp

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, "btc-updown-15m-1774742400", window_start)

        with patch("tools.dome_binance_backfill.requests.get", side_effect=fake_get):
            backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id="btc-updown-15m-1774742400",
                window_start=window_start,
            )

        assert len(captured_urls) == 1
        url, params = captured_urls[0]
        assert url == "https://api.binance.com/api/v3/klines"
        assert params["symbol"] == "BTCUSDT"
        assert params["interval"] == "1s"

    def test_url_start_time_is_120s_before_window(self, tmp_path):
        """startTime param must be window_start - 120_000 ms."""
        window_start = 1_774_742_400

        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_kline_response(
            start_ms=(window_start - 120) * 1000,
            count=150,
        )
        mock_resp.raise_for_status.return_value = None

        captured_params = []

        def fake_get(url, params=None, timeout=None):
            captured_params.append(params or {})
            return mock_resp

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, "btc-updown-15m-1774742400", window_start)

        with patch("tools.dome_binance_backfill.requests.get", side_effect=fake_get):
            backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id="btc-updown-15m-1774742400",
                window_start=window_start,
            )

        assert len(captured_params) == 1
        p = captured_params[0]
        expected_start = (window_start - 120) * 1000
        assert p["startTime"] == expected_start

    def test_url_end_time_is_30s_after_window(self, tmp_path):
        """endTime param must be window_start + 30_000 ms."""
        window_start = 1_774_742_400

        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_kline_response(
            start_ms=(window_start - 120) * 1000,
            count=150,
        )
        mock_resp.raise_for_status.return_value = None

        captured_params = []

        def fake_get(url, params=None, timeout=None):
            captured_params.append(params or {})
            return mock_resp

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, "btc-updown-15m-1774742400", window_start)

        with patch("tools.dome_binance_backfill.requests.get", side_effect=fake_get):
            backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id="btc-updown-15m-1774742400",
                window_start=window_start,
            )

        assert len(captured_params) == 1
        p = captured_params[0]
        expected_end = (window_start + 30) * 1000
        assert p["endTime"] == expected_end


# ---------------------------------------------------------------------------
# (b) Correct output file format
# ---------------------------------------------------------------------------

class TestOutputFileFormat:
    """Assert output JSONL has the correct structure per row."""

    def test_output_file_is_created_in_out_dir(self, tmp_path):
        """Output file must exist at out_dir/{market_id}.jsonl."""
        window_start = 1_774_742_400
        market_id = "btc-updown-15m-1774742400"

        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_kline_response(
            start_ms=(window_start - 120) * 1000,
            count=3,
        )
        mock_resp.raise_for_status.return_value = None

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, market_id, window_start)

        with patch("tools.dome_binance_backfill.requests.get", return_value=mock_resp):
            backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id=market_id,
                window_start=window_start,
            )

        out_file = out_dir / f"{market_id}.jsonl"
        assert out_file.exists(), f"Expected output file at {out_file}"

    def test_each_row_has_ts_ms_and_close_price(self, tmp_path):
        """Every row must have 'ts_ms' (int) and 'close_price' (float) keys."""
        window_start = 1_774_742_400
        market_id = "btc-updown-15m-1774742400"
        n_klines = 5

        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_kline_response(
            start_ms=(window_start - 120) * 1000,
            count=n_klines,
        )
        mock_resp.raise_for_status.return_value = None

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, market_id, window_start)

        with patch("tools.dome_binance_backfill.requests.get", return_value=mock_resp):
            backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id=market_id,
                window_start=window_start,
            )

        out_file = out_dir / f"{market_id}.jsonl"
        rows = [json.loads(l) for l in out_file.read_text().splitlines() if l.strip()]
        assert len(rows) == n_klines
        for row in rows:
            assert "ts_ms" in row, f"Missing ts_ms in row: {row}"
            assert "close_price" in row, f"Missing close_price in row: {row}"
            assert isinstance(row["ts_ms"], int), f"ts_ms must be int: {row}"
            assert isinstance(row["close_price"], float), f"close_price must be float: {row}"

    def test_close_price_matches_kline_close_field(self, tmp_path):
        """close_price in output must match the 'close' field (index 4) from Binance response."""
        window_start = 1_774_742_400
        market_id = "btc-updown-15m-1774742400"

        klines = _make_kline_response(start_ms=(window_start - 120) * 1000, count=3)
        # Inject known close values
        klines[0][4] = "83001.50"
        klines[1][4] = "83002.75"
        klines[2][4] = "83003.00"

        mock_resp = MagicMock()
        mock_resp.json.return_value = klines
        mock_resp.raise_for_status.return_value = None

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, market_id, window_start)

        with patch("tools.dome_binance_backfill.requests.get", return_value=mock_resp):
            backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id=market_id,
                window_start=window_start,
            )

        out_file = out_dir / f"{market_id}.jsonl"
        rows = [json.loads(l) for l in out_file.read_text().splitlines() if l.strip()]
        assert rows[0]["close_price"] == pytest.approx(83001.50)
        assert rows[1]["close_price"] == pytest.approx(83002.75)
        assert rows[2]["close_price"] == pytest.approx(83003.00)


# ---------------------------------------------------------------------------
# (c) Idempotent skip behavior
# ---------------------------------------------------------------------------

class TestIdempotentSkip:
    """Assert that if the output file already exists, no HTTP call is made."""

    def test_skip_when_output_file_exists(self, tmp_path):
        """backfill_market must skip (no HTTP) if output file already exists."""
        window_start = 1_774_742_400
        market_id = "btc-updown-15m-1774742400"

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        out_dir.mkdir(parents=True)
        # Pre-create output file
        existing_file = out_dir / f"{market_id}.jsonl"
        existing_file.write_text('{"ts_ms": 1, "close_price": 100.0}\n', encoding="utf-8")

        dome_file = _make_dome_file(tmp_path, market_id, window_start)

        call_count = []

        def fake_get(*args, **kwargs):
            call_count.append(1)
            return MagicMock()

        with patch("tools.dome_binance_backfill.requests.get", side_effect=fake_get):
            result = backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id=market_id,
                window_start=window_start,
            )

        assert len(call_count) == 0, "HTTP call was made despite output file existing"
        assert result == "skipped"

    def test_no_skip_when_output_file_missing(self, tmp_path):
        """backfill_market must make HTTP call when output file does not exist."""
        window_start = 1_774_742_400
        market_id = "btc-updown-15m-1774742400"

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, market_id, window_start)

        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_kline_response(
            start_ms=(window_start - 120) * 1000,
            count=2,
        )
        mock_resp.raise_for_status.return_value = None

        call_count = []

        def fake_get(url, params=None, timeout=None):
            call_count.append(1)
            return mock_resp

        with patch("tools.dome_binance_backfill.requests.get", side_effect=fake_get):
            result = backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id=market_id,
                window_start=window_start,
            )

        assert len(call_count) == 1, "Expected exactly one HTTP call"
        assert result == "written"


# ---------------------------------------------------------------------------
# (d) ts_ms alignment sanity check
# ---------------------------------------------------------------------------

class TestTsMsAlignment:
    """Assert all returned ts_ms values are within the requested time range."""

    def test_ts_ms_within_requested_range(self, tmp_path):
        """All ts_ms values must be in [window_start-120s, window_start+30s] (in ms)."""
        window_start = 1_774_742_400
        market_id = "btc-updown-15m-1774742400"
        start_ms = (window_start - 120) * 1000
        end_ms = (window_start + 30) * 1000

        # Build klines exactly in the requested range
        klines = _make_kline_response(start_ms=start_ms, count=150)

        mock_resp = MagicMock()
        mock_resp.json.return_value = klines
        mock_resp.raise_for_status.return_value = None

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, market_id, window_start)

        with patch("tools.dome_binance_backfill.requests.get", return_value=mock_resp):
            backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id=market_id,
                window_start=window_start,
            )

        out_file = out_dir / f"{market_id}.jsonl"
        rows = [json.loads(l) for l in out_file.read_text().splitlines() if l.strip()]
        assert len(rows) > 0, "Expected at least one row"
        for row in rows:
            ts = row["ts_ms"]
            # Allow a tiny margin (Binance returns open_time, which is candle start)
            assert start_ms - 1000 <= ts <= end_ms + 1000, (
                f"ts_ms={ts} is outside expected range [{start_ms}, {end_ms}]"
            )

    def test_ts_ms_are_ascending(self, tmp_path):
        """ts_ms values must be in ascending order (Binance returns sorted klines)."""
        window_start = 1_774_742_400
        market_id = "btc-updown-15m-1774742400"

        klines = _make_kline_response(start_ms=(window_start - 120) * 1000, count=10)

        mock_resp = MagicMock()
        mock_resp.json.return_value = klines
        mock_resp.raise_for_status.return_value = None

        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        dome_file = _make_dome_file(tmp_path, market_id, window_start)

        with patch("tools.dome_binance_backfill.requests.get", return_value=mock_resp):
            backfill.backfill_market(
                dome_file=dome_file,
                out_dir=out_dir,
                market_id=market_id,
                window_start=window_start,
            )

        out_file = out_dir / f"{market_id}.jsonl"
        rows = [json.loads(l) for l in out_file.read_text().splitlines() if l.strip()]
        ts_list = [row["ts_ms"] for row in rows]
        assert ts_list == sorted(ts_list), "ts_ms values must be ascending"


# ---------------------------------------------------------------------------
# Full run_backfill integration test (mocked)
# ---------------------------------------------------------------------------

class TestRunBackfillIntegration:
    """Test the top-level run_backfill function across multiple dome files."""

    def test_run_backfill_processes_all_dome_files(self, tmp_path):
        """run_backfill must process all .jsonl dome files in the dome_dir."""
        dome_dir = tmp_path / "dome_snapshots"
        out_dir = tmp_path / "dome_snapshots_binance_prewindow"

        # Create 3 dome files
        markets = [
            ("btc-updown-15m-1774742400", 1774742400),
            ("btc-updown-15m-1774743300", 1774743300),
            ("btc-updown-15m-1774744200", 1774744200),
        ]
        for mid, ws in markets:
            _make_dome_file(tmp_path, mid, ws)

        mock_resp = MagicMock()
        mock_resp.json.return_value = _make_kline_response(
            start_ms=1774742280 * 1000,
            count=5,
        )
        mock_resp.raise_for_status.return_value = None

        with patch("tools.dome_binance_backfill.requests.get", return_value=mock_resp):
            with patch("tools.dome_binance_backfill.time.sleep"):  # skip rate-limit sleep
                backfill.run_backfill(dome_dir=dome_dir, out_dir=out_dir)

        # All 3 output files must exist
        for mid, _ in markets:
            assert (out_dir / f"{mid}.jsonl").exists(), f"Missing output for {mid}"

    def test_run_backfill_skips_already_done(self, tmp_path):
        """Markets with existing output files must not trigger HTTP calls."""
        dome_dir = tmp_path / "dome_snapshots"
        out_dir = tmp_path / "dome_snapshots_binance_prewindow"
        out_dir.mkdir(parents=True)

        market_id = "btc-updown-15m-1774742400"
        window_start = 1774742400
        _make_dome_file(tmp_path, market_id, window_start)
        # Pre-create output
        (out_dir / f"{market_id}.jsonl").write_text(
            '{"ts_ms": 1774742280000, "close_price": 83000.0}\n',
            encoding="utf-8",
        )

        call_count = []

        def fake_get(*args, **kwargs):
            call_count.append(1)
            return MagicMock()

        with patch("tools.dome_binance_backfill.requests.get", side_effect=fake_get):
            with patch("tools.dome_binance_backfill.time.sleep"):
                backfill.run_backfill(dome_dir=dome_dir, out_dir=out_dir)

        assert len(call_count) == 0, "Should not have called HTTP for already-done market"

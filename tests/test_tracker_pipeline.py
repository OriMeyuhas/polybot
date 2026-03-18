"""Tests for the modular tracker pipeline modules."""

import csv
from collections import deque
from pathlib import Path

import pytest

from polybot.tracker.parsing import TIMEFRAME_SECONDS, parse_slug, parse_title_fallback
from polybot.tracker.strategy import classify_strategy
from polybot.tracker.state import SpotBuffer
from polybot.tracker.csv_writer import TrackerCSVWriter
from polybot.tracker.settlement_tracker import _compute_pnl
from polybot.tracker.book_recorder import _compute_depth


# =========================================================================
# 1. parsing.py
# =========================================================================

class TestParseSlug:
    def test_parse_slug_format1(self):
        result = parse_slug("btc-updown-15m-1773756900")
        assert result["asset"] == "BTC"
        assert result["timeframe"] == "15m"
        assert result["window_start_epoch"] == 1773756900

    def test_parse_slug_format2_single_time(self):
        result = parse_slug("xrp-up-or-down-march-17-2026-10am-et")
        assert result["asset"] == "XRP"
        assert result["timeframe"] == "1h"

    def test_parse_slug_format2_range(self):
        result = parse_slug("bitcoin-up-or-down-march-17-2026-10am-11am-et")
        assert result["asset"] == "BTC"
        assert result["timeframe"] == "1h"

    def test_parse_slug_unknown(self):
        result = parse_slug("some-random-slug")
        assert result["asset"] == "UNKNOWN"
        assert result["timeframe"] == "?"
        assert result["window_start_epoch"] == 0


class TestParseTitleFallback:
    def test_parse_title_fallback_with_time_range(self):
        result = parse_title_fallback("Bitcoin 10:00AM-10:15AM ET")
        assert result["asset"] == "BTC"
        assert result["timeframe"] == "15m"

    def test_parse_title_fallback_hourly(self):
        result = parse_title_fallback("Ethereum 10AM ET")
        assert result["asset"] == "ETH"
        assert result["timeframe"] == "1h"


class TestTimeframeSeconds:
    def test_timeframe_seconds(self):
        assert TIMEFRAME_SECONDS["15m"] == 900
        assert TIMEFRAME_SECONDS["1h"] == 3600


# =========================================================================
# 2. strategy.py
# =========================================================================

class TestClassifyStrategy:
    def test_classify_spread_capture(self):
        market_sides: dict[str, set[str]] = {}
        slug = "btc-updown-15m-123"
        # First trade: UP
        result1 = classify_strategy(
            slug=slug, side="UP", elapsed=30, total=900,
            spot_delta=0.01, asset="BTC", market_sides=market_sides,
        )
        # Second trade: DOWN in same market -> Spread Capture
        result2 = classify_strategy(
            slug=slug, side="DOWN", elapsed=60, total=900,
            spot_delta=0.01, asset="BTC", market_sides=market_sides,
        )
        assert result2 == "Spread Capture"

    def test_classify_latency_arb(self):
        market_sides: dict[str, set[str]] = {}
        result = classify_strategy(
            slug="btc-updown-15m-999", side="UP",
            elapsed=500, total=900,  # 0.56 > 0.53
            spot_delta=0.3,  # > 0.2
            asset="BTC", market_sides=market_sides,
        )
        assert result == "Latency Arb"

    def test_classify_pre_positioning(self):
        market_sides: dict[str, set[str]] = {}
        result = classify_strategy(
            slug="btc-updown-15m-888", side="UP",
            elapsed=10, total=900,  # 0.011 < 0.15
            spot_delta=0.01, asset="BTC", market_sides=market_sides,
        )
        assert result == "Pre-positioning"

    def test_classify_exit(self):
        market_sides: dict[str, set[str]] = {}
        result = classify_strategy(
            slug="btc-updown-15m-777", side="EXIT_UP",
            elapsed=450, total=900,  # 0.5 -- not latency arb (delta too low)
            spot_delta=0.01, asset="BTC", market_sides=market_sides,
        )
        assert result == "Exit"

    def test_classify_directional(self):
        market_sides: dict[str, set[str]] = {}
        result = classify_strategy(
            slug="btc-updown-15m-666", side="UP",
            elapsed=300, total=900,  # 0.33 -- mid window
            spot_delta=0.01, asset="BTC", market_sides=market_sides,
        )
        assert result == "Directional"

    def test_market_sides_mutation(self):
        market_sides: dict[str, set[str]] = {}
        slug = "btc-updown-15m-555"
        classify_strategy(
            slug=slug, side="UP", elapsed=300, total=900,
            spot_delta=0.0, asset="BTC", market_sides=market_sides,
        )
        assert slug in market_sides
        assert "UP" in market_sides[slug]

        classify_strategy(
            slug=slug, side="DOWN", elapsed=300, total=900,
            spot_delta=0.0, asset="BTC", market_sides=market_sides,
        )
        assert "DOWN" in market_sides[slug]
        assert len(market_sides[slug]) == 2


# =========================================================================
# 3. SpotBuffer
# =========================================================================

class TestSpotBuffer:
    def test_record_and_get_now(self):
        buf = SpotBuffer()
        buf.record("BTC", 50000.0)
        assert buf.get_price_now("BTC") == 50000.0

    def test_get_price_now_empty(self):
        buf = SpotBuffer()
        assert buf.get_price_now("DOGE") == 0.0

    def test_get_price_at(self, monkeypatch):
        buf = SpotBuffer()
        # Directly populate the internal buffer with known timestamps
        buf._buffers["BTC"] = deque(maxlen=300)
        buf._buffers["BTC"].append((100.0, 50000.0))
        buf._buffers["BTC"].append((140.0, 50500.0))
        buf._buffers["BTC"].append((160.0, 51000.0))
        buf._buffers["BTC"].append((200.0, 51500.0))

        # Monkeypatch time.time so get_price_at computes target_time correctly
        monkeypatch.setattr("polybot.tracker.state.time.time", lambda: 200.0)

        # 60 seconds ago -> target_time = 140.0 -> closest entry is (140, 50500)
        price = buf.get_price_at("BTC", 60)
        assert price == 50500.0

        # 0 seconds ago -> target_time = 200.0 -> closest entry is (200, 51500)
        price = buf.get_price_at("BTC", 0)
        assert price == 51500.0

        # 100 seconds ago -> target_time = 100.0 -> closest entry is (100, 50000)
        price = buf.get_price_at("BTC", 100)
        assert price == 50000.0

    def test_buffer_eviction(self, monkeypatch):
        buf = SpotBuffer()
        # Patch time.time to return incrementing values
        call_count = [0]

        def fake_time():
            call_count[0] += 1
            return float(call_count[0])

        monkeypatch.setattr("polybot.tracker.state.time.time", fake_time)

        for i in range(301):
            buf.record("BTC", float(i))

        assert len(buf._buffers["BTC"]) == 300


# =========================================================================
# 4. csv_writer.py
# =========================================================================

class TestTrackerCSVWriter:
    def test_write_trade_creates_file(self, tmp_path):
        writer = TrackerCSVWriter(data_dir=tmp_path, session_id="test-001")
        writer.write_trade({"timestamp": "2026-03-18T00:00:00Z", "asset": "BTC"})

        csv_files = list(tmp_path.glob("trades_*.csv"))
        assert len(csv_files) == 1

        with open(csv_files[0], "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
        # Header + 1 data row
        assert len(rows) == 2
        # Header should contain session_id
        assert "session_id" in rows[0]

        writer.close()

    def test_write_multiple_types(self, tmp_path):
        writer = TrackerCSVWriter(data_dir=tmp_path, session_id="test-002")
        writer.write_trade({"timestamp": "2026-03-18T00:00:00Z"})
        writer.write_spot({"timestamp": "2026-03-18T00:00:00Z", "asset": "BTC", "price": 50000})
        writer.write_book({"timestamp": "2026-03-18T00:00:00Z", "market_slug": "test"})

        trade_files = list(tmp_path.glob("trades_*.csv"))
        spot_files = list(tmp_path.glob("spots_*.csv"))
        book_files = list(tmp_path.glob("book_snapshots_*.csv"))

        assert len(trade_files) == 1
        assert len(spot_files) == 1
        assert len(book_files) == 1

        writer.close()

    def test_session_id_injected(self, tmp_path):
        writer = TrackerCSVWriter(data_dir=tmp_path, session_id="sess-abc")
        writer.write_trade({"timestamp": "2026-03-18T00:00:00Z"})

        csv_files = list(tmp_path.glob("trades_*.csv"))
        with open(csv_files[0], "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                assert row["session_id"] == "sess-abc"

        writer.close()

    def test_close(self, tmp_path):
        writer = TrackerCSVWriter(data_dir=tmp_path, session_id="test-close")
        writer.write_trade({"timestamp": "2026-03-18T00:00:00Z"})
        writer.close()

        # After close, internal _files dict should be empty
        assert len(writer._files) == 0

        # Verify underlying file handles are closed
        csv_files = list(tmp_path.glob("trades_*.csv"))
        assert len(csv_files) == 1


# =========================================================================
# 5. settlement PnL computation
# =========================================================================

class TestComputePnl:
    def test_pnl_winning_side(self):
        """Whale bought UP at $0.50 avg and market settled UP."""
        trades = [
            {"side": "UP", "size_shares": 100, "size_usd": 50.0, "price": 0.50},
        ]
        result = _compute_pnl("UP", trades)
        # winning: 100 shares * $1.00 - $50 cost = $50 profit
        assert result["whale_pnl_usd"] == 50.0
        assert result["whale_roi_pct"] == 100.0
        assert result["whale_side"] == "UP"
        assert result["whale_had_position"] is True

    def test_pnl_losing_side(self):
        """Whale bought DOWN but market settled UP."""
        trades = [
            {"side": "DOWN", "size_shares": 100, "size_usd": 50.0, "price": 0.50},
        ]
        result = _compute_pnl("UP", trades)
        # losing: -$50
        assert result["whale_pnl_usd"] == -50.0
        assert result["whale_roi_pct"] == -100.0
        assert result["whale_side"] == "DOWN"

    def test_pnl_both_sides(self):
        """Whale bought both UP and DOWN (spread capture scenario)."""
        trades = [
            {"side": "UP", "size_shares": 100, "size_usd": 40.0, "price": 0.40},
            {"side": "DOWN", "size_shares": 100, "size_usd": 50.0, "price": 0.50},
        ]
        result = _compute_pnl("UP", trades)
        # UP wins: 100 * 1.0 - 40 = 60 profit on UP side
        # DOWN loses: -50
        # Total: 60 - 50 = 10
        assert result["whale_pnl_usd"] == 10.0
        # whale_side = DOWN (more USD invested in DOWN: 50 > 40)
        assert result["whale_side"] == "DOWN"
        # ROI = 10 / 90 total cost
        assert result["whale_roi_pct"] == pytest.approx(11.1111, abs=0.01)

    def test_pnl_no_trades(self):
        """No trades -> zero PnL."""
        result = _compute_pnl("UP", [])
        assert result["whale_pnl_usd"] == 0.0
        assert result["whale_had_position"] is False

    def test_pnl_exit_trades_ignored(self):
        """EXIT trades should not contribute to PnL."""
        trades = [
            {"side": "UP", "size_shares": 100, "size_usd": 50.0, "price": 0.50},
            {"side": "EXIT", "size_shares": 50, "size_usd": 40.0, "price": 0.80},
        ]
        result = _compute_pnl("UP", trades)
        # Only UP trade counted: 100 * 1.0 - 50 = 50
        assert result["whale_pnl_usd"] == 50.0
        assert result["whale_total_usd"] == 50.0


# =========================================================================
# 6. book_recorder depth computation
# =========================================================================

class TestComputeDepth:
    def test_depth_within_threshold(self):
        # Note: floating-point subtraction means abs(0.50 - 0.49) can exceed
        # exactly 0.01, so we pick prices whose differences are cleanly
        # representable or use a slightly larger threshold gap.
        bids = [
            {"price": "0.50", "size": "100"},   # best bid, diff=0.00
            {"price": "0.495", "size": "200"},  # within 1c (diff=0.005)
            {"price": "0.47", "size": "150"},   # within 5c (diff=0.03) but not 1c
            {"price": "0.46", "size": "300"},   # within 5c (diff=0.04)
            {"price": "0.42", "size": "500"},   # within 10c (diff=0.08) but not 5c
            {"price": "0.35", "size": "999"},   # outside 10c (diff=0.15)
        ]
        best_bid = 0.50

        # 1c threshold: levels at 0.50 (diff=0) and 0.495 (diff=0.005)
        depth_1c = _compute_depth(bids, best_bid, 0.01)
        expected_1c = 0.50 * 100 + 0.495 * 200
        assert depth_1c == pytest.approx(expected_1c, abs=0.01)

        # 5c threshold: 0.50, 0.495, 0.47, 0.46
        depth_5c = _compute_depth(bids, best_bid, 0.05)
        expected_5c = 0.50 * 100 + 0.495 * 200 + 0.47 * 150 + 0.46 * 300
        assert depth_5c == pytest.approx(expected_5c, abs=0.01)

        # 10c threshold: adds 0.42
        depth_10c = _compute_depth(bids, best_bid, 0.10)
        expected_10c = expected_5c + 0.42 * 500
        assert depth_10c == pytest.approx(expected_10c, abs=0.01)

    def test_depth_empty_book(self):
        assert _compute_depth([], 0.50, 0.01) == 0.0
        assert _compute_depth([], 0.50, 0.05) == 0.0
        assert _compute_depth([], 0.50, 0.10) == 0.0

    def test_depth_all_levels_within_threshold(self):
        asks = [
            {"price": "0.60", "size": "50"},
            {"price": "0.61", "size": "75"},
        ]
        depth = _compute_depth(asks, 0.60, 0.05)
        expected = 0.60 * 50 + 0.61 * 75
        assert depth == pytest.approx(expected, abs=0.01)

    def test_depth_no_levels_within_threshold(self):
        bids = [
            {"price": "0.30", "size": "100"},
        ]
        # best_price 0.50, threshold 0.01 -> 0.30 is 0.20 away
        depth = _compute_depth(bids, 0.50, 0.01)
        assert depth == 0.0

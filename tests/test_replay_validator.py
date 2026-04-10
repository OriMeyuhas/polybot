"""Tests for tools/replay_validator.py — Proposal #45."""
import json
import pathlib
import sys
import time

import pytest

# Make tools/ importable
_tools_dir = pathlib.Path(__file__).parent.parent / "tools"
if str(_tools_dir) not in sys.path:
    sys.path.insert(0, str(_tools_dir))

from replay_validator import (
    BookQuote,
    BookSnapshot,
    FillRecord,
    ValidationResult,
    find_nearest_book,
    find_quote_at,
    check_fill_against_book,
    load_fills,
    load_market_token_map,
    load_book_index,
    load_book_quotes,
    validate,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _fill(ts: float, market_id: str, side: str, price: float, size: float, order_id: str = "ord1") -> dict:
    return {"ts": ts, "event": "fill", "market_id": market_id, "side": side,
            "price": price, "size": size, "order_id": order_id, "reason": "detected"}


def _book_snap(ts: float, token_id: str, best_bid: float, best_ask: float, ask_size: float) -> dict:
    """Create a minimal book snapshot record (full-depth 'book' event)."""
    return {
        "ts": ts,
        "token_id": token_id[:20],
        "event_type": "book",
        "data": {
            "asset_id": token_id,
            "bids": [{"price": str(best_bid), "size": "100"}],
            "asks": [{"price": str(best_ask), "size": str(ask_size)}],
        },
    }


def _price_change(ts: float, asset_id: str, best_bid: float, best_ask: float) -> dict:
    """Create a minimal price_change event record."""
    return {
        "ts": ts,
        "token_id": "",
        "event_type": "price_change",
        "data": {
            "market": "0xdeadbeef",
            "price_changes": [
                {
                    "asset_id": asset_id,
                    "price": str(best_bid),
                    "size": "100",
                    "side": "BUY",
                    "hash": "abc",
                    "best_bid": str(best_bid),
                    "best_ask": str(best_ask),
                }
            ],
            "timestamp": str(int(ts * 1000)),
            "event_type": "price_change",
        },
    }


def _book_quote(ts: float, token_id: str, best_bid: float, best_ask: float,
                bids=None, asks=None) -> BookQuote:
    """Create a BookQuote directly for unit tests."""
    return BookQuote(
        ts=ts,
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bids=bids,
        asks=asks,
    )


def _market_event(ts: float, market_id: str, up_token: str, dn_token: str) -> dict:
    return {
        "ts": ts,
        "event": "discovered",
        "market_id": market_id,
        "asset": "BTC",
        "timeframe_sec": 900,
        "metadata": {
            "open_epoch": int(ts),
            "close_epoch": int(ts) + 900,
            "up_token_id": up_token,
            "dn_token_id": dn_token,
        },
    }


# ---------------------------------------------------------------------------
# Unit tests for core functions
# ---------------------------------------------------------------------------

class TestFindNearestBook:
    """Tests for find_nearest_book (backward-compat wrapper) and find_quote_at."""

    def test_finds_exact_match(self):
        quotes = [_book_quote(100.0, "t", 0.44, 0.46)]
        result = find_nearest_book(quotes, 100.0, max_delta_sec=2.0)
        assert result is not None
        assert result.ts == 100.0

    def test_finds_quote_before_fill(self):
        """Quote at T=100, fill at T=101.5 — quote is before fill, should be used."""
        quotes = [_book_quote(100.0, "t", 0.44, 0.46)]
        result = find_nearest_book(quotes, 101.5, max_delta_sec=2.0)
        assert result is not None
        assert result.ts == 100.0

    def test_returns_none_if_stale(self):
        """Quote at T=100, fill at T=200 — 100s gap, stale_sec=60 → None."""
        quotes = [_book_quote(100.0, "t", 0.44, 0.46)]
        # find_nearest_book uses stale_sec = min(max_delta_sec * 30, 60) = 60
        result = find_nearest_book(quotes, 200.0, max_delta_sec=2.0)
        assert result is None

    def test_returns_none_if_empty(self):
        result = find_nearest_book([], 100.0, max_delta_sec=2.0)
        assert result is None

    def test_picks_most_recent_before_fill(self):
        """Fill at T=102 — should use T=100.5, not T=103 (future) or T=98 (older)."""
        quotes = [
            _book_quote(98.0, "t", 0.44, 0.46),
            _book_quote(100.5, "t", 0.44, 0.46),
            _book_quote(103.0, "t", 0.44, 0.46),  # future — must NOT be used
        ]
        result = find_nearest_book(quotes, 102.0, max_delta_sec=2.0)
        assert result is not None
        assert result.ts == 100.5


class TestFindQuoteAt:
    """Tests for find_quote_at — the new primary lookup function."""

    def test_uses_most_recent_quote(self):
        """Fill at T=100, last quote at T=50 → should be used (within 60s)."""
        quotes = [_book_quote(50.0, "t", 0.44, 0.46)]
        result = find_quote_at(quotes, 100.0, stale_sec=60.0)
        assert result is not None
        assert result.ts == 50.0

    def test_rejects_stale_quote(self):
        """Fill at T=1000, last quote at T=800 → stale (200s > 60s) → None."""
        quotes = [_book_quote(800.0, "t", 0.44, 0.46)]
        result = find_quote_at(quotes, 1000.0, stale_sec=60.0)
        assert result is None

    def test_rejects_future_quote(self):
        """All quotes in the future → None."""
        quotes = [_book_quote(200.0, "t", 0.44, 0.46)]
        result = find_quote_at(quotes, 100.0, stale_sec=60.0)
        assert result is None

    def test_picks_most_recent_not_future(self):
        """Fill at T=102, quotes at T=98, T=100, T=105 → should pick T=100."""
        quotes = [
            _book_quote(98.0, "t", 0.44, 0.46),
            _book_quote(100.0, "t", 0.44, 0.46),
            _book_quote(105.0, "t", 0.44, 0.46),  # future
        ]
        result = find_quote_at(quotes, 102.0, stale_sec=60.0)
        assert result is not None
        assert result.ts == 100.0

    def test_empty_list(self):
        assert find_quote_at([], 100.0) is None


class TestCheckFillAgainstBook:
    """Tests for check_fill_against_book with BookQuote objects."""

    def _quote(self, best_bid: float, best_ask: float, ask_size: float,
               with_depth: bool = True) -> BookQuote:
        """Create a BookQuote. If with_depth=True, includes full bids/asks lists."""
        if with_depth:
            return BookQuote(
                ts=100.0, token_id="tok",
                best_bid=best_bid, best_ask=best_ask,
                bids=[{"price": str(best_bid), "size": "100"}],
                asks=[{"price": str(best_ask), "size": str(ask_size)}],
            )
        return BookQuote(
            ts=100.0, token_id="tok",
            best_bid=best_bid, best_ask=best_ask,
            bids=None, asks=None,
        )

    def test_realistic_fill(self):
        quote = self._quote(best_bid=0.44, best_ask=0.46, ask_size=100)
        fill = FillRecord(ts=100.0, market_id="m", side="UP", price=0.45, size=10, order_id="o")
        assert check_fill_against_book(fill, quote) == "realistic"

    def test_price_outside_spread_too_high(self):
        quote = self._quote(best_bid=0.44, best_ask=0.46, ask_size=100)
        fill = FillRecord(ts=100.0, market_id="m", side="UP", price=0.50, size=10, order_id="o")
        assert check_fill_against_book(fill, quote) == "price_outside_spread"

    def test_realistic_fill_no_depth(self):
        """price_change event quote (no depth) — skips size check, returns realistic."""
        quote = self._quote(best_bid=0.44, best_ask=0.46, ask_size=100, with_depth=False)
        fill = FillRecord(ts=100.0, market_id="m", side="UP", price=0.45, size=10, order_id="o")
        assert check_fill_against_book(fill, quote) == "realistic"

    def test_insufficient_size(self):
        """Fill at price 0.45 when book has ask only at 0.90 (far from fill) and tiny total size."""
        # asks only at 0.90 (>2c from fill_price=0.45), total_ask_size=1 < fill.size=100
        quote = BookQuote(
            ts=100.0, token_id="tok",
            best_bid=0.44, best_ask=0.90,
            bids=[{"price": "0.44", "size": "100"}],
            asks=[{"price": "0.90", "size": "1"}],  # no asks near 0.45
        )
        fill = FillRecord(ts=100.0, market_id="m", side="UP", price=0.45, size=100, order_id="o")
        # available_ask_size at [0.43..0.47] = 0, total_ask_size=1 < fill.size=100
        assert check_fill_against_book(fill, quote) == "insufficient_size"


# ---------------------------------------------------------------------------
# Integration tests with temp JSONL files
# ---------------------------------------------------------------------------

class TestLoadAndValidate:
    def test_load_fills_filters_non_fill_events(self, tmp_path):
        log = tmp_path / "order_log_2026-04-09.jsonl"
        records = [
            {"ts": 1.0, "event": "post", "market_id": "m1", "side": "UP", "price": 0.45, "size": 10, "order_id": "o1", "reason": "ladder"},
            _fill(2.0, "m1", "UP", 0.45, 10, "o2"),
            {"ts": 3.0, "event": "cancel", "market_id": "m1", "side": "DN", "price": 0.40, "size": 5, "order_id": "o3", "reason": "fv"},
        ]
        _write_jsonl(log, records)
        fills = load_fills(log)
        assert len(fills) == 1
        assert fills[0].order_id == "o2"

    def test_load_market_token_map(self, tmp_path):
        log = tmp_path / "market_event_log_2026-04-09.jsonl"
        records = [
            _market_event(1000.0, "btc-15m-100", "tok_up_full_id", "tok_dn_full_id"),
        ]
        _write_jsonl(log, records)
        mapping = load_market_token_map(log)
        assert "btc-15m-100" in mapping
        assert mapping["btc-15m-100"]["up_token_id"] == "tok_up_full_id"
        assert mapping["btc-15m-100"]["dn_token_id"] == "tok_dn_full_id"

    def test_load_market_token_map_old_format(self, tmp_path):
        """Old format without token_ids returns empty mapping for that market."""
        log = tmp_path / "market_event_log_2026-04-09.jsonl"
        records = [{"ts": 1000.0, "event": "discovered", "market_id": "btc-15m-100",
                    "asset": "BTC", "timeframe_sec": 900,
                    "metadata": {"open_epoch": 1000, "close_epoch": 1900}}]
        _write_jsonl(log, records)
        mapping = load_market_token_map(log)
        # Old format: no token IDs → market not added
        assert "btc-15m-100" not in mapping

    def test_full_pipeline_realistic(self, tmp_path):
        """End-to-end: fill at price 0.45, book has ask=0.46 with 100 shares → realistic."""
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        ts = 1000.0
        up_tok = "tok_up_full_id_abc123"
        dn_tok = "tok_dn_full_id_abc123"

        _write_jsonl(order_log, [_fill(ts, "btc-15m-100", "UP", 0.45, 10, "o1")])
        _write_jsonl(book_log, [_book_snap(ts - 0.5, up_tok, 0.44, 0.46, 100)])
        _write_jsonl(market_log, [_market_event(ts - 10, "btc-15m-100", up_tok, dn_tok)])

        results = run(date=date, data_dir=tmp_path, max_delta_sec=2.0)
        assert len(results) == 1
        assert results[0].verdict == "realistic"

    def test_full_pipeline_no_book(self, tmp_path):
        """Fill with no market token map → no_book verdict."""
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        ts = 1000.0
        _write_jsonl(order_log, [_fill(ts, "btc-15m-unknown", "UP", 0.45, 10, "o1")])
        _write_jsonl(book_log, [_book_snap(ts, "tok_some_other", 0.44, 0.46, 100)])
        _write_jsonl(market_log, [])  # empty — no token map

        results = run(date=date, data_dir=tmp_path, max_delta_sec=2.0)
        assert len(results) == 1
        assert results[0].verdict == "no_book"

    def test_full_pipeline_price_outside_spread(self, tmp_path):
        """Fill at 0.55 when book ask is 0.46 → price_outside_spread."""
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        ts = 1000.0
        up_tok = "tok_up_full_id_abc456"
        dn_tok = "tok_dn_full_id_abc456"

        _write_jsonl(order_log, [_fill(ts, "btc-15m-200", "UP", 0.55, 10, "o2")])
        _write_jsonl(book_log, [_book_snap(ts, up_tok, 0.44, 0.46, 100)])
        _write_jsonl(market_log, [_market_event(ts - 10, "btc-15m-200", up_tok, dn_tok)])

        results = run(date=date, data_dir=tmp_path, max_delta_sec=2.0)
        assert len(results) == 1
        assert results[0].verdict == "price_outside_spread"

    def test_full_pipeline_insufficient_size(self, tmp_path):
        """Fill of 100 shares when book has asks only far from fill price → insufficient_size."""
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        ts = 1000.0
        up_tok = "tok_up_full_id_abc789"
        dn_tok = "tok_dn_full_id_abc789"

        # Fill at 0.45 but book only has asks at 0.90 (far away) and tiny size
        book_rec = {
            "ts": ts,
            "token_id": up_tok[:20],
            "event_type": "book",
            "data": {
                "asset_id": up_tok,
                "bids": [{"price": "0.44", "size": "100"}],
                "asks": [{"price": "0.90", "size": "1"}],  # no asks near fill price 0.45
            },
        }
        _write_jsonl(order_log, [_fill(ts, "btc-15m-300", "UP", 0.45, 100, "o3")])
        _write_jsonl(book_log, [book_rec])
        _write_jsonl(market_log, [_market_event(ts - 10, "btc-15m-300", up_tok, dn_tok)])

        results = run(date=date, data_dir=tmp_path, max_delta_sec=2.0)
        assert len(results) == 1
        assert results[0].verdict == "insufficient_size"

    def test_mixed_verdicts_counts(self, tmp_path):
        """Multiple fills with known verdicts — assert correct per-verdict counts."""
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        ts = 1000.0

        # Market A: UP token
        up_a = "tok_up_aaa_full_id"
        dn_a = "tok_dn_aaa_full_id"

        fills = [
            _fill(ts, "mkt-A", "UP", 0.45, 10, "oa1"),   # realistic
            _fill(ts + 0.5, "mkt-A", "UP", 0.55, 10, "oa2"),  # price_outside_spread
            _fill(ts + 1.0, "mkt-B", "UP", 0.45, 10, "ob1"),   # no_book (no token map)
        ]
        books = [
            _book_snap(ts - 0.3, up_a, 0.44, 0.46, 100),
        ]
        events = [
            _market_event(ts - 10, "mkt-A", up_a, dn_a),
            # mkt-B not in market_event_log
        ]

        _write_jsonl(order_log, fills)
        _write_jsonl(book_log, books)
        _write_jsonl(market_log, events)

        results = run(date=date, data_dir=tmp_path, max_delta_sec=2.0)
        assert len(results) == 3

        verdicts = [r.verdict for r in results]
        assert verdicts.count("realistic") == 1
        assert verdicts.count("price_outside_spread") == 1
        assert verdicts.count("no_book") == 1


# ---------------------------------------------------------------------------
# New tests for price_change events and running book state (Bug fixes)
# ---------------------------------------------------------------------------

class TestLoadPriceChangeEvents:
    """Test that load_book_quotes correctly handles price_change events."""

    def test_loads_price_change_events(self, tmp_path):
        """Seed a book_log with price_change events, assert quotes are loaded."""
        book_log = tmp_path / "book_log_test.jsonl"
        tok = "tok_full_id_12345"
        _write_jsonl(book_log, [
            _price_change(ts=1000.0, asset_id=tok, best_bid=0.44, best_ask=0.46),
            _price_change(ts=1001.0, asset_id=tok, best_bid=0.45, best_ask=0.47),
        ])
        index = load_book_quotes(book_log, {tok})
        assert tok in index
        quotes = index[tok]
        assert len(quotes) == 2
        assert quotes[0].ts == 1000.0
        assert quotes[0].best_bid == 0.44
        assert quotes[0].best_ask == 0.46
        assert quotes[0].bids is None   # price_change has no depth
        assert quotes[0].asks is None
        assert quotes[1].ts == 1001.0
        assert quotes[1].best_bid == 0.45

    def test_loads_both_book_and_price_change(self, tmp_path):
        """Mixed log with both 'book' and 'price_change' events — all loaded."""
        book_log = tmp_path / "book_log_test.jsonl"
        tok = "tok_full_id_67890"
        _write_jsonl(book_log, [
            _book_snap(ts=1000.0, token_id=tok, best_bid=0.44, best_ask=0.46, ask_size=100),
            _price_change(ts=1001.0, asset_id=tok, best_bid=0.45, best_ask=0.47),
        ])
        index = load_book_quotes(book_log, {tok})
        assert tok in index
        quotes = index[tok]
        assert len(quotes) == 2
        # Book event has depth
        book_quote = next(q for q in quotes if q.ts == 1000.0)
        assert book_quote.bids is not None
        assert book_quote.asks is not None
        # Price_change event has no depth
        pc_quote = next(q for q in quotes if q.ts == 1001.0)
        assert pc_quote.bids is None

    def test_ignores_unrelated_tokens(self, tmp_path):
        """price_change events for tokens not in token_ids set are ignored."""
        book_log = tmp_path / "book_log_test.jsonl"
        tok_wanted = "tok_wanted_id"
        tok_other = "tok_other_id"
        _write_jsonl(book_log, [
            _price_change(ts=1000.0, asset_id=tok_wanted, best_bid=0.44, best_ask=0.46),
            _price_change(ts=1001.0, asset_id=tok_other, best_bid=0.50, best_ask=0.52),
        ])
        index = load_book_quotes(book_log, {tok_wanted})
        assert tok_wanted in index
        assert tok_other not in index


class TestRunningBookState:
    """Test that fills are validated against the correct book state (not nearest ±2s)."""

    def test_uses_most_recent_quote_not_nearest(self, tmp_path):
        """
        Fill at T=100. Book has quotes at T=50 (bid=0.44, ask=0.46) and T=200 (future).
        Should use T=50 quote (not reject because >2s old), not T=200 (future).
        """
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        tok = "tok_up_running_state_test"
        dn_tok = "tok_dn_running_state_test"

        # Fill at T=100, but book quote is at T=50 (50s before fill)
        _write_jsonl(order_log, [_fill(ts=100.0, market_id="mkt-run", side="UP", price=0.45, size=10)])
        _write_jsonl(book_log, [
            _price_change(ts=50.0, asset_id=tok, best_bid=0.44, best_ask=0.46),
            _price_change(ts=200.0, asset_id=tok, best_bid=0.50, best_ask=0.52),  # future
        ])
        _write_jsonl(market_log, [_market_event(ts=1.0, market_id="mkt-run", up_token=tok, dn_token=dn_tok)])

        results = run(date=date, data_dir=tmp_path, stale_sec=60.0)
        assert len(results) == 1
        # Should be "realistic" (price 0.45 within spread 0.44-0.46), not "no_book"
        assert results[0].verdict == "realistic", f"Expected realistic, got {results[0].verdict}"

    def test_realistic_fill_within_spread(self, tmp_path):
        """Fill price between best_bid and best_ask → realistic."""
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        tok = "tok_up_in_spread"
        dn_tok = "tok_dn_in_spread"

        _write_jsonl(order_log, [_fill(ts=1000.0, market_id="mkt-sp", side="UP", price=0.45, size=5)])
        _write_jsonl(book_log, [_price_change(ts=999.0, asset_id=tok, best_bid=0.44, best_ask=0.46)])
        _write_jsonl(market_log, [_market_event(ts=900.0, market_id="mkt-sp", up_token=tok, dn_token=dn_tok)])

        results = run(date=date, data_dir=tmp_path)
        assert results[0].verdict == "realistic"

    def test_price_outside_spread_from_price_change(self, tmp_path):
        """Fill price 5c above best_ask from a price_change event → price_outside_spread."""
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        tok = "tok_up_outside_spread"
        dn_tok = "tok_dn_outside_spread"

        # best_ask=0.46, fill price=0.55 → 9c above ask
        _write_jsonl(order_log, [_fill(ts=1000.0, market_id="mkt-out", side="UP", price=0.55, size=5)])
        _write_jsonl(book_log, [_price_change(ts=999.5, asset_id=tok, best_bid=0.44, best_ask=0.46)])
        _write_jsonl(market_log, [_market_event(ts=900.0, market_id="mkt-out", up_token=tok, dn_token=dn_tok)])

        results = run(date=date, data_dir=tmp_path)
        assert results[0].verdict == "price_outside_spread"

    def test_rejects_stale_quote(self, tmp_path):
        """Fill at T=1000, last quote at T=800 → stale (200s > 60s) → no_book."""
        date = "2026-04-09"
        order_log = tmp_path / f"order_log_{date}.jsonl"
        book_log = tmp_path / f"book_log_{date}.jsonl"
        market_log = tmp_path / f"market_event_log_{date}.jsonl"

        tok = "tok_up_stale_test"
        dn_tok = "tok_dn_stale_test"

        _write_jsonl(order_log, [_fill(ts=1000.0, market_id="mkt-stale", side="UP", price=0.45, size=5)])
        _write_jsonl(book_log, [_price_change(ts=800.0, asset_id=tok, best_bid=0.44, best_ask=0.46)])
        _write_jsonl(market_log, [_market_event(ts=700.0, market_id="mkt-stale", up_token=tok, dn_token=dn_tok)])

        results = run(date=date, data_dir=tmp_path, stale_sec=60.0)
        assert results[0].verdict == "no_book", f"Expected no_book (stale), got {results[0].verdict}"


# ---------------------------------------------------------------------------
# Fix #52 — --debug-outside-spread flag
# ---------------------------------------------------------------------------

class TestDebugOutsideSpread:
    """Tests for print_report(debug_outside_spread=N) — Proposal #52."""

    def _make_outside_result(self, ts: float, market_id: str, side: str,
                              fill_price: float, best_ask: float) -> ValidationResult:
        """Build a ValidationResult with verdict='price_outside_spread' and real book context."""
        fill = FillRecord(
            ts=ts, market_id=market_id, side=side,
            price=fill_price, size=10.0, order_id="o1",
        )
        book = BookQuote(
            ts=ts - 0.5, token_id="tok_up",
            best_bid=best_ask - 0.01, best_ask=best_ask,
            bids=None, asks=None,
        )
        return ValidationResult(fill=fill, token_id="tok_up", book=book,
                                verdict="price_outside_spread")

    def test_debug_flag_outputs_outside_spread_fills(self, capsys):
        """With debug_outside_spread=5, print_report emits the table header."""
        from replay_validator import print_report
        results = [
            self._make_outside_result(1775858581.12, "btc-updown-15m-1775858400", "UP", 0.40, 0.31),
            self._make_outside_result(1775858578.00, "btc-updown-15m-1775858400", "UP", 0.41, 0.36),
        ]
        print_report(results, debug_outside_spread=5)
        captured = capsys.readouterr()
        assert "outside-spread fills" in captured.out
        assert "fill_price" in captured.out
        assert "real_best_ask" in captured.out
        # Verify the largest-gap fill (delta +0.09) appears before the smaller one (+0.05)
        idx_large = captured.out.index("0.400")
        idx_small = captured.out.index("0.410")
        assert idx_large < idx_small, "Largest gap fill should appear first (sorted descending)"

    def test_debug_flag_zero_does_not_print_table(self, capsys):
        """With debug_outside_spread=0 (default), the table is NOT printed."""
        from replay_validator import print_report
        results = [
            self._make_outside_result(1775858581.12, "btc-updown-15m-1775858400", "UP", 0.40, 0.31),
        ]
        print_report(results, debug_outside_spread=0)
        captured = capsys.readouterr()
        assert "outside-spread fills" not in captured.out

    def test_debug_flag_respects_n_limit(self, capsys):
        """With debug_outside_spread=2, only top 2 fills are shown even if more exist."""
        from replay_validator import print_report
        # 5 outside-spread fills with varying gaps
        results = [
            self._make_outside_result(1000.0 + i, "mkt-test", "UP", 0.40 + i * 0.01, 0.31)
            for i in range(5)
        ]
        print_report(results, debug_outside_spread=2)
        captured = capsys.readouterr()
        # Count the separator lines in the debug table — header + 2 fill rows, not 5
        lines_with_mkt = [ln for ln in captured.out.splitlines() if "mkt-test" in ln]
        assert len(lines_with_mkt) == 2, f"Expected 2 fill rows, got {len(lines_with_mkt)}"

    def test_argparse_accepts_debug_outside_spread_flag(self):
        """The --debug-outside-spread argparse flag is wired correctly in __main__."""
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "replay_validator", "--help"],
            capture_output=True, text=True,
            cwd=str(pathlib.Path(__file__).parent.parent / "tools"),
        )
        assert "--debug-outside-spread" in result.stdout

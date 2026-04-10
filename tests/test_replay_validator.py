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
    BookSnapshot,
    FillRecord,
    ValidationResult,
    find_nearest_book,
    check_fill_against_book,
    load_fills,
    load_market_token_map,
    load_book_index,
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
    """Create a minimal book snapshot record."""
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
    def test_finds_exact_match(self):
        snaps = [BookSnapshot(ts=100.0, token_id="t", bids=[], asks=[])]
        result = find_nearest_book(snaps, 100.0, max_delta_sec=2.0)
        assert result is not None
        assert result.ts == 100.0

    def test_finds_within_delta(self):
        snaps = [BookSnapshot(ts=100.0, token_id="t", bids=[], asks=[])]
        result = find_nearest_book(snaps, 101.5, max_delta_sec=2.0)
        assert result is not None

    def test_returns_none_if_too_far(self):
        snaps = [BookSnapshot(ts=100.0, token_id="t", bids=[], asks=[])]
        result = find_nearest_book(snaps, 105.0, max_delta_sec=2.0)
        assert result is None

    def test_returns_none_if_empty(self):
        result = find_nearest_book([], 100.0, max_delta_sec=2.0)
        assert result is None

    def test_picks_closest_of_multiple(self):
        snaps = [
            BookSnapshot(ts=98.0, token_id="t", bids=[], asks=[]),
            BookSnapshot(ts=100.5, token_id="t", bids=[], asks=[]),
            BookSnapshot(ts=103.0, token_id="t", bids=[], asks=[]),
        ]
        result = find_nearest_book(snaps, 100.0, max_delta_sec=2.0)
        assert result.ts == 100.5


class TestCheckFillAgainstBook:
    def _book(self, best_bid: float, best_ask: float, ask_size: float) -> BookSnapshot:
        return BookSnapshot(
            ts=100.0,
            token_id="tok",
            bids=[{"price": str(best_bid), "size": "100"}],
            asks=[{"price": str(best_ask), "size": str(ask_size)}],
        )

    def test_realistic_fill(self):
        book = self._book(best_bid=0.44, best_ask=0.46, ask_size=100)
        fill = FillRecord(ts=100.0, market_id="m", side="UP", price=0.45, size=10, order_id="o")
        assert check_fill_against_book(fill, book) == "realistic"

    def test_price_outside_spread_too_high(self):
        book = self._book(best_bid=0.44, best_ask=0.46, ask_size=100)
        fill = FillRecord(ts=100.0, market_id="m", side="UP", price=0.50, size=10, order_id="o")
        assert check_fill_against_book(fill, book) == "price_outside_spread"

    def test_insufficient_size(self):
        """Fill at price 0.45 when book has ask only at 0.90 (far from fill) and tiny total size."""
        # asks only at 0.90 (>2c from fill_price=0.45), total_ask_size=1 < fill.size=100
        book = BookSnapshot(
            ts=100.0, token_id="tok",
            bids=[{"price": "0.44", "size": "100"}],
            asks=[{"price": "0.90", "size": "1"}],  # no asks near 0.45
        )
        fill = FillRecord(ts=100.0, market_id="m", side="UP", price=0.45, size=100, order_id="o")
        # available_ask_size at [0.43..0.47] = 0, total_ask_size=1 < fill.size=100
        assert check_fill_against_book(fill, book) == "insufficient_size"


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

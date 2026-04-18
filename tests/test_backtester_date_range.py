"""Test: Bug 2 — Date-range filter in run_backtest_dome / file enumeration.

The filter must include EVERY file whose window_start epoch falls within
[start_date, end_date] (inclusive, UTC midnight boundaries).  Prior runs
silently dropped entire days because either:
  (a) the epoch comparison used local time instead of UTC, or
  (b) the filename-epoch parsing failed for certain names, or
  (c) the boundary condition excluded the start-day or end-day.

Test strategy
-----------
Create a temporary directory of synthetic dome JSONL files (minimal content —
header + one Binance line so has_orderbook=False, which is fine for filter tests).
Tag each file with a known UTC epoch.  Invoke run_backtest_dome (or the filter
logic directly via a helper) and assert:
  1. Files from every day in the date range are present in the filtered list.
  2. Files before start_date and after end_date are excluded.
  3. Boundary days (start_date itself, end_date itself) are included.
  4. A day with a market spanning UTC midnight is NOT split or dropped.

We test the filter in isolation by checking the files it processes, not the
simulation results (which depend on has_orderbook / has_outcome).
"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys

import pytest

_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.backtester import BacktestConfig, run_backtest_dome


def _utc_midnight(date_str: str) -> int:
    """Return the UTC midnight epoch for a YYYY-MM-DD string."""
    d = datetime.date.fromisoformat(date_str)
    return int(datetime.datetime(d.year, d.month, d.day,
                                 tzinfo=datetime.timezone.utc).timestamp())


def _make_minimal_dome_file(directory: pathlib.Path, epoch: int) -> pathlib.Path:
    """Write a minimal dome JSONL with a valid header but no orderbook data.

    The file will be skipped by simulate_market_dome (no book data), but it
    must still be *enumerated* and *parsed* by the date filter.  We verify
    the filter counts by patching skipped_no_book in the returned stats dict.
    """
    path = directory / f"btc-updown-15m-{epoch}.jsonl"
    lines = [
        json.dumps({
            "type": "header",
            "market_slug": f"btc-updown-15m-{epoch}",
            "condition_id": f"0x{epoch:016x}",
            "up_token_id": "up_tok",
            "dn_token_id": "dn_tok",
            "window_start": epoch,
            "window_end": epoch + 900,
            "raw_market": {
                "winning_side": {"label": "Up", "id": "up_tok"},
                "extra_fields": {"price_to_beat": "84000"},
            },
        }),
        # One Binance price so file isn't empty
        json.dumps({
            "type": "binance",
            "data": {"timestamp": epoch * 1000, "value": 84000.0},
        }),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _epochs_for_date(date_str: str, count: int = 4) -> list[int]:
    """Return `count` evenly-spaced epochs within the given UTC date."""
    midnight = _utc_midnight(date_str)
    step = 86400 // count
    return [midnight + i * step for i in range(count)]


class TestDateRangeFilter:
    """run_backtest_dome must enumerate files from every day in the given range."""

    def _setup_dir(self, tmp_path: pathlib.Path, dates: list[str], count_per_day: int = 4) -> dict[str, list[int]]:
        """Create dome files for each date, return mapping date -> list of epochs."""
        date_epochs: dict[str, list[int]] = {}
        for date_str in dates:
            epochs = _epochs_for_date(date_str, count_per_day)
            date_epochs[date_str] = epochs
            for ep in epochs:
                _make_minimal_dome_file(tmp_path, ep)
        return date_epochs

    def test_all_days_in_range_are_included(self, tmp_path):
        """Files from every day in [start, end] must appear in enumerated set.

        We create 4 files per day across 7 days (2026-04-09 through 2026-04-15).
        Filter to [2026-04-10, 2026-04-13] (inclusive).
        Expect 4 days x 4 files = 16 files processed (skipped_no_book, but enumerated).
        """
        all_dates = [
            "2026-04-09",  # before range — must be excluded
            "2026-04-10",  # start boundary — must be included
            "2026-04-11",  # middle — must be included
            "2026-04-12",  # middle — must be included
            "2026-04-13",  # end boundary — must be included
            "2026-04-14",  # after range — must be excluded
            "2026-04-15",  # after range — must be excluded
        ]
        self._setup_dir(tmp_path, all_dates, count_per_day=4)
        cfg = BacktestConfig()
        stats = run_backtest_dome(
            tmp_path, cfg,
            start_date="2026-04-10",
            end_date="2026-04-13",
        )

        # Files that were enumerated = simulated + skipped_no_book + skipped_no_outcome + other
        markets_skipped = stats.get("markets_skipped", {})
        no_book = markets_skipped.get("no_book", 0)
        no_outcome = markets_skipped.get("no_settlement", 0)
        other_skip = markets_skipped.get("other", 0)
        simulated = stats.get("markets_simulated", 0)
        total_enumerated = simulated + no_book + no_outcome + other_skip

        # 4 days in range, 4 files each = 16 files must be enumerated
        assert total_enumerated == 16, (
            f"Expected 16 files enumerated for [2026-04-10, 2026-04-13] "
            f"(4 days x 4 files), got {total_enumerated}. "
            f"Breakdown: simulated={simulated}, no_book={no_book}, "
            f"no_outcome={no_outcome}, other={other_skip}. "
            f"Date-range filter is silently dropping days."
        )

    def test_start_day_boundary_is_inclusive(self, tmp_path):
        """A file whose epoch is exactly at UTC midnight of start_date must be included."""
        start_date = "2026-04-10"
        midnight_ep = _utc_midnight(start_date)

        # One file exactly at midnight (boundary)
        _make_minimal_dome_file(tmp_path, midnight_ep)
        # One file one second before midnight (must be excluded)
        _make_minimal_dome_file(tmp_path, midnight_ep - 1)

        cfg = BacktestConfig()
        stats = run_backtest_dome(tmp_path, cfg,
                                  start_date=start_date,
                                  end_date=start_date)

        markets_skipped = stats.get("markets_skipped", {})
        total = (stats.get("markets_simulated", 0)
                 + markets_skipped.get("no_book", 0)
                 + markets_skipped.get("no_settlement", 0)
                 + markets_skipped.get("other", 0))
        assert total == 1, (
            f"Expected exactly 1 file for start_date boundary (midnight epoch), "
            f"got {total}. The midnight-epoch file may be excluded or the -1s file included."
        )

    def test_end_day_boundary_is_inclusive(self, tmp_path):
        """A file at 23:45 UTC on end_date must be included; one at 00:00 of next day excluded."""
        end_date = "2026-04-13"
        next_midnight = _utc_midnight("2026-04-14")
        last_ep = next_midnight - 900  # 23:45 UTC on end_date

        _make_minimal_dome_file(tmp_path, last_ep)       # must be included
        _make_minimal_dome_file(tmp_path, next_midnight)  # must be excluded

        cfg = BacktestConfig()
        stats = run_backtest_dome(tmp_path, cfg,
                                  start_date="2026-04-13",
                                  end_date="2026-04-13")

        markets_skipped = stats.get("markets_skipped", {})
        total = (stats.get("markets_simulated", 0)
                 + markets_skipped.get("no_book", 0)
                 + markets_skipped.get("no_settlement", 0)
                 + markets_skipped.get("other", 0))
        assert total == 1, (
            f"Expected exactly 1 file for end_date boundary (23:45 on end_date), "
            f"got {total}. Either 23:45 file excluded or next-day midnight file included."
        )

    def test_mid_week_start_and_end_no_silent_drops(self, tmp_path):
        """Both start and end mid-week — no days silently dropped.

        Range: 2026-04-05 (Saturday) through 2026-04-11 (Friday) = 7 days.
        3 files per day = 21 total.  All must be enumerated.
        """
        dates = [
            "2026-04-05",
            "2026-04-06",
            "2026-04-07",
            "2026-04-08",
            "2026-04-09",
            "2026-04-10",
            "2026-04-11",
        ]
        self._setup_dir(tmp_path, dates, count_per_day=3)

        cfg = BacktestConfig()
        stats = run_backtest_dome(tmp_path, cfg,
                                  start_date="2026-04-05",
                                  end_date="2026-04-11")

        markets_skipped = stats.get("markets_skipped", {})
        total = (stats.get("markets_simulated", 0)
                 + markets_skipped.get("no_book", 0)
                 + markets_skipped.get("no_settlement", 0)
                 + markets_skipped.get("other", 0))

        assert total == 21, (
            f"Expected 21 files for 7-day mid-week range, got {total}. "
            f"Silent date drop detected."
        )

    def test_no_filter_returns_all_files(self, tmp_path):
        """Without start/end filter, all files in the directory are enumerated."""
        dates = ["2026-04-09", "2026-04-10", "2026-04-11"]
        self._setup_dir(tmp_path, dates, count_per_day=2)  # 6 total

        cfg = BacktestConfig()
        stats = run_backtest_dome(tmp_path, cfg)  # no date filter

        markets_skipped = stats.get("markets_skipped", {})
        total = (stats.get("markets_simulated", 0)
                 + markets_skipped.get("no_book", 0)
                 + markets_skipped.get("no_settlement", 0)
                 + markets_skipped.get("other", 0))
        assert total == 6, (
            f"Without date filter, expected 6 files enumerated, got {total}."
        )

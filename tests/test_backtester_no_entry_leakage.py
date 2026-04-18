"""Test: Bug 1 — Median-book leakage in load_dome_snapshot.

The entry-time book quote (up_best_bid, up_best_ask, etc.) must be derived ONLY
from snapshots that arrive within the first 30 seconds of the window (the entry
window), NOT from the full-window median.  Using the full-window median leaks
end-of-window book state into the gate decision at t=0, inflating fv_accuracy.

Test strategy
-----------
Build a synthetic Dome JSONL with two distinct book regimes:
  - Early snapshots (t=0 .. t=29s):  UP best_bid=0.28, best_ask=0.31
  - Late snapshots  (t=30 .. t=900s): UP best_bid=0.68, best_ask=0.71

With the BUG (full-window median), entry_up_best_ask ≈ (0.31+0.71)/2 = 0.51.
With the FIX (first-30s median),   entry_up_best_ask ≈ 0.31.

We assert that `dome.entry_up_best_ask` (or equivalent exposed field) equals
the early-window value, NOT the full-window median.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.backtester import load_dome_snapshot, DomeMarketData


WINDOW_START = 1_776_038_400  # arbitrary epoch
WINDOW_END = WINDOW_START + 900  # 15-minute window


def _make_ob_entry(side: str, ts_ms: int, bid: float, ask: float) -> str:
    """Return a JSON line representing an orderbook snapshot at ts_ms."""
    return json.dumps({
        "type": "orderbook",
        "side": side,
        "data": {
            "timestamp": ts_ms,
            "bids": [{"price": str(bid), "size": "100"}],
            "asks": [{"price": str(ask), "size": "100"}],
        },
    })


def _make_synthetic_dome_file(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write a JSONL file with early (0-29s) and late (30-900s) book snapshots.

    Early UP book:  bid=0.28, ask=0.31   (centered around 0.295)
    Late  UP book:  bid=0.68, ask=0.71   (centered around 0.695)

    Full-window median bid ≈ 0.48, ask ≈ 0.51
    Early-window  median bid = 0.28, ask = 0.31
    """
    path = tmp_path / "btc-updown-15m-1776038400.jsonl"
    lines: list[str] = []

    # Header
    header = {
        "type": "header",
        "market_slug": "btc-updown-15m-1776038400",
        "condition_id": "0xtest",
        "up_token_id": "up_tok",
        "dn_token_id": "dn_tok",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "raw_market": {
            "winning_side": {"label": "Up", "id": "up_tok"},
            "extra_fields": {"price_to_beat": "84000"},
        },
    }
    lines.append(json.dumps(header))

    # Early snapshots: t=0 .. t=25s (5 snapshots every 5s, all within first 30s)
    for offset_s in range(0, 30, 5):
        ts_ms = (WINDOW_START + offset_s) * 1000
        lines.append(_make_ob_entry("UP", ts_ms, bid=0.28, ask=0.31))
        lines.append(_make_ob_entry("DN", ts_ms, bid=0.47, ask=0.50))

    # Late snapshots: t=60 .. t=860s (many snapshots, well outside entry window)
    for offset_s in range(60, 900, 60):
        ts_ms = (WINDOW_START + offset_s) * 1000
        lines.append(_make_ob_entry("UP", ts_ms, bid=0.68, ask=0.71))
        lines.append(_make_ob_entry("DN", ts_ms, bid=0.47, ask=0.50))

    # Binance prices (required so file isn't empty)
    for offset_s in range(0, 900, 60):
        ts_ms = (WINDOW_START + offset_s) * 1000
        lines.append(json.dumps({
            "type": "binance",
            "data": {"timestamp": ts_ms, "value": 84000.0},
        }))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestNoEntryLeakage:
    """Assert that entry-window book quotes use only the first-30s snapshots."""

    def test_entry_up_best_ask_uses_early_window_not_full_median(self, tmp_path):
        """entry_up_best_ask must equal the early-window ask (0.31), not full median (~0.51).

        With the bug: median of [0.31, 0.31, 0.31, 0.31, 0.31, 0.71, 0.71, ...] ≈ 0.51
        With the fix: median of first-30s only [0.31, 0.31, 0.31, 0.31, 0.31] = 0.31
        """
        path = _make_synthetic_dome_file(tmp_path)
        dome = load_dome_snapshot(path)
        assert dome is not None, "load_dome_snapshot returned None for valid file"

        # The new field must exist on DomeMarketData
        assert hasattr(dome, "entry_up_best_ask"), (
            "DomeMarketData missing 'entry_up_best_ask' field — Bug 1 not fixed"
        )

        # The entry ask must come from the early window (≈0.31), not the full median (≈0.51)
        assert dome.entry_up_best_ask == pytest.approx(0.31, abs=0.01), (
            f"entry_up_best_ask={dome.entry_up_best_ask:.4f} looks like full-window median "
            f"rather than early-window value 0.31 — median-book leakage not fixed"
        )

    def test_entry_up_best_bid_uses_early_window_not_full_median(self, tmp_path):
        """entry_up_best_bid must equal the early-window bid (0.28), not full median (~0.48)."""
        path = _make_synthetic_dome_file(tmp_path)
        dome = load_dome_snapshot(path)
        assert dome is not None

        assert hasattr(dome, "entry_up_best_bid"), (
            "DomeMarketData missing 'entry_up_best_bid' field"
        )
        assert dome.entry_up_best_bid == pytest.approx(0.28, abs=0.01), (
            f"entry_up_best_bid={dome.entry_up_best_bid:.4f} looks like full-window median "
            f"rather than early-window value 0.28"
        )

    def test_full_window_ask_still_available_for_simulation(self, tmp_path):
        """The full-window median (up_best_ask) must still be accessible for fill simulation."""
        path = _make_synthetic_dome_file(tmp_path)
        dome = load_dome_snapshot(path)
        assert dome is not None

        # The full-window median should be approximately midway between early and late asks
        # Early has 5 snapshots at 0.31, late has ~14 snapshots at 0.71
        # The median of all asks will be 0.71 (late snapshots dominate numerically)
        # What matters is that it's NOT the same as the entry value
        assert dome.up_best_ask != pytest.approx(0.31, abs=0.01), (
            "up_best_ask (full-window) should differ from entry_up_best_ask (early-window). "
            "Either the full-window field was overwritten or the test fixture is wrong."
        )

    def test_simulate_market_dome_uses_entry_field_for_fv_gate(self, tmp_path):
        """simulate_market_dome must use entry_up_best_bid/ask for FV-gate computation,
        not the full-window up_best_bid/ask.

        With fv_gate_enabled and early book centered at 0.295 → fv_up_entry ≈ 0.295,
        cert_entry ≈ _fv_certainty(0.295).  If full-window median ≈ 0.51 is used
        instead, cert_entry → _fv_certainty(0.51) ≈ 0.02, far from the early value.
        """
        from tools.backtester import simulate_market_dome, BacktestConfig, _fv_certainty

        path = _make_synthetic_dome_file(tmp_path)
        dome = load_dome_snapshot(path)
        assert dome is not None

        cfg = BacktestConfig(fv_gate_enabled=True, fv_gate_certainty_threshold=0.50)
        result = simulate_market_dome(dome, cfg)

        # With entry book at 0.28/0.31, UP mid ≈ 0.295, cert ≈ _fv_certainty(0.295)
        # That is a reasonably high certainty for DN direction
        entry_mid_correct = (0.28 + 0.31) / 2.0  # 0.295
        expected_cert = _fv_certainty(entry_mid_correct)

        assert result.certainty_at_entry == pytest.approx(expected_cert, abs=0.02), (
            f"certainty_at_entry={result.certainty_at_entry:.4f} does not match "
            f"expected cert from early-window book mid={entry_mid_correct:.3f} "
            f"(expected≈{expected_cert:.4f}).  FV gate may be using full-window median."
        )

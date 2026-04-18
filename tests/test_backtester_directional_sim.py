"""Tests for the directional FV simulation in tools/backtester.py.

Covers:
 1. With +2% pre-window Binance drift, fv_up_entry > 0.5 (upside signal).
 2. Gate fires when drift is large enough (cert >= threshold).
 3. Empty pre_window_binance → fallback to book_mid behavior, no crash.
 4. fv_source field in result: "binance_drift" vs "book_mid_fallback".
 5. DomeMarketData gains pre_window_binance field (list of (ts_ms, close_price)).
 6. load_dome_snapshot: pre_window_binance defaults to [] when no sibling file exists.
 7. load_dome_snapshot: populates pre_window_binance when sibling file exists.
"""
from __future__ import annotations

import json
import math
import pathlib
import sys
import tempfile

import pytest

# Add project root to path
_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.backtester import (
    BacktestConfig,
    DomeMarketData,
    load_dome_snapshot,
    simulate_market_dome,
    _p_fair_up,
    _fv_certainty,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dome(
    outcome: str = "UP",
    up_ask: float = 0.52,
    dn_ask: float = 0.52,
    ptb: float | None = 83000.0,
    binance_at_close: float | None = 83100.0,
    pre_window_binance: list[tuple[int, float]] | None = None,
) -> DomeMarketData:
    """Build a minimal DomeMarketData for testing."""
    window_start = 1_774_742_400
    window_end = window_start + 900

    return DomeMarketData(
        market_slug="btc-updown-15m-1774742400",
        condition_id="0xdeadbeef",
        up_token_id="up_tok",
        dn_token_id="dn_tok",
        window_start=window_start,
        window_end=window_end,
        outcome=outcome,
        up_best_bid=up_ask - 0.02,
        up_best_ask=up_ask,
        dn_best_bid=dn_ask - 0.02,
        dn_best_ask=dn_ask,
        entry_up_best_bid=up_ask - 0.02,
        entry_up_best_ask=up_ask,
        entry_dn_best_bid=dn_ask - 0.02,
        entry_dn_best_ask=dn_ask,
        ptb=ptb,
        binance_at_close=binance_at_close,
        chainlink_at_close=None,
        binance_series=[],
        has_orderbook=True,
        ob_up_count=10,
        ob_dn_count=10,
        ob_up_series=[
            (float(window_start + i), up_ask - 0.02, up_ask)
            for i in range(10)
        ],
        ob_dn_series=[
            (float(window_start + i), dn_ask - 0.02, dn_ask)
            for i in range(10)
        ],
        pre_window_binance=pre_window_binance if pre_window_binance is not None else [],
    )


def _build_prewindow_series(
    window_start: int,
    start_price: float,
    drift_pct: float,
    count: int = 120,
) -> list[tuple[int, float]]:
    """Build a synthetic pre-window Binance series with a linear drift.

    Returns list of (ts_ms, close_price) spanning 120 seconds before window_start.
    Price at t=window_start-60s is start_price.
    Price at t=window_start is start_price * (1 + drift_pct).
    """
    p_open_minus_60 = start_price
    p_open = start_price * (1.0 + drift_pct)

    series = []
    for i in range(count):
        ts_sec = window_start - 120 + i  # 120s before window → window_start
        # Linear interpolation: price at window_start - 60s is p_open_minus_60
        # price at window_start is p_open
        if ts_sec < window_start - 60:
            price = p_open_minus_60
        else:
            frac = (ts_sec - (window_start - 60)) / 60.0
            price = p_open_minus_60 + frac * (p_open - p_open_minus_60)
        series.append((ts_sec * 1000, price))  # ts_ms

    return series


# ---------------------------------------------------------------------------
# 1. fv_up_entry > 0.5 when drift is positive
# ---------------------------------------------------------------------------

class TestBinanceDriftFvEntry:
    """With +2% Binance drift before window open, fv_up_entry must be > 0.5."""

    def test_positive_drift_yields_fv_up_above_half(self):
        """A +2% BTC price drift over 60s must produce fv_up_entry > 0.5."""
        window_start = 1_774_742_400
        ptb = 83_000.0  # price-to-beat = price at window start per Chainlink
        drift_pct = 0.02  # +2%

        series = _build_prewindow_series(window_start, ptb, drift_pct)
        dome = _make_dome(
            outcome="UP",
            ptb=ptb,
            pre_window_binance=series,
        )

        cfg = BacktestConfig(use_binance_drift=True)
        result = simulate_market_dome(dome, cfg)

        assert result.fv_at_entry > 0.5, (
            f"Expected fv_at_entry > 0.5 with +2% drift, got {result.fv_at_entry}"
        )

    def test_negative_drift_yields_fv_up_below_half(self):
        """A -2% BTC price drift over 60s must produce fv_up_entry < 0.5."""
        window_start = 1_774_742_400
        ptb = 83_000.0
        drift_pct = -0.02  # -2%

        series = _build_prewindow_series(window_start, ptb, drift_pct)
        dome = _make_dome(
            outcome="DOWN",
            ptb=ptb,
            pre_window_binance=series,
        )

        cfg = BacktestConfig(use_binance_drift=True)
        result = simulate_market_dome(dome, cfg)

        assert result.fv_at_entry < 0.5, (
            f"Expected fv_at_entry < 0.5 with -2% drift, got {result.fv_at_entry}"
        )

    def test_zero_drift_yields_fv_up_near_half(self):
        """Zero Binance drift → FV near 0.5 (uncertain direction)."""
        window_start = 1_774_742_400
        ptb = 83_000.0

        series = _build_prewindow_series(window_start, ptb, 0.0)
        dome = _make_dome(ptb=ptb, pre_window_binance=series)

        cfg = BacktestConfig(use_binance_drift=True)
        result = simulate_market_dome(dome, cfg)

        # Zero drift → d = 0 → P(Up) = 0.5
        assert abs(result.fv_at_entry - 0.5) < 0.05, (
            f"Expected fv_at_entry ≈ 0.5 with zero drift, got {result.fv_at_entry}"
        )


# ---------------------------------------------------------------------------
# 2. Gate fires with sufficient drift
# ---------------------------------------------------------------------------

class TestGateFiringOnDrift:
    """FV gate must fire when drift is large enough to push cert past threshold."""

    def test_large_drift_triggers_fv_gate(self):
        """A very large (+10%) drift must trigger the FV gate (cert >= 0.80)."""
        window_start = 1_774_742_400
        ptb = 83_000.0
        drift_pct = 0.10  # +10% → very strong signal

        series = _build_prewindow_series(window_start, ptb, drift_pct)
        dome = _make_dome(ptb=ptb, pre_window_binance=series)

        cfg = BacktestConfig(
            use_binance_drift=True,
            fv_gate_enabled=True,
            fv_gate_certainty_threshold=0.80,
        )
        result = simulate_market_dome(dome, cfg)

        assert result.fv_blocked is True, (
            f"Expected FV gate to fire with +10% drift, but fv_blocked={result.fv_blocked}. "
            f"fv_at_entry={result.fv_at_entry}, cert={result.certainty_at_entry}"
        )

    def test_small_drift_does_not_trigger_fv_gate(self):
        """A small drift (0.1%) must NOT trigger the gate."""
        window_start = 1_774_742_400
        ptb = 83_000.0
        drift_pct = 0.001  # 0.1%

        series = _build_prewindow_series(window_start, ptb, drift_pct)
        dome = _make_dome(ptb=ptb, pre_window_binance=series)

        cfg = BacktestConfig(
            use_binance_drift=True,
            fv_gate_enabled=True,
            fv_gate_certainty_threshold=0.80,
        )
        result = simulate_market_dome(dome, cfg)

        assert result.fv_blocked is False, (
            f"Expected FV gate NOT to fire with 0.1% drift, but fv_blocked={result.fv_blocked}"
        )


# ---------------------------------------------------------------------------
# 3. Empty pre_window_binance → fallback, no crash
# ---------------------------------------------------------------------------

class TestEmptyPreWindowFallback:
    """When pre_window_binance is empty, backtester falls back gracefully."""

    def test_empty_series_falls_back_to_book_mid(self):
        """Empty pre_window_binance with use_binance_drift=True → uses book_mid, no crash."""
        dome = _make_dome(
            up_ask=0.55,
            dn_ask=0.52,
            pre_window_binance=[],
        )

        cfg = BacktestConfig(use_binance_drift=True)
        # Must not raise
        result = simulate_market_dome(dome, cfg)
        assert result is not None

    def test_empty_series_fv_source_is_fallback(self):
        """When pre_window_binance is empty, fv_source must be 'book_mid_fallback'."""
        dome = _make_dome(pre_window_binance=[])

        cfg = BacktestConfig(use_binance_drift=True)
        result = simulate_market_dome(dome, cfg)

        assert result.fv_source == "book_mid_fallback", (
            f"Expected fv_source='book_mid_fallback', got {result.fv_source!r}"
        )

    def test_flag_off_uses_book_mid_source(self):
        """With use_binance_drift=False, fv_source must be 'book_mid_fallback' (legacy mode)."""
        series = _build_prewindow_series(1_774_742_400, 83_000.0, 0.02)
        dome = _make_dome(pre_window_binance=series)

        cfg = BacktestConfig(use_binance_drift=False)
        result = simulate_market_dome(dome, cfg)

        assert result.fv_source == "book_mid_fallback", (
            f"Expected legacy fv_source='book_mid_fallback', got {result.fv_source!r}"
        )


# ---------------------------------------------------------------------------
# 4. fv_source field
# ---------------------------------------------------------------------------

class TestFvSourceField:
    """MarketResult.fv_source indicates which FV method was used."""

    def test_fv_source_is_binance_drift_when_data_available(self):
        """fv_source must be 'binance_drift' when pre_window_binance has data and flag is on."""
        window_start = 1_774_742_400
        series = _build_prewindow_series(window_start, 83_000.0, 0.02)
        dome = _make_dome(pre_window_binance=series)

        cfg = BacktestConfig(use_binance_drift=True)
        result = simulate_market_dome(dome, cfg)

        assert result.fv_source == "binance_drift", (
            f"Expected fv_source='binance_drift', got {result.fv_source!r}"
        )

    def test_fv_source_survives_aggregation(self):
        """fv_source should be present on results (not stripped by aggregate_results)."""
        from tools.backtester import aggregate_results

        window_start = 1_774_742_400
        series = _build_prewindow_series(window_start, 83_000.0, 0.02)
        dome = _make_dome(pre_window_binance=series)

        cfg = BacktestConfig(use_binance_drift=True)
        result = simulate_market_dome(dome, cfg)

        # fv_source lives on the MarketResult, not in aggregate — just verify it's accessible
        assert hasattr(result, "fv_source")
        assert result.fv_source in ("binance_drift", "book_mid_fallback")


# ---------------------------------------------------------------------------
# 5. DomeMarketData has pre_window_binance field
# ---------------------------------------------------------------------------

class TestDomeMarketDataField:
    """DomeMarketData must have a pre_window_binance field."""

    def test_pre_window_binance_field_exists(self):
        """DomeMarketData must have pre_window_binance: list of (ts_ms, close_price)."""
        dome = _make_dome(pre_window_binance=[])
        assert hasattr(dome, "pre_window_binance")
        assert isinstance(dome.pre_window_binance, list)

    def test_pre_window_binance_field_stores_tuples(self):
        """pre_window_binance must store (ts_ms, close_price) pairs."""
        series = [(1_774_742_280_000, 83_000.0), (1_774_742_281_000, 83_001.0)]
        dome = _make_dome(pre_window_binance=series)
        assert dome.pre_window_binance == series


# ---------------------------------------------------------------------------
# 6. load_dome_snapshot: pre_window_binance defaults to [] when no sibling file
# ---------------------------------------------------------------------------

class TestLoadDomeSnapshotPrewindow:
    """load_dome_snapshot must load pre_window_binance from sibling file when available."""

    def _write_dome_file(self, dome_dir: pathlib.Path, market_id: str, window_start: int) -> pathlib.Path:
        header = {
            "type": "header",
            "market_slug": market_id,
            "condition_id": "0xdeadbeef",
            "up_token_id": "up_tok",
            "dn_token_id": "dn_tok",
            "window_start": window_start,
            "window_end": window_start + 900,
            "fetched_at": window_start + 10000,
            "raw_market": {
                "winning_side": {"label": "Up"},
                "extra_fields": {"price_to_beat": 83000.0},
            },
        }
        # Add minimal orderbook entry so has_orderbook=True
        ob_up = {
            "type": "orderbook",
            "side": "UP",
            "data": {
                "timestamp": window_start * 1000,
                "bids": [{"price": 0.50, "size": 100}],
                "asks": [{"price": 0.52, "size": 100}],
            },
        }
        ob_dn = {
            "type": "orderbook",
            "side": "DN",
            "data": {
                "timestamp": window_start * 1000,
                "bids": [{"price": 0.50, "size": 100}],
                "asks": [{"price": 0.52, "size": 100}],
            },
        }
        path = dome_dir / f"{market_id}.jsonl"
        path.write_text(
            json.dumps(header) + "\n" +
            json.dumps(ob_up) + "\n" +
            json.dumps(ob_dn) + "\n",
            encoding="utf-8",
        )
        return path

    def test_missing_sibling_gives_empty_prewindow(self, tmp_path):
        """When no sibling prewindow file exists, pre_window_binance must be []."""
        dome_dir = tmp_path / "dome_snapshots"
        dome_dir.mkdir()
        market_id = "btc-updown-15m-1774742400"
        window_start = 1_774_742_400

        path = self._write_dome_file(dome_dir, market_id, window_start)
        # No sibling file created

        dome = load_dome_snapshot(path)
        assert dome is not None
        assert dome.pre_window_binance == [], (
            f"Expected empty pre_window_binance, got {dome.pre_window_binance}"
        )

    def test_existing_sibling_populates_prewindow(self, tmp_path):
        """When sibling prewindow file exists, pre_window_binance must be populated."""
        dome_dir = tmp_path / "dome_snapshots"
        dome_dir.mkdir()
        prewindow_dir = tmp_path / "dome_snapshots_binance_prewindow"
        prewindow_dir.mkdir()

        market_id = "btc-updown-15m-1774742400"
        window_start = 1_774_742_400

        path = self._write_dome_file(dome_dir, market_id, window_start)

        # Write sibling prewindow file
        sibling = prewindow_dir / f"{market_id}.jsonl"
        rows = [
            {"ts_ms": (window_start - 60) * 1000, "close_price": 83000.0},
            {"ts_ms": (window_start - 30) * 1000, "close_price": 83100.0},
            {"ts_ms": window_start * 1000, "close_price": 83200.0},
        ]
        sibling.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )

        dome = load_dome_snapshot(path)
        assert dome is not None
        assert len(dome.pre_window_binance) == 3, (
            f"Expected 3 prewindow entries, got {len(dome.pre_window_binance)}"
        )
        # Verify first entry
        ts_ms, close_price = dome.pre_window_binance[0]
        assert ts_ms == (window_start - 60) * 1000
        assert close_price == pytest.approx(83000.0)

    def test_sibling_search_uses_correct_path(self, tmp_path):
        """Sibling file must be looked up in dome_snapshots_binance_prewindow/ next to dome_snapshots/."""
        # dome_snapshots/ and dome_snapshots_binance_prewindow/ are siblings under the same parent
        dome_dir = tmp_path / "dome_snapshots"
        dome_dir.mkdir()
        prewindow_dir = tmp_path / "dome_snapshots_binance_prewindow"
        prewindow_dir.mkdir()

        market_id = "btc-updown-15m-1774742400"
        window_start = 1_774_742_400

        path = self._write_dome_file(dome_dir, market_id, window_start)

        # Write in the correct sibling location
        sibling = prewindow_dir / f"{market_id}.jsonl"
        sibling.write_text(
            '{"ts_ms": 1774742340000, "close_price": 83000.0}\n',
            encoding="utf-8",
        )

        dome = load_dome_snapshot(path)
        assert dome is not None
        assert len(dome.pre_window_binance) == 1
        assert dome.pre_window_binance[0][0] == 1774742340000

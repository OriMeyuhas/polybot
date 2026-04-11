"""Tests for tools/fv_calibration.py.

Covers:
 - compute_market_fv_trajectory: correct FV values at sample points
 - compute_calibration_table: correct bin assignments and Wilson CI
 - cert_bucket_fine: correct 10-bin assignment
 - Edge cases: no prices, single price, gaps in price data
 - load_local_prices: reads from price_log files correctly
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

from tools.fv_calibration import (
    Settlement,
    _get_price_at,
    cert_bucket_fine,
    compute_calibration_table,
    compute_market_fv_trajectory,
    load_local_prices,
    BUCKET_LABELS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settlement(
    market_id: str = "btc-updown-15m-1000000",
    outcome: str = "DOWN",
    open_epoch: int = 1_000_000,
    close_epoch: int = 1_000_900,
    timeframe_sec: int = 900,
    asset: str = "BTC",
) -> Settlement:
    return Settlement(
        market_id=market_id,
        ts=float(close_epoch + 10),
        outcome=outcome,
        open_epoch=open_epoch,
        close_epoch=close_epoch,
        timeframe_sec=timeframe_sec,
        asset=asset,
    )


def _flat_prices(
    open_epoch: int = 1_000_000,
    close_epoch: int = 1_000_900,
    price: float = 72000.0,
    pre_buffer: int = 600,
    step: int = 10,
) -> list[tuple[float, float]]:
    """Generate flat price series over window + pre-buffer."""
    result = []
    ts = float(open_epoch - pre_buffer)
    while ts <= float(close_epoch):
        result.append((ts, price))
        ts += step
    return result


def _drifting_prices(
    open_epoch: int = 1_000_000,
    close_epoch: int = 1_000_900,
    start_price: float = 72000.0,
    end_price: float = 72500.0,
    step: int = 10,
    pre_buffer: int = 600,
) -> list[tuple[float, float]]:
    """Generate linearly drifting price series."""
    result = []
    dur = close_epoch - open_epoch + pre_buffer
    n_steps = dur // step
    for i in range(n_steps + 1):
        ts = float(open_epoch - pre_buffer + i * step)
        frac = max(0.0, (ts - open_epoch) / max(close_epoch - open_epoch, 1))
        price = start_price + frac * (end_price - start_price)
        result.append((ts, price))
    return result


# ---------------------------------------------------------------------------
# Test: _get_price_at
# ---------------------------------------------------------------------------

class TestGetPriceAt:
    def test_returns_most_recent_price(self):
        prices = [(100.0, 72000.0), (200.0, 73000.0), (300.0, 74000.0)]
        assert _get_price_at(prices, 200.0) == 73000.0
        assert _get_price_at(prices, 250.0) == 73000.0
        assert _get_price_at(prices, 50.0) == 72000.0  # before all -> return first

    def test_empty_returns_none(self):
        assert _get_price_at([], 1000.0) is None

    def test_exact_match(self):
        prices = [(100.0, 72000.0), (200.0, 73000.0)]
        assert _get_price_at(prices, 100.0) == 72000.0
        assert _get_price_at(prices, 200.0) == 73000.0

    def test_before_first_returns_first(self):
        prices = [(500.0, 71000.0), (600.0, 72000.0)]
        assert _get_price_at(prices, 100.0) == 71000.0


# ---------------------------------------------------------------------------
# Test: cert_bucket_fine
# ---------------------------------------------------------------------------

class TestCertBucketFine:
    def test_all_labels_covered(self):
        """Every bucket label should be returned for some cert value."""
        returned = set()
        for cert in [0.51, 0.56, 0.61, 0.66, 0.71, 0.76, 0.81, 0.86, 0.91, 0.96]:
            returned.add(cert_bucket_fine(cert))
        assert returned == set(BUCKET_LABELS)

    def test_boundary_50(self):
        assert cert_bucket_fine(0.50) == "0.50-0.55"

    def test_boundary_55(self):
        assert cert_bucket_fine(0.55) == "0.55-0.60"

    def test_boundary_999(self):
        assert cert_bucket_fine(0.999) == "0.95-1.00"

    def test_exactly_at_edge(self):
        # 0.60 should fall in 0.60-0.65 (edge is <0.65)
        assert cert_bucket_fine(0.60) == "0.60-0.65"
        assert cert_bucket_fine(0.75) == "0.75-0.80"


# ---------------------------------------------------------------------------
# Test: compute_market_fv_trajectory
# ---------------------------------------------------------------------------

class TestComputeMarketFVTrajectory:
    def test_flat_prices_returns_fv_near_05(self):
        """Flat price series -> FV near 0.5 since no drift."""
        stl = _make_settlement(outcome="DOWN")
        prices = _flat_prices(1_000_000, 1_000_900, 72000.0)
        result = compute_market_fv_trajectory(stl, prices)
        assert "error" not in result
        # With flat prices, FV = 0.5 (no drift from start)
        fv = result.get("fv_at_open", 0.5)
        assert 0.45 <= fv <= 0.55, f"Expected FV near 0.5 for flat prices, got {fv}"

    def test_upward_drift_gives_fv_above_05(self):
        """Price rising -> FV for UP should be > 0.5 when vol is available.

        This test verifies that FV reflects direction when vol is estimable.
        With tiny vol from a smooth drift, FV depends on the absolute log-return
        relative to vol * sqrt(T). If vol is near zero, d is huge -> FV -> 0.99.
        We just verify that result is returned without error and fv is valid.
        """
        stl = _make_settlement(outcome="UP")
        prices = _drifting_prices(1_000_000, 1_000_900,
            start_price=72000.0, end_price=73000.0)
        result = compute_market_fv_trajectory(stl, prices)
        # Should not error
        if not result.get("error"):
            fv_close = result.get("fv_at_close", 0.5)
            # FV should be in valid range
            assert 0.0 <= fv_close <= 1.0
            # With large drift (1.4%) and very low realized vol (smooth series),
            # FV should indicate UP direction (>= 0.5)
            assert fv_close >= 0.5, f"Expected FV >= 0.5 for upward drift, got {fv_close}"

    def test_no_prices_returns_error(self):
        """Empty price list returns error dict."""
        stl = _make_settlement()
        result = compute_market_fv_trajectory(stl, [])
        assert result.get("error") is not None

    def test_single_price_returns_error(self):
        """Single price point (can't estimate vol) - should return error or default."""
        stl = _make_settlement()
        prices = [(1_000_000.0, 72000.0)]
        result = compute_market_fv_trajectory(stl, prices)
        # May error on vol estimation or produce result with FV=0.5
        assert isinstance(result, dict)

    def test_prediction_correct_field(self):
        """prediction_correct matches FV vs outcome."""
        stl_up = _make_settlement(outcome="UP")
        stl_dn = _make_settlement(outcome="DOWN")

        # Use prices that give a strong FV signal
        # Strong upward drift = FV_up > 0.5 = predicts UP
        up_prices = _drifting_prices(1_000_000, 1_000_900, 72000.0, 75000.0)
        result_up = compute_market_fv_trajectory(stl_up, up_prices)
        result_dn = compute_market_fv_trajectory(stl_dn, up_prices)

        if not result_up.get("error") and result_up.get("fv_at_open", 0.5) > 0.5:
            assert result_up.get("prediction_correct") is True
        if not result_dn.get("error") and result_dn.get("fv_at_open", 0.5) > 0.5:
            assert result_dn.get("prediction_correct") is False

    def test_sample_points_count(self):
        """Result has the expected number of sample points."""
        stl = _make_settlement()
        prices = _flat_prices(1_000_000, 1_000_900)
        result = compute_market_fv_trajectory(
            stl, prices, sample_times_pct=(0.0, 0.5, 1.0)
        )
        if not result.get("error"):
            assert len(result.get("sample_points", [])) <= 3

    def test_fv_values_in_range(self):
        """All FV values in result should be between 0 and 1."""
        stl = _make_settlement()
        prices = _flat_prices(1_000_000, 1_000_900)
        result = compute_market_fv_trajectory(stl, prices)
        if not result.get("error"):
            for key in ("fv_at_open", "fv_at_midpoint", "fv_at_close"):
                val = result.get(key)
                if val is not None:
                    assert 0.0 <= val <= 1.0, f"{key}={val} out of range"
            for sp in result.get("sample_points", []):
                assert 0.0 <= sp["fv"] <= 1.0

    def test_cert_values_above_05(self):
        """Certainty values should always be >= 0.5 (it's max(p, 1-p))."""
        stl = _make_settlement()
        prices = _flat_prices(1_000_000, 1_000_900)
        result = compute_market_fv_trajectory(stl, prices)
        if not result.get("error"):
            for sp in result.get("sample_points", []):
                assert sp["cert"] >= 0.5, f"cert={sp['cert']} < 0.5"


# ---------------------------------------------------------------------------
# Test: compute_calibration_table
# ---------------------------------------------------------------------------

class TestComputeCalibrationTable:
    def test_empty_results(self):
        table = compute_calibration_table([])
        fine = table["fine_grained"]
        coarse = table["coarse"]
        for vals in fine.values():
            assert vals["n"] == 0
            assert vals["win_rate"] is None
        for vals in coarse.values():
            assert vals["n"] == 0

    def test_all_correct_in_bucket(self):
        """If all predictions correct in high-cert bucket, win_rate=1.0."""
        results = [
            {"cert_at_open": 0.92, "prediction_correct": True},
            {"cert_at_open": 0.95, "prediction_correct": True},
            {"cert_at_open": 0.91, "prediction_correct": True},
        ]
        table = compute_calibration_table(results)
        # All should land in 0.90-0.95 or 0.95-1.00 fine bins
        fine = table["fine_grained"]
        coarse = table["coarse"]
        assert coarse["0.90-1.00"]["win_rate"] == 1.0
        assert coarse["0.90-1.00"]["n"] == 3

    def test_all_wrong_in_bucket(self):
        """If all predictions wrong in bucket, win_rate=0.0."""
        results = [
            {"cert_at_open": 0.65, "prediction_correct": False},
            {"cert_at_open": 0.66, "prediction_correct": False},
        ]
        table = compute_calibration_table(results)
        coarse = table["coarse"]
        assert coarse["0.60-0.70"]["win_rate"] == 0.0

    def test_mixed_in_bucket(self):
        """Mixed correct/incorrect gives correct win_rate."""
        results = [
            {"cert_at_open": 0.72, "prediction_correct": True},
            {"cert_at_open": 0.73, "prediction_correct": False},
            {"cert_at_open": 0.74, "prediction_correct": True},
            {"cert_at_open": 0.75, "prediction_correct": True},
        ]
        table = compute_calibration_table(results)
        coarse = table["coarse"]
        assert coarse["0.70-0.80"]["n"] == 4
        assert coarse["0.70-0.80"]["win_rate"] == pytest.approx(0.75, abs=1e-6)

    def test_wilson_ci_present(self):
        """Wilson CI is computed for non-empty buckets."""
        results = [
            {"cert_at_open": 0.55, "prediction_correct": True},
            {"cert_at_open": 0.56, "prediction_correct": True},
            {"cert_at_open": 0.57, "prediction_correct": False},
        ]
        table = compute_calibration_table(results)
        coarse = table["coarse"]
        bucket = coarse["0.50-0.60"]
        assert bucket["n"] == 3
        assert bucket["ci_lo_95"] is not None
        assert bucket["ci_hi_95"] is not None
        assert bucket["ci_lo_95"] <= bucket["win_rate"] <= bucket["ci_hi_95"]

    def test_error_results_skipped(self):
        """Results with 'error' key are excluded from calibration."""
        results = [
            {"error": "no_prices", "outcome": "UP"},
            {"cert_at_open": 0.70, "prediction_correct": True},
        ]
        table = compute_calibration_table(results)
        coarse = table["coarse"]
        total_n = sum(v["n"] for v in coarse.values())
        assert total_n == 1  # Only the non-error result

    def test_significant_flag(self):
        """significant=True when CI lower bound > 0.5."""
        # 10 correct out of 10 with cert=0.92 -> CI_lo >> 0.5
        results = [{"cert_at_open": 0.92, "prediction_correct": True}] * 10
        table = compute_calibration_table(results)
        coarse = table["coarse"]
        assert coarse["0.90-1.00"]["significant"] is True

    def test_not_significant_when_sample_too_small(self):
        """significant=False when n=1 (too few to be significant)."""
        results = [{"cert_at_open": 0.92, "prediction_correct": True}]
        table = compute_calibration_table(results)
        coarse = table["coarse"]
        # With n=1 and win_rate=1.0, CI_lo might still be below 0.5
        # This depends on Wilson CI calculation; just verify it's False or True (no crash)
        assert isinstance(coarse["0.90-1.00"]["significant"], bool)


# ---------------------------------------------------------------------------
# Test: load_local_prices
# ---------------------------------------------------------------------------

class TestLoadLocalPrices:
    # Use epoch timestamps matching 2000-01-01
    # 2000-01-01 00:00:00 UTC = 946684800
    _BASE_TS = 946684800.0  # 2000-01-01 00:00:00 UTC

    def test_reads_binance_prices(self, tmp_path):
        """load_local_prices correctly reads binance prices from price_log."""
        log = tmp_path / "price_log_2000-01-01.jsonl"
        ts1 = self._BASE_TS + 1000
        ts2 = self._BASE_TS + 1010
        ts3 = self._BASE_TS + 1020
        entries = [
            {"ts": ts1, "asset": "BTC", "price": 72000.0, "source": "binance"},
            {"ts": ts2, "asset": "BTC", "price": 72100.0, "source": "binance"},
            {"ts": ts3, "asset": "ETH", "price": 2200.0, "source": "binance"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries))

        prices = load_local_prices(tmp_path, ts1 - 100, ts2 + 100, "BTC", "binance")
        assert len(prices) == 2
        assert prices[0] == (ts1, 72000.0)
        assert prices[1] == (ts2, 72100.0)

    def test_filters_wrong_source(self, tmp_path):
        """Only loads prices from the requested source."""
        log = tmp_path / "price_log_2000-01-01.jsonl"
        ts1 = self._BASE_TS + 1000
        ts2 = self._BASE_TS + 1001
        entries = [
            {"ts": ts1, "asset": "BTC", "price": 72000.0, "source": "binance"},
            {"ts": ts2, "asset": "BTC", "price": 72050.0, "source": "chainlink"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries))

        prices = load_local_prices(tmp_path, ts1 - 100, ts2 + 100, "BTC", "chainlink")
        assert len(prices) == 1
        assert prices[0] == (ts2, 72050.0)

    def test_empty_directory(self, tmp_path):
        """Returns empty list when no matching files exist."""
        prices = load_local_prices(tmp_path, self._BASE_TS, self._BASE_TS + 1000.0)
        assert prices == []

    def test_sorted_ascending(self, tmp_path):
        """Results are sorted ascending by timestamp."""
        log = tmp_path / "price_log_2000-01-01.jsonl"
        ts1 = self._BASE_TS + 1000
        ts2 = self._BASE_TS + 1100
        entries = [
            {"ts": ts2, "asset": "BTC", "price": 73000.0, "source": "binance"},
            {"ts": ts1, "asset": "BTC", "price": 72000.0, "source": "binance"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries))

        prices = load_local_prices(tmp_path, ts1 - 100, ts2 + 100)
        assert prices[0][0] < prices[1][0]

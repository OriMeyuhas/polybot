"""Tests for tools/backtester.py (local book_log version).

Covers:
 - BookState / lookup_book_state: correct best_bid/best_ask from price_change events
 - BacktestConfig loading from YAML
 - simulate_market: paired fills when both sides have resting orders matching book state
 - fv_gate_enabled flag changes behavior
 - MarketWindow with/without token mapping
 - aggregate_results metrics
"""
from __future__ import annotations

import json
import math
import pathlib
import sys
import tempfile

import pytest
import yaml

# Add project root to path
_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.backtester import (
    BacktestConfig,
    BookState,
    MarketResult,
    MarketWindow,
    _cert_bucket,
    aggregate_results,
    build_book_index,
    lookup_book_state,
    simulate_market,
    _p_fair_up,
    _fv_certainty,
)


# ---------------------------------------------------------------------------
# Helpers for building synthetic test data
# ---------------------------------------------------------------------------

def _make_window(
    market_id: str = "btc-updown-15m-1000000",
    open_epoch: int = 1_000_000,
    close_epoch: int = 1_000_900,
    outcome: str | None = "DOWN",
    up_token_id: str | None = "up_token_short_123",
    dn_token_id: str | None = "dn_token_short_456",
    pnl_actual: float = 0.0,
) -> MarketWindow:
    return MarketWindow(
        market_id=market_id,
        open_epoch=open_epoch,
        close_epoch=close_epoch,
        outcome=outcome,
        up_token_id=up_token_id,
        dn_token_id=dn_token_id,
        pnl_actual=pnl_actual,
    )


def _make_book_index(
    up_token: str,
    dn_token: str,
    up_ask: float = 0.50,
    dn_ask: float = 0.50,
    open_epoch: int = 1_000_000,
    close_epoch: int = 1_000_900,
    step: int = 30,
) -> dict[str, list[tuple[float, float, float]]]:
    """Build a synthetic book index with constant best_bid/best_ask throughout the window."""
    index: dict[str, list] = {}
    for tok, ask in [(up_token, up_ask), (dn_token, dn_ask)]:
        bid = max(0.01, ask - 0.01)
        entries = []
        ts = float(open_epoch - 10)
        while ts <= close_epoch + 10:
            entries.append((ts, bid, ask))
            ts += step
        index[tok] = entries
    return index


def _make_price_series(
    open_epoch: int = 1_000_000,
    close_epoch: int = 1_000_900,
    start_price: float = 72000.0,
    end_price: float | None = None,
    step: int = 30,
) -> list[tuple[float, float]]:
    """Build a synthetic Binance price series."""
    if end_price is None:
        end_price = start_price
    prices = []
    n = max(1, (close_epoch - open_epoch) // step)
    for i in range(n + 1):
        ts = open_epoch - 600 + i * step
        # Linear interpolation from start to end
        frac = min(1.0, (ts - (open_epoch - 600)) / max(close_epoch - (open_epoch - 600), 1))
        price = start_price + frac * (end_price - start_price)
        prices.append((float(ts), price))
    return prices


def _make_result(
    market_id: str = "test",
    outcome: str | None = "UP",
    pnl: float = 1.0,
    paired: bool = True,
    up_qty: float = 10.0,
    dn_qty: float = 10.0,
    cert: float = 0.65,
    outcome_correct: bool | None = True,
    has_book_data: bool = True,
) -> MarketResult:
    return MarketResult(
        market_id=market_id,
        outcome=outcome,
        outcome_correct=outcome_correct,
        fills=[],
        events=[],
        pnl=pnl,
        paired=paired,
        up_cost=up_qty * 0.45,
        dn_cost=dn_qty * 0.45,
        pair_cost=0.90,
        up_qty=up_qty,
        dn_qty=dn_qty,
        fv_at_entry=cert if outcome == "UP" else 1.0 - cert,
        certainty_at_entry=cert,
        aborted=False,
        fv_blocked=False,
        cert_bucket=_cert_bucket(cert),
        market_hour=12,
        has_book_data=has_book_data,
    )


# ---------------------------------------------------------------------------
# Test: BacktestConfig
# ---------------------------------------------------------------------------

class TestBacktestConfig:
    def test_from_dict_defaults(self):
        cfg = BacktestConfig.from_dict({})
        assert cfg.rungs == 10
        assert cfg.spacing == 0.01
        assert cfg.width == 0.10
        assert cfg.fv_gate_enabled is False

    def test_from_dict_override(self):
        cfg = BacktestConfig.from_dict({"fv_gate_enabled": True, "rungs": 5})
        assert cfg.fv_gate_enabled is True
        assert cfg.rungs == 5
        assert cfg.spacing == 0.01

    def test_from_dict_ignores_unknown_keys(self):
        cfg = BacktestConfig.from_dict({"unknown_key": 99, "rungs": 7})
        assert cfg.rungs == 7

    def test_from_yaml(self, tmp_path):
        data = {"fv_gate_enabled": True, "fv_gate_certainty_threshold": 0.95, "rungs": 8}
        p = tmp_path / "test_config.yaml"
        p.write_text(yaml.dump(data))
        cfg = BacktestConfig.from_yaml(p)
        assert cfg.fv_gate_enabled is True
        assert cfg.fv_gate_certainty_threshold == 0.95
        assert cfg.rungs == 8
        assert cfg.name == "test_config"

    def test_from_yaml_empty_file(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cfg = BacktestConfig.from_yaml(p)
        assert cfg.rungs == 10

    def test_to_dict_round_trip(self):
        cfg = BacktestConfig(rungs=7, fv_gate_enabled=True)
        d = cfg.to_dict()
        cfg2 = BacktestConfig.from_dict(d)
        assert cfg2.rungs == 7
        assert cfg2.fv_gate_enabled is True


# ---------------------------------------------------------------------------
# Test: Book state reconstruction
# ---------------------------------------------------------------------------

class TestBookStateReconstruction:
    def test_initial_state(self):
        """Fresh BookState has default best_bid=0, best_ask=1."""
        bs = BookState("mytoken")
        assert bs.best_bid == 0.0
        assert bs.best_ask == 1.0
        assert bs.last_update_ts == 0.0

    def test_apply_book_event_updates_bid_ask(self):
        """Applying a book event with bids/asks updates best_bid/best_ask."""
        bs = BookState("tok1")
        event = {
            "ts": 1000.5,
            "token_id": "tok1",
            "event_type": "book",
            "data": {
                "bids": [{"price": "0.44", "size": "100"}, {"price": "0.43", "size": "200"}],
                "asks": [{"price": "0.46", "size": "100"}, {"price": "0.47", "size": "200"}],
            },
        }
        bs.apply_book_event(event)
        assert bs.best_bid == pytest.approx(0.44, abs=1e-9)
        assert bs.best_ask == pytest.approx(0.46, abs=1e-9)
        assert bs.last_update_ts == pytest.approx(1000.5)

    def test_apply_price_change_updates_bid_ask(self):
        """Applying a price_change entry updates best_bid/best_ask."""
        bs = BookState("tok1")
        pc = {"asset_id": "tok1full", "best_bid": "0.52", "best_ask": "0.54", "price": "0.01", "size": "10", "side": "BUY"}
        bs.apply_price_change(pc, ts=1001.0)
        assert bs.best_bid == pytest.approx(0.52)
        assert bs.best_ask == pytest.approx(0.54)

    def test_apply_price_change_partial_update(self):
        """If only best_ask is present, best_bid stays unchanged."""
        bs = BookState("tok1")
        bs.best_bid = 0.45
        bs.best_ask = 0.50
        pc = {"asset_id": "tok1full", "best_ask": "0.48"}
        bs.apply_price_change(pc, ts=1002.0)
        assert bs.best_bid == pytest.approx(0.45)
        assert bs.best_ask == pytest.approx(0.48)


# ---------------------------------------------------------------------------
# Test: build_book_index and lookup_book_state
# ---------------------------------------------------------------------------

class TestBookIndex:
    def test_build_index_from_book_events(self, tmp_path):
        """Index correctly extracts best_bid/best_ask from book events."""
        book_log = tmp_path / "book_log_2000-01-01.jsonl"
        events = [
            {"ts": 1000.0, "token_id": "tok1", "event_type": "book",
             "data": {"bids": [{"price": "0.44", "size": "100"}],
                      "asks": [{"price": "0.46", "size": "100"}]}},
            {"ts": 1030.0, "token_id": "tok1", "event_type": "book",
             "data": {"bids": [{"price": "0.42", "size": "100"}],
                      "asks": [{"price": "0.44", "size": "100"}]}},
        ]
        book_log.write_text("\n".join(json.dumps(e) for e in events))

        index = build_book_index(tmp_path, ["2000-01-01"], cache_dir=None)
        assert "tok1" in index
        assert len(index["tok1"]) == 2
        assert index["tok1"][0] == pytest.approx((1000.0, 0.44, 0.46), abs=1e-6)
        assert index["tok1"][1] == pytest.approx((1030.0, 0.42, 0.44), abs=1e-6)

    def test_build_index_from_price_change_events(self, tmp_path):
        """Index correctly extracts best_bid/best_ask from price_change events."""
        book_log = tmp_path / "book_log_2000-01-01.jsonl"
        events = [
            {"ts": 1000.0, "token_id": "", "event_type": "price_change",
             "data": {"price_changes": [
                 {"asset_id": "tok2full", "best_bid": "0.51", "best_ask": "0.53",
                  "price": "0.01", "size": "10", "side": "BUY"},
             ]}},
            {"ts": 1060.0, "token_id": "", "event_type": "price_change",
             "data": {"price_changes": [
                 {"asset_id": "tok2full", "best_bid": "0.52", "best_ask": "0.54",
                  "price": "0.01", "size": "10", "side": "BUY"},
             ]}},
        ]
        book_log.write_text("\n".join(json.dumps(e) for e in events))

        index = build_book_index(tmp_path, ["2000-01-01"], cache_dir=None)
        tok2_short = "tok2full"[:20]
        assert tok2_short in index
        assert len(index[tok2_short]) == 2

    def test_lookup_book_state_binary_search(self):
        """lookup_book_state returns the most recent entry before ts."""
        entries = [
            (1000.0, 0.44, 0.46),
            (1030.0, 0.42, 0.44),
            (1060.0, 0.40, 0.42),
        ]
        index = {"tok1": entries}

        result = lookup_book_state(index, "tok1", 1029.9)
        assert result == pytest.approx((0.44, 0.46), abs=1e-9)

        result = lookup_book_state(index, "tok1", 1060.0)
        assert result == pytest.approx((0.40, 0.42), abs=1e-9)

        result = lookup_book_state(index, "tok1", 999.0)
        assert result == pytest.approx((0.44, 0.46), abs=1e-9)  # first entry

    def test_lookup_missing_token(self):
        """lookup_book_state returns None for unknown token."""
        index: dict = {}
        result = lookup_book_state(index, "unknown", 1000.0)
        assert result is None

    def test_lookup_empty_list(self):
        """lookup_book_state returns None for token with empty list."""
        index = {"tok1": []}
        result = lookup_book_state(index, "tok1", 1000.0)
        assert result is None

    def test_index_from_multiple_dates(self, tmp_path):
        """Index merges data from multiple date files."""
        log1 = tmp_path / "book_log_2000-01-01.jsonl"
        log2 = tmp_path / "book_log_2000-01-02.jsonl"
        log1.write_text(json.dumps({
            "ts": 1000.0, "token_id": "tok1", "event_type": "book",
            "data": {"bids": [{"price": "0.44", "size": "1"}], "asks": [{"price": "0.46", "size": "1"}]}
        }))
        log2.write_text(json.dumps({
            "ts": 86401.0, "token_id": "tok1", "event_type": "book",
            "data": {"bids": [{"price": "0.48", "size": "1"}], "asks": [{"price": "0.50", "size": "1"}]}
        }))
        index = build_book_index(tmp_path, ["2000-01-01", "2000-01-02"], cache_dir=None)
        assert len(index["tok1"]) == 2

    def test_paired_fills_detected_when_ask_low(self):
        """simulate_market detects paired fills when book ask falls to rung level."""
        # Set up a scenario where UP and DN asks are at 0.01 (very cheap)
        # Our ladder at ~0.45 would fill immediately since 0.45 >= 0.01
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        open_epoch = 1_000_000
        close_epoch = 1_000_900

        book_index = _make_book_index(up_tok, dn_tok, up_ask=0.01, dn_ask=0.01,
            open_epoch=open_epoch, close_epoch=close_epoch)
        prices = _make_price_series(open_epoch, close_epoch, 72000.0)
        window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok,
            open_epoch=open_epoch, close_epoch=close_epoch, outcome="DOWN")

        cfg = BacktestConfig(
            fv_cancel_enabled=False,
            one_sided_abort_enabled=False,
            fv_gate_enabled=False,
        )
        result = simulate_market(window, book_index, prices, cfg)

        # With ask=0.01 on both sides, all our resting bids (0.35-0.45) should fill
        assert result.up_qty > 0
        assert result.dn_qty > 0
        assert result.paired

    def test_no_fills_when_ask_too_high(self):
        """No fills occur when book ask is 0.99 (above all our resting bids)."""
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        open_epoch = 1_000_000
        close_epoch = 1_000_900

        book_index = _make_book_index(up_tok, dn_tok, up_ask=0.99, dn_ask=0.99,
            open_epoch=open_epoch, close_epoch=close_epoch)
        prices = _make_price_series(open_epoch, close_epoch, 72000.0)
        window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok,
            open_epoch=open_epoch, close_epoch=close_epoch, outcome="DOWN")

        cfg = BacktestConfig(
            fv_cancel_enabled=False,
            one_sided_abort_enabled=False,
            fv_gate_enabled=False,
        )
        result = simulate_market(window, book_index, prices, cfg)
        assert result.up_qty == 0.0
        assert result.dn_qty == 0.0
        assert not result.paired
        assert result.pnl == 0.0

    def test_paired_pnl_positive_when_pair_cost_below_max(self):
        """Paired position with pair_cost < max_pair_cost produces positive PnL."""
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        open_epoch = 1_000_000
        close_epoch = 1_000_900

        # Asks at 0.01 -> our bids at ~0.40-0.45 fill; pair cost well below 0.98
        book_index = _make_book_index(up_tok, dn_tok, up_ask=0.01, dn_ask=0.01,
            open_epoch=open_epoch, close_epoch=close_epoch)
        prices = _make_price_series(open_epoch, close_epoch, 72000.0)

        for outcome in ("UP", "DOWN"):
            window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok,
                open_epoch=open_epoch, close_epoch=close_epoch, outcome=outcome)
            cfg = BacktestConfig(
                fv_cancel_enabled=False,
                one_sided_abort_enabled=False,
                fv_gate_enabled=False,
                max_pair_cost=0.98,
            )
            result = simulate_market(window, book_index, prices, cfg)
            if result.paired:
                assert result.pnl > 0, f"Expected positive PnL for paired fill (outcome={outcome})"

    def test_no_book_data_returns_no_fills(self):
        """If no token IDs assigned, simulate_market produces no fills."""
        window = _make_window(up_token_id=None, dn_token_id=None, outcome="UP")
        book_index: dict = {}
        prices = _make_price_series(1_000_000, 1_000_900, 72000.0)
        cfg = BacktestConfig(fv_cancel_enabled=False, one_sided_abort_enabled=False)
        result = simulate_market(window, book_index, prices, cfg)
        assert result.up_qty == 0.0
        assert result.dn_qty == 0.0
        assert result.has_book_data is False

    def test_unresolved_market_pnl_zero(self):
        """Markets with outcome=None produce PnL=0."""
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        book_index = _make_book_index(up_tok, dn_tok, up_ask=0.01, dn_ask=0.01)
        prices = _make_price_series()
        window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok, outcome=None)
        cfg = BacktestConfig(fv_cancel_enabled=False, one_sided_abort_enabled=False)
        result = simulate_market(window, book_index, prices, cfg)
        assert result.pnl == 0.0

    def test_fv_gate_enabled_blocks_both_sides(self):
        """FV gate enabled with threshold 0.50 fires on all markets (cert >= 0.50 always)."""
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        book_index = _make_book_index(up_tok, dn_tok, up_ask=0.01, dn_ask=0.01)
        prices = _make_price_series()
        window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok, outcome="UP")

        cfg_off = BacktestConfig(fv_gate_enabled=False, fv_cancel_enabled=False)
        cfg_on = BacktestConfig(
            fv_gate_enabled=True, fv_gate_certainty_threshold=0.50,
            fv_cancel_enabled=False,
        )
        r_off = simulate_market(window, book_index, prices, cfg_off)
        r_on = simulate_market(window, book_index, prices, cfg_on)

        assert not r_off.fv_blocked
        assert r_on.fv_blocked

    def test_fv_gate_off_never_blocks(self):
        """FV gate disabled -> fv_blocked always False."""
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        book_index = _make_book_index(up_tok, dn_tok)
        prices = _make_price_series()
        window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok, outcome="DOWN")
        cfg = BacktestConfig(fv_gate_enabled=False)
        result = simulate_market(window, book_index, prices, cfg)
        assert result.fv_blocked is False

    def test_fv_entry_in_range(self):
        """FV at entry is always in [0, 1]."""
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        book_index = _make_book_index(up_tok, dn_tok)
        prices = _make_price_series()
        window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok, outcome="UP")
        cfg = BacktestConfig()
        result = simulate_market(window, book_index, prices, cfg)
        assert 0.0 <= result.fv_at_entry <= 1.0
        assert 0.5 <= result.certainty_at_entry <= 1.0


# ---------------------------------------------------------------------------
# Test: FV gate changes output
# ---------------------------------------------------------------------------

class TestFVGateToggle:
    def test_fv_gate_on_produces_fv_blocked_flag(self):
        """FV gate on + low threshold -> fv_blocked=True."""
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        book_index = _make_book_index(up_tok, dn_tok)
        prices = _make_price_series()
        window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok)
        cfg = BacktestConfig(fv_gate_enabled=True, fv_gate_certainty_threshold=0.50)
        result = simulate_market(window, book_index, prices, cfg)
        assert result.fv_blocked is True

    def test_fv_gate_results_differ_from_baseline(self):
        """FV gate on vs off produces different fv_blocked flag."""
        up_tok = "up_token_abc123456789"
        dn_tok = "dn_token_xyz987654321"
        book_index = _make_book_index(up_tok, dn_tok, up_ask=0.01, dn_ask=0.01)
        prices = _make_price_series()
        window = _make_window(up_token_id=up_tok, dn_token_id=dn_tok, outcome="UP")
        cfg_off = BacktestConfig(fv_gate_enabled=False, fv_cancel_enabled=False)
        cfg_on = BacktestConfig(
            fv_gate_enabled=True, fv_gate_certainty_threshold=0.50,
            fv_cancel_enabled=False,
        )
        r_off = simulate_market(window, book_index, prices, cfg_off)
        r_on = simulate_market(window, book_index, prices, cfg_on)
        assert r_off.fv_blocked != r_on.fv_blocked


# ---------------------------------------------------------------------------
# Test: Calibration table
# ---------------------------------------------------------------------------

class TestCalibrationTable:
    def test_cert_bucket_boundaries(self):
        assert _cert_bucket(0.50) == "0.50-0.60"
        assert _cert_bucket(0.59) == "0.50-0.60"
        assert _cert_bucket(0.60) == "0.60-0.70"
        assert _cert_bucket(0.699) == "0.60-0.70"
        assert _cert_bucket(0.70) == "0.70-0.80"
        assert _cert_bucket(0.80) == "0.80-0.90"
        assert _cert_bucket(0.90) == "0.90-1.00"
        assert _cert_bucket(1.0) == "0.90-1.00"

    def test_aggregate_results_empty(self):
        cfg = BacktestConfig()
        result = aggregate_results([], cfg)
        assert "error" in result

    def test_aggregate_single_market_win(self):
        cfg = BacktestConfig()
        r = _make_result(pnl=5.0, paired=True, cert=0.65)
        agg = aggregate_results([r], cfg)
        assert agg["total_pnl"] == 5.0
        assert agg["win_rate"] == 1.0
        assert agg["paired_rate"] == 1.0
        assert agg["markets_simulated"] == 1

    def test_aggregate_multiple_markets(self):
        cfg = BacktestConfig()
        results = [
            _make_result(pnl=3.0, paired=True, cert=0.65),
            _make_result(pnl=-2.0, paired=False, cert=0.55,
                up_qty=0.0, dn_qty=0.0, outcome_correct=False),
            _make_result(pnl=1.0, paired=True, cert=0.80),
        ]
        agg = aggregate_results(results, cfg)
        assert agg["total_pnl"] == pytest.approx(2.0, abs=1e-6)
        assert agg["markets_simulated"] == 3
        assert agg["win_rate"] == pytest.approx(2/3, abs=1e-3)

    def test_aggregate_calibration_table_populated(self):
        cfg = BacktestConfig()
        results = [
            _make_result(cert=0.55, outcome_correct=True),
            _make_result(cert=0.55, outcome_correct=False),
            _make_result(cert=0.75, outcome_correct=True),
            _make_result(cert=0.85, outcome_correct=True),
        ]
        agg = aggregate_results(results, cfg)
        cal = agg["calibration_table"]
        assert cal["0.50-0.60"]["n"] == 2
        assert cal["0.50-0.60"]["win_rate"] == 0.5
        assert cal["0.70-0.80"]["n"] == 1
        assert cal["0.80-0.90"]["n"] == 1

    def test_aggregate_max_loss(self):
        cfg = BacktestConfig()
        results = [
            _make_result(pnl=5.0),
            _make_result(pnl=-15.0),
            _make_result(pnl=2.0),
        ]
        agg = aggregate_results(results, cfg)
        assert agg["max_loss"] == -15.0
        assert agg["max_gain"] == 5.0

    def test_aggregate_has_book_coverage(self):
        cfg = BacktestConfig()
        results = [
            _make_result(has_book_data=True),
            _make_result(has_book_data=False),
            _make_result(has_book_data=True),
        ]
        agg = aggregate_results(results, cfg)
        assert agg["book_coverage_rate"] == pytest.approx(2/3, abs=1e-3)


# ---------------------------------------------------------------------------
# Test: build_book_index caching
# ---------------------------------------------------------------------------

class TestBookIndexCaching:
    def test_cache_round_trip(self, tmp_path):
        """Index is saved to cache and loaded correctly on second call."""
        book_log = tmp_path / "book_log_2000-01-01.jsonl"
        book_log.write_text(json.dumps({
            "ts": 1000.0, "token_id": "tok1", "event_type": "book",
            "data": {"bids": [{"price": "0.44", "size": "100"}],
                     "asks": [{"price": "0.46", "size": "100"}]}
        }))

        # First call builds index
        index1 = build_book_index(tmp_path, ["2000-01-01"], cache_dir=tmp_path)
        assert "tok1" in index1

        # Second call uses cache
        index2 = build_book_index(tmp_path, ["2000-01-01"], cache_dir=tmp_path)
        assert "tok1" in index2
        assert index2["tok1"] == index1["tok1"]


# ---------------------------------------------------------------------------
# Test: Experiment YAML configs still load correctly
# ---------------------------------------------------------------------------

class TestExperimentConfigs:
    """Verify all experiment YAML files parse without error."""

    def _config_dir(self) -> pathlib.Path:
        return _PROJECT_ROOT / "experiments"

    def test_baseline_current_loads(self):
        p = self._config_dir() / "baseline_current.yaml"
        if not p.exists():
            pytest.skip("baseline_current.yaml not found")
        cfg = BacktestConfig.from_yaml(p)
        assert cfg.fv_gate_enabled is False
        assert cfg.rungs == 10

    def test_paired_only_loads(self):
        p = self._config_dir() / "paired_only.yaml"
        if not p.exists():
            pytest.skip("paired_only.yaml not found")
        cfg = BacktestConfig.from_yaml(p)
        assert cfg.fv_cancel_enabled is False
        assert cfg.one_sided_abort_enabled is False

    def test_narrow_band_fv_gate_loads(self):
        p = self._config_dir() / "narrow_band_fv_gate.yaml"
        if not p.exists():
            pytest.skip("narrow_band_fv_gate.yaml not found")
        cfg = BacktestConfig.from_yaml(p)
        assert cfg.fv_gate_enabled is True
        assert cfg.fv_gate_certainty_threshold == 0.95

    def test_fv_gate_full_loads(self):
        p = self._config_dir() / "fv_gate_full.yaml"
        if not p.exists():
            pytest.skip("fv_gate_full.yaml not found")
        cfg = BacktestConfig.from_yaml(p)
        assert isinstance(cfg.fv_gate_enabled, bool)

    def test_paired_plus_trend_filter_loads(self):
        p = self._config_dir() / "paired_plus_trend_filter.yaml"
        if not p.exists():
            pytest.skip("paired_plus_trend_filter.yaml not found")
        cfg = BacktestConfig.from_yaml(p)
        assert isinstance(cfg.trend_filter_enabled, bool)

"""Tests: gate-decision fields appear on every POST event in order_log.

Verifies that all 7 gate-context fields (gate_fired, gate_reason, book_mid,
fv_price, fv_certainty, spread, origin) appear on every 'post' event emitted
by DataRecorder.log_order(), regardless of which ladder path produced the order.

CANCEL and FILL events must NOT include these fields (backward-compat invariant).
"""

import json
import pathlib
import time
import pytest
from unittest.mock import MagicMock, patch

from polybot.data.data_recorder import DataRecorder
from polybot.config import BotConfig
from polybot.oms.order_executor import OrderExecutor
from polybot.strategy.ladder_manager import LadderManager
from polybot.order_tracker import OrderTracker
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.types import MarketWindow, Side


GATE_FIELDS = {"gate_fired", "gate_reason", "book_mid", "fv_price", "fv_certainty", "spread", "origin"}
VALID_REASONS = {"fired", "skip_on_gate_miss", "crossed_book", "fv_certainty_below_thresh", "no_eval"}


# ---------------------------------------------------------------------------
# DataRecorder unit tests
# ---------------------------------------------------------------------------

class TestDataRecorderLogOrder:
    def _recorder(self, tmpdir):
        return DataRecorder(data_dir=tmpdir)

    def _read_last(self, tmpdir) -> dict:
        files = list(pathlib.Path(tmpdir).glob("order_log_*.jsonl"))
        assert files, "No order_log file written"
        lines = files[0].read_text().strip().splitlines()
        return json.loads(lines[-1])

    def test_post_event_includes_all_7_gate_fields(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="post", market_id="mkt1", side="UP",
            price=0.50, size=10.0, order_id="o1", reason="ladder",
            gate_fired=True, gate_reason="fired", book_mid=0.55,
            fv_price=0.60, fv_certainty=0.82, spread=0.02, origin="initial_post",
        )
        record = self._read_last(tmp_path)
        for field in GATE_FIELDS:
            assert field in record, f"Missing field: {field}"

    def test_post_event_gate_reason_is_valid_enum(self, tmp_path):
        rec = self._recorder(tmp_path)
        for reason in VALID_REASONS:
            rec.log_order(
                ts=time.time(), event="post", market_id="mkt1", side="UP",
                price=0.50, size=10.0,
                gate_fired=False, gate_reason=reason, book_mid=None,
                fv_price=0.50, fv_certainty=0.0, spread=None, origin="initial_post",
            )
            record = self._read_last(tmp_path)
            assert record["gate_reason"] in VALID_REASONS

    def test_cancel_event_does_not_include_gate_fields(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="cancel", market_id="", side="",
            price=0, size=0, order_id="o1", reason="cancel",
        )
        record = self._read_last(tmp_path)
        for field in GATE_FIELDS:
            assert field not in record, f"Gate field unexpectedly present on cancel: {field}"

    def test_fill_event_does_not_include_gate_fields(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="fill", market_id="mkt1", side="UP",
            price=0.50, size=10.0, order_id="o1", reason="detected",
        )
        record = self._read_last(tmp_path)
        for field in GATE_FIELDS:
            assert field not in record, f"Gate field unexpectedly present on fill: {field}"

    def test_post_without_gate_fired_omits_gate_fields(self, tmp_path):
        """Backward-compat: callers that do not pass gate_fired get no gate fields."""
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="post", market_id="mkt1", side="UP",
            price=0.50, size=10.0, order_id="o1", reason="ladder",
            # No gate kwargs
        )
        record = self._read_last(tmp_path)
        for field in GATE_FIELDS:
            assert field not in record, f"Gate field present when not supplied: {field}"

    def test_post_gate_fields_null_values_written(self, tmp_path):
        """book_mid, fv_price, fv_certainty, spread may legitimately be None."""
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="post", market_id="mkt1", side="DN",
            price=0.45, size=20.0, order_id="o2", reason="ladder",
            gate_fired=False, gate_reason="no_eval", book_mid=None,
            fv_price=None, fv_certainty=None, spread=None, origin="reprice",
        )
        record = self._read_last(tmp_path)
        assert record["book_mid"] is None
        assert record["fv_price"] is None
        assert record["origin"] == "reprice"

    def test_reprice_origin_written_correctly(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="post", market_id="mkt1", side="UP",
            price=0.50, size=10.0,
            gate_fired=True, gate_reason="fired", book_mid=0.55,
            fv_price=None, fv_certainty=None, spread=None,
            origin="reprice",
        )
        record = self._read_last(tmp_path)
        assert record["origin"] == "reprice"


# ---------------------------------------------------------------------------
# OrderExecutor integration: gate kwargs thread through to recorder
# ---------------------------------------------------------------------------

class TestOrderExecutorGateForwarding:
    def _make_executor(self, tmp_path):
        cfg = BotConfig(private_key="0xfake", api_key="k", api_secret="s", api_passphrase="p")
        clob = MagicMock()
        clob.create_order.return_value = {"signed": True}
        clob.post_order.return_value = {"orderID": "ord1", "status": "resting"}
        recorder = DataRecorder(data_dir=tmp_path)
        return OrderExecutor(cfg, clob_client=clob, data_recorder=recorder), tmp_path

    def _read_last(self, tmp_path) -> dict:
        files = list(pathlib.Path(tmp_path).glob("order_log_*.jsonl"))
        lines = files[0].read_text().strip().splitlines()
        return json.loads(lines[-1])

    def test_place_limit_buy_forwards_gate_fields(self, tmp_path):
        executor, tmpdir = self._make_executor(tmp_path)
        executor.place_limit_buy(
            token_id="tok1", price=0.50, size=10.0,
            market_id="mkt1", side=Side.UP,
            gate_fired=True, gate_reason="fired", book_mid=0.55,
            fv_price=0.60, fv_certainty=0.80, spread=0.03, origin="initial_post",
        )
        record = self._read_last(tmpdir)
        assert record["event"] == "post"
        assert record["gate_fired"] is True
        assert record["gate_reason"] == "fired"
        assert record["book_mid"] == pytest.approx(0.55)
        assert record["origin"] == "initial_post"

    def test_place_limit_sell_forwards_gate_fields(self, tmp_path):
        executor, tmpdir = self._make_executor(tmp_path)
        executor.place_limit_sell(
            token_id="tok1", price=0.40, size=10.0,
            market_id="mkt1", side=Side.UP,
            gate_fired=False, gate_reason="no_eval", book_mid=None,
            fv_price=None, fv_certainty=None, spread=None, origin="initial_post",
        )
        record = self._read_last(tmpdir)
        assert record["event"] == "post"
        assert record["gate_fired"] is False
        assert record["gate_reason"] == "no_eval"
        assert record["book_mid"] is None

    def test_place_batch_limit_buys_forwards_gate_fields(self, tmp_path):
        executor, tmpdir = self._make_executor(tmp_path)
        orders = [
            {
                "token_id": "tok1", "price": 0.50, "size": 10.0,
                "market_id": "mkt1", "side": Side.UP,
                "gate_fired": True, "gate_reason": "fired",
                "book_mid": 0.55, "fv_price": 0.60, "fv_certainty": 0.82,
                "spread": 0.03, "origin": "initial_post",
            },
            {
                "token_id": "tok2", "price": 0.48, "size": 8.0,
                "market_id": "mkt1", "side": Side.UP,
                "gate_fired": True, "gate_reason": "fired",
                "book_mid": 0.55, "fv_price": 0.60, "fv_certainty": 0.82,
                "spread": 0.03, "origin": "initial_post",
            },
        ]
        executor.place_batch_limit_buys(orders)
        files = list(pathlib.Path(tmpdir).glob("order_log_*.jsonl"))
        records = [json.loads(l) for l in files[0].read_text().strip().splitlines()]
        post_records = [r for r in records if r["event"] == "post"]
        assert len(post_records) == 2
        for r in post_records:
            for field in GATE_FIELDS:
                assert field in r, f"Batch post missing gate field: {field}"

    def test_cancel_order_does_not_emit_gate_fields(self, tmp_path):
        executor, tmpdir = self._make_executor(tmp_path)
        executor.cancel_order("ord1")
        record = self._read_last(tmpdir)
        assert record["event"] == "cancel"
        for field in GATE_FIELDS:
            assert field not in record


# ---------------------------------------------------------------------------
# LadderManager integration: end-to-end post_ladder emits gate fields
# ---------------------------------------------------------------------------

def _make_market(market_id="mkt-test", timeframe_sec=900):
    now = int(time.time())
    return MarketWindow(
        market_id=market_id,
        condition_id="0xcond",
        asset="BTC",
        timeframe_sec=timeframe_sec,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=now - 60,
        close_epoch=now + (timeframe_sec - 60),
    )


def _make_ladder_manager(tmp_path, book_mid_gate_enabled=False, skip_on_gate_miss=False):
    cfg = BotConfig(
        private_key="0xfake", api_key="k", api_secret="s", api_passphrase="p",
        ladder_rungs=4,
        ladder_width=0.10,
        ladder_spacing=0.02,
        ladder_size_skew=2.0,
        book_mid_gate_enabled=book_mid_gate_enabled,
        book_mid_gate_certainty_threshold=0.55,
        book_mid_gate_max_spread=0.05,
        skip_on_gate_miss=skip_on_gate_miss,
        fair_value_enabled=False,
        fv_gate_enabled=False,
    )
    clob = MagicMock()
    clob.get_order_book.return_value = MagicMock(
        bids=[MagicMock(price="0.44", size="5000")],
        asks=[MagicMock(price="0.46", size="5000")],
    )
    clob.create_order.return_value = {"signed": True}
    clob.post_order.return_value = {"orderID": "o1", "status": "resting"}
    clob.get_open_orders.return_value = []

    recorder = DataRecorder(data_dir=tmp_path)
    executor = OrderExecutor(cfg, clob_client=clob, data_recorder=recorder)
    tracker = OrderTracker()
    positions = PositionManager(cfg, bankroll=5000.0)
    risk = RiskManager(cfg, starting_bankroll=5000.0)
    mgr = LadderManager(cfg, executor, tracker, positions, risk)
    return mgr, recorder, tmp_path


def _read_post_records(tmp_path) -> list[dict]:
    files = list(pathlib.Path(tmp_path).glob("order_log_*.jsonl"))
    if not files:
        return []
    records = [json.loads(l) for l in files[0].read_text().strip().splitlines()]
    return [r for r in records if r["event"] == "post"]


class TestLadderManagerGateInstrumentation:
    def test_initial_post_gate_disabled_emits_no_eval(self, tmp_path):
        """When book_mid_gate_enabled=False, all POST events have gate_reason='no_eval'."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        posts = _read_post_records(tmpdir)
        assert len(posts) > 0, "Expected at least one POST event"
        for r in posts:
            for field in GATE_FIELDS:
                assert field in r, f"POST missing gate field '{field}': {r}"
            assert r["gate_fired"] is False
            assert r["gate_reason"] == "no_eval"
            assert r["origin"] == "initial_post"

    def test_initial_post_gate_fires_emits_fired(self, tmp_path):
        """When gate fires (tight book + high cert), posts have gate_reason='fired'."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=True)
        # up_mid=0.80, dn_mid=0.20 → book_mid_up = 0.80/(0.80+0.20) = 0.80
        # cert = 2*|0.80 - 0.5| = 0.60 > 0.55 threshold → gate fires
        up_mid = 0.80
        dn_mid = 0.20
        # get_midpoint called for: up_best_ask_up correction check, dn_best_ask_dn correction check,
        # then gate: up_mid, dn_mid; then get_best_bid up, get_best_ask up, get_best_bid dn, get_best_ask dn
        # set up_bid < up_ask and dn_bid < dn_ask so spread is tight
        def mock_get_order_book(token_id):
            if token_id == "tok_up":
                book = MagicMock()
                book.bids = [MagicMock(price="0.78", size="5000")]
                book.asks = [MagicMock(price="0.82", size="5000")]
                return book
            else:
                book = MagicMock()
                book.bids = [MagicMock(price="0.18", size="5000")]
                book.asks = [MagicMock(price="0.22", size="5000")]
                return book

        mgr.executor.client.get_order_book.side_effect = mock_get_order_book
        # Mock get_midpoint to return the expected values
        # Called for: artifact-check on up, artifact-check on dn, then gate: up then dn
        midpoint_calls = [0.80, 0.20]  # not 0.99 so no artifact correction
        midpoint_call_idx = [0]

        def mock_get_midpoint(token_id):
            if token_id == "tok_up":
                return up_mid
            else:
                return dn_mid

        with patch.object(mgr.executor, "get_midpoint", side_effect=mock_get_midpoint):
            market = _make_market()
            mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)

        posts = _read_post_records(tmpdir)
        assert len(posts) > 0, "Expected at least one POST (gate fires for UP side only)"
        for r in posts:
            assert r["gate_fired"] is True
            assert r["gate_reason"] == "fired"
            assert r["origin"] == "initial_post"
            assert r["book_mid"] is not None

    def test_reprice_emits_persisted_gate_state(self, tmp_path):
        """Reprice events emit gate_persisted (LadderState.gate_fired), not a re-evaluated gate.

        Updated cycle 28: reprice POST events now carry gate_persisted + gate_reevaluated
        instead of gate_fired, to let analyzers distinguish persisted from live decisions.
        """
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        # Initial post
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        # Manually set gate_fired on the LadderState to simulate a gate-fire window
        state = mgr.ladders[market.market_id]
        state.gate_fired = True
        state.gate_winner_side = Side.UP
        state.gate_budget_cap = 18.0
        # Force reprice by making best_ask move beyond threshold
        mgr.executor.client.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.54", size="5000")],
            asks=[MagicMock(price="0.56", size="5000")],  # moved from 0.46
        )
        state.last_reprice_at = 0  # force cooldown bypass
        markets = {market.market_id: market}
        mgr.reprice_if_needed(markets)
        posts = _read_post_records(tmpdir)
        reprice_posts = [r for r in posts if r.get("origin") == "reprice"]
        assert len(reprice_posts) > 0, "Expected at least one reprice POST event"
        for r in reprice_posts:
            # Reprice events carry gate_persisted, NOT gate_fired
            assert "gate_persisted" in r, f"Reprice POST missing 'gate_persisted': {r}"
            assert "gate_fired" not in r, f"Reprice POST must NOT carry 'gate_fired': {r}"
            assert r["gate_persisted"] is True
            assert r["gate_reevaluated"] is False
            assert r["gate_reason"] == "fired"
            assert r["origin"] == "reprice"
            # Reprice does NOT re-evaluate live book data for gate
            assert r["book_mid"] is None
            assert r["fv_price"] is None

    def test_reprice_gate_not_fired_emits_no_eval(self, tmp_path):
        """Reprice of a non-gate-fired ladder emits gate_persisted=False, gate_reason='no_eval'."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        state = mgr.ladders[market.market_id]
        # gate_fired defaults to False
        assert state.gate_fired is False
        mgr.executor.client.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.54", size="5000")],
            asks=[MagicMock(price="0.56", size="5000")],
        )
        state.last_reprice_at = 0
        mgr.reprice_if_needed({market.market_id: market})
        posts = _read_post_records(tmpdir)
        reprice_posts = [r for r in posts if r.get("origin") == "reprice"]
        for r in reprice_posts:
            assert "gate_persisted" in r, f"Reprice POST missing 'gate_persisted': {r}"
            assert "gate_fired" not in r, f"Reprice POST must NOT carry 'gate_fired': {r}"
            assert r["gate_persisted"] is False
            assert r["gate_reevaluated"] is False
            assert r["gate_reason"] == "no_eval"

    def test_all_7_fields_present_on_every_post_event(self, tmp_path):
        """Integration: every POST event from post_ladder() carries all 7 gate fields."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        posts = _read_post_records(tmpdir)
        assert len(posts) > 0
        for r in posts:
            for field in GATE_FIELDS:
                assert field in r, f"POST event missing required gate field '{field}': {r}"

    def test_cancel_events_have_no_gate_fields(self, tmp_path):
        """CANCEL events from ladder_manager must not carry gate fields."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        mgr.cancel_ladder(market.market_id)
        files = list(pathlib.Path(tmpdir).glob("order_log_*.jsonl"))
        records = [json.loads(l) for l in files[0].read_text().strip().splitlines()]
        cancel_records = [r for r in records if r["event"] == "cancel"]
        for r in cancel_records:
            for field in GATE_FIELDS:
                assert field not in r, f"CANCEL event has unexpected gate field: {field}"

    # -----------------------------------------------------------------------
    # Cycle 28 telemetry: reprice events emit gate_persisted + gate_reevaluated
    # -----------------------------------------------------------------------

    def test_reprice_post_carries_gate_persisted_not_gate_fired(self, tmp_path):
        """Reprice-origin POST events must carry 'gate_persisted', NOT 'gate_fired'."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        state = mgr.ladders[market.market_id]
        state.gate_fired = True
        state.gate_winner_side = Side.UP
        state.gate_budget_cap = 18.0
        mgr.executor.client.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.54", size="5000")],
            asks=[MagicMock(price="0.56", size="5000")],
        )
        state.last_reprice_at = 0
        mgr.reprice_if_needed({market.market_id: market})
        posts = _read_post_records(tmpdir)
        reprice_posts = [r for r in posts if r.get("origin") == "reprice"]
        assert len(reprice_posts) > 0, "Expected at least one reprice POST event"
        for r in reprice_posts:
            assert "gate_persisted" in r, f"Reprice POST missing 'gate_persisted': {r}"
            assert "gate_fired" not in r, f"Reprice POST must NOT carry 'gate_fired': {r}"

    def test_reprice_post_carries_gate_reevaluated_false(self, tmp_path):
        """Reprice-origin POST events must carry gate_reevaluated=False."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        state = mgr.ladders[market.market_id]
        state.last_reprice_at = 0
        mgr.executor.client.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.54", size="5000")],
            asks=[MagicMock(price="0.56", size="5000")],
        )
        mgr.reprice_if_needed({market.market_id: market})
        posts = _read_post_records(tmpdir)
        reprice_posts = [r for r in posts if r.get("origin") == "reprice"]
        assert len(reprice_posts) > 0, "Expected at least one reprice POST event"
        for r in reprice_posts:
            assert r.get("gate_reevaluated") is False, (
                f"Reprice POST 'gate_reevaluated' must be False, got: {r.get('gate_reevaluated')!r}"
            )

    def test_initial_post_still_carries_gate_fired_not_gate_persisted(self, tmp_path):
        """Initial_post events must still carry 'gate_fired' (not broken by reprice rename)."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        posts = _read_post_records(tmpdir)
        initial_posts = [r for r in posts if r.get("origin") == "initial_post"]
        assert len(initial_posts) > 0, "Expected at least one initial_post POST event"
        for r in initial_posts:
            assert "gate_fired" in r, f"Initial_post must carry 'gate_fired': {r}"
            assert "gate_persisted" not in r, (
                f"Initial_post must NOT carry 'gate_persisted': {r}"
            )

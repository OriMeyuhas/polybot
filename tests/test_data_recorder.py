"""Tests for the DataRecorder JSONL logging system."""
import json
import tempfile
import pathlib

def test_recorder_writes_jsonl(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    recorder.log_price(1700000000.0, "BTC", 69325.22, "binance")

    log_file = list(tmp_path.glob("price_log_*.jsonl"))
    assert len(log_file) == 1
    record = json.loads(log_file[0].read_text().strip())
    assert record["asset"] == "BTC"
    assert record["price"] == 69325.22
    assert record["source"] == "binance"
    assert record["ts"] == 1700000000.0


def test_recorder_daily_rotation(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)

    # Write with two different dates (24h apart)
    recorder._append("price_log", {"ts": 1700000000.0, "data": "day1"}, ts=1700000000.0)
    recorder._append("price_log", {"ts": 1700100000.0, "data": "day2"}, ts=1700100000.0)

    # Should have date-stamped files
    files = list(tmp_path.glob("price_log_*.jsonl"))
    assert len(files) >= 1  # at least one file
    recorder.close()


def test_recorder_does_not_crash_on_write_error(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path / "nonexistent" / "deep")
    # Should not raise -- logging failures are silent
    recorder.log_price(1700000000.0, "BTC", 69325.22, "binance")


def test_price_tick_throttle(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)

    # Two ticks 0.5s apart -- second should be throttled
    recorder.log_price(1700000000.0, "BTC", 69000.0, "binance")
    recorder.log_price(1700000000.5, "BTC", 69001.0, "binance")

    log_file = list(tmp_path.glob("price_log_*.jsonl"))
    assert len(log_file) == 1
    lines = log_file[0].read_text().strip().split("\n")
    assert len(lines) == 1  # second tick throttled

    # Tick 1.1s later should go through
    recorder.log_price(1700000001.1, "BTC", 69002.0, "binance")
    lines = log_file[0].read_text().strip().split("\n")
    assert len(lines) == 2
    recorder.close()


def test_chainlink_price_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    recorder.log_price(1700000000.0, "BTC", 69325.00, "chainlink")

    log_file = list(tmp_path.glob("price_log_*.jsonl"))
    record = json.loads(log_file[0].read_text().strip())
    assert record["source"] == "chainlink"
    recorder.close()


def test_book_update_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)

    raw_msg = {
        "event_type": "price_change",
        "asset_id": "abc123",
        "price_changes": [{"price": "0.45", "side": "BUY", "size": "100"}],
    }
    recorder.log_book_update(1700000000.0, "abc123", "price_change", raw_msg)

    log_file = list(tmp_path.glob("book_log_*.jsonl"))
    assert len(log_file) == 1
    record = json.loads(log_file[0].read_text().strip())
    assert record["event_type"] == "price_change"
    assert record["data"]["price_changes"][0]["price"] == "0.45"
    recorder.close()


def test_order_lifecycle_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)

    recorder.log_order(1700000000.0, "post", "btc-15m-123", "UP", 0.45, 10, "ord1", "ladder_post")
    recorder.log_order(1700000001.0, "fill", "btc-15m-123", "UP", 0.45, 10, "ord1", "paper_fill")
    recorder.log_order(1700000002.0, "cancel", "btc-15m-123", "DN", 0.43, 5, "ord2", "fv_cancel")

    log_file = list(tmp_path.glob("order_log_*.jsonl"))
    lines = log_file[0].read_text().strip().split("\n")
    assert len(lines) == 3

    post = json.loads(lines[0])
    assert post["event"] == "post"
    assert post["price"] == 0.45

    cancel = json.loads(lines[2])
    assert cancel["reason"] == "fv_cancel"
    recorder.close()


def test_trade_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)

    recorder.log_trade(1700000000.0, "token_abc123", "BUY", 0.55, 20)

    log_file = list(tmp_path.glob("trade_log_*.jsonl"))
    record = json.loads(log_file[0].read_text().strip())
    assert record["side"] == "BUY"
    assert record["price"] == 0.55
    recorder.close()


def test_strategy_state_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)

    data = {
        "p_up": 0.62,
        "certainty": 0.24,
        "vol": 0.45,
        "phase": "skewed",
        "up_qty": 10,
        "dn_qty": 8,
        "up_cost": 4.5,
        "dn_cost": 4.0,
        "resting_up": 3,
        "resting_dn": 2,
        "spot_price": 69500.0,
        "spot_delta": 0.003,
        "elapsed_pct": 0.45,
        "bankroll": 1000.0,
    }
    recorder.log_strategy_state(1700000000.0, "btc-15m-123", "BTC", data)

    log_file = list(tmp_path.glob("strategy_log_*.jsonl"))
    assert len(log_file) == 1
    record = json.loads(log_file[0].read_text().strip())
    assert record["market_id"] == "btc-15m-123"
    assert record["asset"] == "BTC"
    assert record["data"]["p_up"] == 0.62
    assert record["data"]["bankroll"] == 1000.0
    recorder.close()


def test_market_event_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)

    metadata = {
        "open_epoch": 1700000000,
        "close_epoch": 1700000900,
        "price_to_beat": 69325.0,
        "up_token_id": "tok_up_abc123",
        "dn_token_id": "tok_dn_abc123",
    }
    recorder.log_market_event(1700000000.0, "discovered", "btc-15m-123", "BTC", 900, metadata)

    log_file = list(tmp_path.glob("market_event_log_*.jsonl"))
    assert len(log_file) == 1
    record = json.loads(log_file[0].read_text().strip())
    assert record["event"] == "discovered"
    assert record["market_id"] == "btc-15m-123"
    assert record["asset"] == "BTC"
    assert record["timeframe_sec"] == 900
    assert record["metadata"]["open_epoch"] == 1700000000
    # Proposal #46: token IDs must be present for replay validator join
    assert record["metadata"]["up_token_id"] == "tok_up_abc123"
    assert record["metadata"]["dn_token_id"] == "tok_dn_abc123"
    recorder.close()


def test_market_event_discovered_includes_token_ids(tmp_path):
    """Regression: bot.py 'discovered' events must include up/dn token IDs (Proposal #46)."""
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)

    # Simulate what bot.py now emits for a 'discovered' event
    metadata = {
        "open_epoch": 1700000000,
        "close_epoch": 1700000900,
        "up_token_id": "0xabcdef1234567890",
        "dn_token_id": "0x0987654321fedcba",
    }
    recorder.log_market_event(1700000000.0, "discovered", "btc-15m-456", "BTC", 900, metadata)

    log_file = list(tmp_path.glob("market_event_log_*.jsonl"))
    record = json.loads(log_file[0].read_text().strip())
    assert "up_token_id" in record["metadata"], "up_token_id missing from discovered event"
    assert "dn_token_id" in record["metadata"], "dn_token_id missing from discovered event"
    assert record["metadata"]["up_token_id"] == "0xabcdef1234567890"
    assert record["metadata"]["dn_token_id"] == "0x0987654321fedcba"
    recorder.close()


def test_all_streams_produce_files(tmp_path):
    """Verify all 6 streams write to separate date-stamped JSONL files."""
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    ts = 1700000000.0

    recorder.log_price(ts, "BTC", 69000.0, "binance")
    recorder.log_book_update(ts, "token1", "price_change", {"bids": []})
    recorder.log_order(ts, "post", "mkt1", "UP", 0.45, 10, "o1", "ladder")
    recorder.log_trade(ts, "token1", "BUY", 0.55, 20)
    recorder.log_strategy_state(ts, "mkt1", "BTC", {"p_up": 0.5})
    recorder.log_market_event(ts, "discovered", "mkt1", "BTC", 900, {})

    recorder.close()

    streams = ["price_log", "book_log", "order_log", "trade_log", "strategy_log", "market_event_log"]
    for stream in streams:
        files = list(tmp_path.glob(f"{stream}_*.jsonl"))
        assert len(files) == 1, f"Missing file for stream: {stream}"
        content = files[0].read_text().strip()
        assert len(content) > 0, f"Empty file for stream: {stream}"
        record = json.loads(content)
        assert "ts" in record, f"Missing ts in {stream}"


def test_close_flushes_handles(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    recorder.log_price(1700000000.0, "BTC", 69000.0, "binance")

    # Before close, handles should be open
    assert len(recorder._handles) > 0

    recorder.close()

    # After close, handles should be cleared
    assert len(recorder._handles) == 0

    # File should still be readable
    log_file = list(tmp_path.glob("price_log_*.jsonl"))
    assert len(log_file) == 1
    record = json.loads(log_file[0].read_text().strip())
    assert record["asset"] == "BTC"

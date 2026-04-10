"""Tests for _log_strategy_states() — Proposal #44.

Verifies the strategy_log stream actually writes files (previously broken
because Side.DN was referenced but only Side.DOWN exists in Side enum).
"""
import json
import pathlib
import time

import pytest

from polybot.bot import Bot
from polybot.config import BotConfig
from polybot.types import MarketWindow, Side


def _make_bot(tmp_path: pathlib.Path) -> Bot:
    cfg = BotConfig(dry_run=True, bankroll=500)
    bot = Bot(cfg)
    # Redirect recorder to tmp_path so we can inspect output
    bot.data_recorder.close()
    from polybot.data.data_recorder import DataRecorder
    bot.data_recorder = DataRecorder(data_dir=tmp_path)
    return bot


def _make_market(mid: str = "btc-15m-100") -> MarketWindow:
    now = int(time.time())
    return MarketWindow(
        market_id=mid,
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up_abc",
        dn_token_id="tok_dn_abc",
        open_epoch=now - 60,
        close_epoch=now + 840,
    )


class TestStrategyLogWrites:
    def test_log_strategy_states_writes_file(self, tmp_path):
        """_log_strategy_states must produce a strategy_log_*.jsonl file."""
        bot = _make_bot(tmp_path)
        market = _make_market()
        now = int(time.time())

        # Prime fair_values dict so the market gets processed
        fair_values = {market.market_id: (0.62, 0.45)}

        # Call with a 1-element active list; use last_strategy_log_ts=0 so it runs immediately
        bot._last_strategy_log_ts.clear()
        bot._log_strategy_states(now, [market], fair_values)

        files = list(tmp_path.glob("strategy_log_*.jsonl"))
        assert len(files) == 1, (
            f"Expected 1 strategy_log file, found {len(files)}. "
            f"Files in dir: {list(tmp_path.iterdir())}"
        )
        content = files[0].read_text().strip()
        assert content, "strategy_log file is empty"

        record = json.loads(content)
        assert record["market_id"] == market.market_id
        assert record["asset"] == "BTC"
        assert "data" in record
        assert "p_up" in record["data"]
        assert "resting_up" in record["data"]
        assert "resting_dn" in record["data"]

    def test_log_strategy_states_throttled(self, tmp_path):
        """Second call within 5s should NOT write another record."""
        bot = _make_bot(tmp_path)
        market = _make_market()
        now = int(time.time())
        fair_values = {market.market_id: (0.55, None)}

        bot._last_strategy_log_ts.clear()
        bot._log_strategy_states(now, [market], fair_values)
        # Call again immediately — throttle should block
        bot._log_strategy_states(now + 1, [market], fair_values)

        files = list(tmp_path.glob("strategy_log_*.jsonl"))
        assert len(files) == 1
        lines = [l for l in files[0].read_text().strip().split("\n") if l]
        assert len(lines) == 1, f"Expected 1 line (throttled), got {len(lines)}"

    def test_log_strategy_states_writes_after_throttle_expires(self, tmp_path):
        """After 6s elapsed, a second write should produce a second record."""
        bot = _make_bot(tmp_path)
        market = _make_market()
        now = int(time.time())
        fair_values = {market.market_id: (0.55, None)}

        bot._last_strategy_log_ts.clear()
        bot._log_strategy_states(now, [market], fair_values)
        # 6 seconds later — past the 5s throttle
        bot._log_strategy_states(now + 6, [market], fair_values)

        files = list(tmp_path.glob("strategy_log_*.jsonl"))
        assert len(files) == 1
        lines = [l for l in files[0].read_text().strip().split("\n") if l]
        assert len(lines) == 2, f"Expected 2 lines after throttle expires, got {len(lines)}"

    def test_side_down_attr_valid(self):
        """Regression: Side.DOWN must exist (Side.DN was the bad reference)."""
        assert hasattr(Side, "DOWN"), "Side.DOWN must exist"
        assert not hasattr(Side, "DN"), "Side.DN should not exist — use Side.DOWN"

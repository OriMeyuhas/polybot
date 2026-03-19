"""Tests for on-chain token redeemer module."""

import asyncio
import pytest
from polybot.redeemer import Redeemer, RedemptionEntry


class TestQueueRedemption:
    def test_queue_adds_to_pending(self):
        r = Redeemer()
        r.queue_redemption("cond_1", ["tok_a", "tok_b"])
        assert "cond_1" in r.pending
        assert r.pending["cond_1"].token_ids == ["tok_a", "tok_b"]
        assert r.pending["cond_1"].attempts == 0

    def test_duplicate_queue_ignored(self):
        r = Redeemer()
        r.queue_redemption("cond_1", ["tok_a"])
        r.queue_redemption("cond_1", ["tok_x", "tok_y"])
        # Still the original entry
        assert r.pending["cond_1"].token_ids == ["tok_a"]

    def test_duplicate_ignored_if_in_failed(self):
        r = Redeemer(max_retries=1)
        r.queue_redemption("cond_1", ["tok_a"])
        # Manually move to failed
        r.failed["cond_1"] = r.pending.pop("cond_1")
        r.queue_redemption("cond_1", ["tok_a"])
        assert "cond_1" not in r.pending


class TestRecordFailure:
    def test_failed_after_max_retries(self):
        r = Redeemer(max_retries=3)
        r.queue_redemption("cond_1", ["tok_a"])
        for _ in range(3):
            r._record_failure("cond_1")
        assert "cond_1" not in r.pending
        assert "cond_1" in r.failed
        assert r.failed["cond_1"].attempts == 3

    def test_still_pending_before_max_retries(self):
        r = Redeemer(max_retries=3)
        r.queue_redemption("cond_1", ["tok_a"])
        r._record_failure("cond_1")
        assert "cond_1" in r.pending
        assert r.pending["cond_1"].attempts == 1

    def test_record_failure_unknown_id_noop(self):
        r = Redeemer()
        r._record_failure("nonexistent")  # should not raise


class TestRecordSuccess:
    def test_success_removes_from_pending(self):
        r = Redeemer()
        r.queue_redemption("cond_1", ["tok_a"])
        r._record_success("cond_1")
        assert "cond_1" not in r.pending

    def test_success_removes_from_failed(self):
        r = Redeemer(max_retries=1)
        r.queue_redemption("cond_1", ["tok_a"])
        r._record_failure("cond_1")
        assert "cond_1" in r.failed
        r._record_success("cond_1")
        assert "cond_1" not in r.failed

    def test_success_unknown_id_noop(self):
        r = Redeemer()
        r._record_success("nonexistent")  # should not raise


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_successful_redemption(self):
        r = Redeemer()
        r.queue_redemption("cond_1", ["tok_a"])

        async def mock_redeem(cid, token_ids):
            return 100.0

        # Run one iteration then cancel
        task = asyncio.create_task(r.run(mock_redeem))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "cond_1" not in r.pending

    @pytest.mark.asyncio
    async def test_failed_redemption_moves_to_failed(self):
        r = Redeemer(max_retries=2, backoff_sec=0.01)
        r.queue_redemption("cond_1", ["tok_a"])

        async def mock_redeem(cid, token_ids):
            raise RuntimeError("tx failed")

        # Simulate what run() does: call redeem, record failure, repeat
        for _ in range(2):
            entry = r.pending.get("cond_1")
            if entry is None:
                break
            try:
                await mock_redeem("cond_1", entry.token_ids)
            except Exception:
                r._record_failure("cond_1")

        assert "cond_1" not in r.pending
        assert "cond_1" in r.failed
        assert r.failed["cond_1"].attempts == 2

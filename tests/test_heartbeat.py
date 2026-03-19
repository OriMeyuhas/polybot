"""Tests for polybot.heartbeat module."""

from polybot.heartbeat import Heartbeat


class TestHeartbeat:
    """Unit tests for the Heartbeat class."""

    def test_initially_healthy(self):
        hb = Heartbeat()
        assert hb.is_healthy() is True

    def test_unhealthy_after_max_failures(self):
        hb = Heartbeat(max_failures=2)
        hb._record_failure()
        assert hb.is_healthy() is True  # only 1 failure so far
        hb._record_failure()
        assert hb.is_healthy() is False  # 2 failures == max_failures

    def test_recovery_resets_health(self):
        hb = Heartbeat(max_failures=2)
        hb._record_failure()
        hb._record_failure()
        assert hb.is_healthy() is False
        hb._record_success()
        assert hb.is_healthy() is True

    def test_callback_invoked_on_connection_lost(self):
        called = []
        hb = Heartbeat(max_failures=2)
        hb._on_connection_lost = lambda: called.append(True)
        hb._record_failure()
        hb._record_failure()
        assert len(called) == 1

    def test_callback_not_re_invoked_on_additional_failures(self):
        called = []
        hb = Heartbeat(max_failures=2)
        hb._on_connection_lost = lambda: called.append(True)
        hb._record_failure()
        hb._record_failure()
        hb._record_failure()  # additional failure beyond max
        hb._record_failure()
        assert len(called) == 1  # callback fired only once

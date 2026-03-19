import asyncio
from polybot.oms.heartbeat import Heartbeat


def test_initial_state():
    hb = Heartbeat(interval_sec=5.0, max_failures=2)
    assert hb.is_healthy() is True


def test_record_failure():
    hb = Heartbeat(interval_sec=5.0, max_failures=2)
    hb._record_failure()
    assert hb.is_healthy() is True  # 1 failure, threshold is 2
    hb._record_failure()
    assert hb.is_healthy() is False  # 2 failures


def test_record_success_resets():
    hb = Heartbeat(interval_sec=5.0, max_failures=2)
    hb._record_failure()
    hb._record_failure()
    assert hb.is_healthy() is False
    hb._record_success()
    assert hb.is_healthy() is True

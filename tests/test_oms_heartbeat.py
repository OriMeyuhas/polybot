"""Tests for OMS Heartbeat health transitions, recovery callback, and consecutive-success gating."""

from polybot.oms.heartbeat import Heartbeat


def test_healthy_by_default():
    """New Heartbeat is healthy."""
    hb = Heartbeat(interval_sec=5.0, max_failures=2)
    assert hb.is_healthy() is True


def test_unhealthy_after_max_failures():
    """After max_failures calls to _record_failure(), is_healthy() returns False."""
    hb = Heartbeat(interval_sec=5.0, max_failures=2)
    hb._record_failure()
    assert hb.is_healthy() is True  # 1 failure, threshold is 2
    hb._record_failure()
    assert hb.is_healthy() is False  # 2 failures = unhealthy


def test_recovery_requires_threshold_successes():
    """After becoming unhealthy, a single success makes is_healthy True
    but is_recovering stays True. Only after recovery_threshold consecutive
    successes does is_recovering return False."""
    hb = Heartbeat(interval_sec=5.0, max_failures=2, recovery_threshold=3)
    # Go unhealthy
    hb._record_failure()
    hb._record_failure()
    assert hb.is_healthy() is False
    assert hb.is_recovering() is True

    # One success -> healthy but still recovering
    hb._record_success()
    assert hb.is_healthy() is True
    assert hb.is_recovering() is True

    # Two successes -> still recovering
    hb._record_success()
    assert hb.is_recovering() is True

    # Three successes -> recovered
    hb._record_success()
    assert hb.is_recovering() is False


def test_connection_lost_callback_fires_once():
    """on_connection_lost fires exactly once when crossing the failure threshold,
    not on every subsequent failure."""
    calls = []
    hb = Heartbeat(interval_sec=5.0, max_failures=2, recovery_threshold=3)
    hb._on_connection_lost = lambda: calls.append("lost")

    hb._record_failure()
    assert len(calls) == 0  # below threshold

    hb._record_failure()
    assert len(calls) == 1  # crossed threshold

    hb._record_failure()
    assert len(calls) == 1  # already fired, should not fire again

    hb._record_failure()
    assert len(calls) == 1  # still only once


def test_connection_recovered_callback_fires_after_threshold():
    """Recovery callback fires exactly once after recovery_threshold consecutive successes."""
    lost_calls = []
    recovered_calls = []
    hb = Heartbeat(interval_sec=5.0, max_failures=2, recovery_threshold=3)
    hb._on_connection_lost = lambda: lost_calls.append("lost")
    hb._on_connection_recovered = lambda: recovered_calls.append("recovered")

    # Go unhealthy
    hb._record_failure()
    hb._record_failure()
    assert len(lost_calls) == 1

    # Recover with 3 consecutive successes
    hb._record_success()
    assert len(recovered_calls) == 0
    hb._record_success()
    assert len(recovered_calls) == 0
    hb._record_success()
    assert len(recovered_calls) == 1

    # Additional successes should not fire again
    hb._record_success()
    assert len(recovered_calls) == 1


def test_recovery_resets_on_intermittent_failure():
    """If a failure interrupts recovery, the consecutive success counter resets.
    Recovery callback only fires after a full run of threshold successes."""
    recovered_calls = []
    hb = Heartbeat(interval_sec=5.0, max_failures=2, recovery_threshold=3)
    hb._on_connection_lost = lambda: None
    hb._on_connection_recovered = lambda: recovered_calls.append("recovered")

    # Go unhealthy
    hb._record_failure()
    hb._record_failure()

    # Partial recovery: 2 successes then a failure
    hb._record_success()
    hb._record_success()
    hb._record_failure()  # Interrupts recovery
    assert len(recovered_calls) == 0

    # _was_unhealthy stays True during intermittent failures
    # Now do full recovery_threshold successes
    hb._record_success()
    hb._record_success()
    hb._record_success()
    assert len(recovered_calls) == 1


def test_no_recovery_callback_if_never_unhealthy():
    """Continuous successes should never fire the recovery callback."""
    recovered_calls = []
    hb = Heartbeat(interval_sec=5.0, max_failures=2, recovery_threshold=3)
    hb._on_connection_recovered = lambda: recovered_calls.append("recovered")

    for _ in range(20):
        hb._record_success()

    assert len(recovered_calls) == 0


def test_is_recovering_false_by_default():
    """A fresh heartbeat is not recovering."""
    hb = Heartbeat(interval_sec=5.0, max_failures=2, recovery_threshold=3)
    assert hb.is_recovering() is False


def test_config_heartbeat_recovery_threshold():
    """BotConfig has heartbeat_recovery_threshold field with default 3."""
    from polybot.config import BotConfig
    cfg = BotConfig()
    assert cfg.heartbeat_recovery_threshold == 3

"""Heartbeat task for Polymarket CLOB session keepalive."""

import asyncio
import logging
from typing import Callable

log = logging.getLogger(__name__)


class Heartbeat:
    """Sends periodic heartbeats to keep Polymarket session alive.

    Polymarket cancels ALL open orders after 10 seconds without a heartbeat.
    We send every 5 seconds (configurable) for safety margin.
    """

    def __init__(self, interval_sec: float = 5.0, max_failures: int = 2):
        self._interval = interval_sec
        self._max_failures = max_failures
        self._consecutive_failures = 0
        self._healthy = True
        self._on_connection_lost: Callable | None = None
        self._connection_lost_fired = False
        self._heartbeat_id: str | None = None

    def is_healthy(self) -> bool:
        return self._healthy

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures and not self._connection_lost_fired:
            self._healthy = False
            self._connection_lost_fired = True
            log.error("Heartbeat failed %d consecutive times — connection lost",
                      self._consecutive_failures)
            if self._on_connection_lost:
                self._on_connection_lost()

    def _record_success(self, heartbeat_id: str | None = None) -> None:
        was_unhealthy = not self._healthy
        self._consecutive_failures = 0
        self._healthy = True
        self._connection_lost_fired = False
        if heartbeat_id:
            self._heartbeat_id = heartbeat_id
        if was_unhealthy:
            log.info("Heartbeat recovered")

    async def run(self, client, on_connection_lost: Callable) -> None:
        """Main heartbeat loop. Runs as an independent async task."""
        self._on_connection_lost = on_connection_lost
        log.info("Heartbeat started (interval=%.1fs, max_failures=%d)",
                 self._interval, self._max_failures)
        while True:
            try:
                # TODO: Replace with actual Polymarket heartbeat API call
                # resp = client.send_heartbeat(self._heartbeat_id)
                # self._record_success(resp.get("heartbeat_id"))
                self._record_success()
            except Exception as exc:
                log.warning("Heartbeat failed: %s", exc)
                self._record_failure()
            await asyncio.sleep(self._interval)

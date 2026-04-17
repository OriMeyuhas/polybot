"""CLOB heartbeat — keeps session alive, tracks connection health."""

import asyncio
import logging
from typing import Callable

logger = logging.getLogger(__name__)


class Heartbeat:
    """Posts heartbeat to CLOB at regular intervals, tracks health."""

    def __init__(
        self,
        interval_sec: float = 5.0,
        max_failures: int = 2,
        recovery_threshold: int = 3,
    ):
        self._interval_sec = interval_sec
        self._max_failures = max_failures
        self._recovery_threshold = recovery_threshold
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._was_unhealthy = False
        self._connection_lost_fired = False
        self._running = False
        self._on_connection_lost: Callable | None = None
        self._on_connection_recovered: Callable | None = None

    def is_healthy(self) -> bool:
        return self._consecutive_failures < self._max_failures

    def is_recovering(self) -> bool:
        """True if we are recovering from a connection loss episode."""
        return self._was_unhealthy

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        # Reset consecutive successes on any failure
        self._consecutive_successes = 0
        if self._consecutive_failures >= self._max_failures and not self._connection_lost_fired:
            self._was_unhealthy = True
            self._connection_lost_fired = True
            logger.error(
                "Heartbeat failed %d consecutive times — connection lost",
                self._consecutive_failures,
            )
            if self._on_connection_lost:
                self._on_connection_lost()

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._connection_lost_fired = False
        if self._was_unhealthy:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self._recovery_threshold:
                self._was_unhealthy = False
                self._consecutive_successes = 0
                logger.info("Heartbeat fully recovered after %d consecutive successes",
                            self._recovery_threshold)
                if self._on_connection_recovered:
                    self._on_connection_recovered()

    async def run(
        self,
        client,
        on_connection_lost: Callable | None = None,
        on_connection_recovered: Callable | None = None,
    ) -> None:
        self._on_connection_lost = on_connection_lost
        self._on_connection_recovered = on_connection_recovered
        self._running = True
        while self._running:
            try:
                # Run sync HTTP call in thread to avoid blocking the event loop
                await asyncio.get_event_loop().run_in_executor(
                    None, client.post_heartbeat
                )
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning(
                    "Heartbeat failed (%d/%d): %s",
                    self._consecutive_failures, self._max_failures, e,
                )
            await asyncio.sleep(self._interval_sec)

    async def stop(self) -> None:
        self._running = False

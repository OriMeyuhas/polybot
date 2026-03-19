"""CLOB heartbeat — keeps session alive, tracks connection health."""

import asyncio
import logging
from typing import Callable

logger = logging.getLogger(__name__)


class Heartbeat:
    """Posts heartbeat to CLOB at regular intervals, tracks health."""

    def __init__(self, interval_sec: float = 5.0, max_failures: int = 2):
        self._interval_sec = interval_sec
        self._max_failures = max_failures
        self._consecutive_failures = 0
        self._running = False

    def is_healthy(self) -> bool:
        return self._consecutive_failures < self._max_failures

    def _record_failure(self) -> None:
        self._consecutive_failures += 1

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    async def run(self, client, on_connection_lost: Callable | None = None) -> None:
        self._running = True
        while self._running:
            try:
                client.post_heartbeat()
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning(
                    "Heartbeat failed (%d/%d): %s",
                    self._consecutive_failures, self._max_failures, e,
                )
                if not self.is_healthy() and on_connection_lost:
                    on_connection_lost()
            await asyncio.sleep(self._interval_sec)

    async def stop(self) -> None:
        self._running = False

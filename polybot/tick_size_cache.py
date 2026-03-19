"""Tick size cache with TTL and invalidation support."""

import time


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest valid tick."""
    return round(round(price / tick_size) * tick_size, 10)


class TickSizeCache:
    def __init__(self, client, ttl_sec: float = 60.0):
        self._client = client
        self._ttl = ttl_sec
        self._cache: dict[str, tuple[float, float]] = {}

    def get_tick_size(self, condition_id: str) -> float:
        entry = self._cache.get(condition_id)
        now = time.monotonic()
        if entry is not None:
            tick_size, fetched_at = entry
            if now - fetched_at < self._ttl:
                return tick_size
        tick_size = self._client.get_tick_size(condition_id)
        self._cache[condition_id] = (tick_size, now)
        return tick_size

    def invalidate(self, condition_id: str) -> None:
        self._cache.pop(condition_id, None)

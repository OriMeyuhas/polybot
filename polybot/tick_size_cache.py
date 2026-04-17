"""Tick size cache with TTL and invalidation support."""

import time


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest valid tick."""
    return round(round(price / tick_size) * tick_size, 10)


def _fetch_tick_size(client, key: str, token_id: str | None = None) -> tuple[float, bool]:
    """Fetch tick size from the client, supporting both mock and real py-clob-client.

    Mock clients expose ``get_tick_size(key)``.  The real py-clob-client does
    *not* have a standalone ``get_tick_size`` method — the tick size is obtained
    via ``client.get_order_book(token_id).tick_size``.

    Returns ``(tick_size, is_authoritative)`` — authoritative means the value
    came from a real API call and is safe to cache.  Fallback values are NOT
    authoritative.
    """
    # Preferred path: real py-clob-client — order book carries tick_size
    if token_id is not None:
        try:
            book = client.get_order_book(token_id)
            ts = getattr(book, "tick_size", None)
            if ts is not None:
                return float(ts), True
        except Exception:
            pass

    # Fallback path: mock / test clients that implement get_tick_size directly
    getter = getattr(client, "get_tick_size", None)
    if getter is not None:
        try:
            return getter(key), True
        except Exception:
            pass

    # Hardcoded fallback — all Polymarket up-or-down markets use 0.001
    return 0.001, False


class TickSizeCache:
    def __init__(self, client, ttl_sec: float = 60.0):
        self._client = client
        self._ttl = ttl_sec
        self._cache: dict[str, tuple[float, float]] = {}

    def get_tick_size(self, key: str, *, token_id: str | None = None) -> float:
        """Return the cached tick size for *key*, fetching on miss or TTL expiry.

        Parameters
        ----------
        key:
            Cache key (typically a condition_id or token_id).
        token_id:
            Optional token_id used to query the order book when the client
            does not expose a standalone ``get_tick_size`` method (i.e. the
            real py-clob-client).
        """
        entry = self._cache.get(key)
        now = time.monotonic()
        if entry is not None:
            tick_size, fetched_at = entry
            if now - fetched_at < self._ttl:
                return tick_size
        tick_size, authoritative = _fetch_tick_size(self._client, key, token_id)
        if authoritative:
            self._cache[key] = (tick_size, now)
        return tick_size

    def evict_stale(self, max_age_factor: float = 10.0) -> int:
        """Remove entries older than max_age_factor * TTL. Returns evicted count."""
        now = time.monotonic()
        max_age = self._ttl * max_age_factor
        stale_keys = [
            k for k, (_, fetched_at) in self._cache.items()
            if now - fetched_at > max_age
        ]
        for k in stale_keys:
            del self._cache[k]
        return len(stale_keys)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

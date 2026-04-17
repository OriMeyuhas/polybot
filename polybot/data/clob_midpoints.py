"""CLOB midpoint poller — REST polling for canonical midpoint prices."""

import asyncio
import logging
import time
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

# Concurrency and backoff constants
_MAX_CONCURRENT = 5
_MAX_POLL_INTERVAL = 30.0


class ClobMidpointPoller:
    """Polls CLOB /midpoints endpoint for canonical midpoint prices."""

    def __init__(self):
        self._token_ids: set[str] = set()
        self._midpoints: dict[str, Decimal] = {}
        self._running = False
        self._backoff_until: float = 0.0
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        self._consecutive_429s: int = 0
        self._base_interval: float = 3.0
        self._effective_interval: float = 3.0

    def register_tokens(self, token_ids: list[str]) -> None:
        self._token_ids.update(token_ids)

    def set_tokens(self, token_ids: list[str]) -> None:
        """Replace the full token set; prune midpoints for removed tokens."""
        new_set = set(token_ids)
        removed = self._token_ids - new_set
        self._token_ids = new_set
        for tid in removed:
            self._midpoints.pop(tid, None)

    def remove_tokens(self, token_ids: list[str]) -> None:
        for tid in token_ids:
            self._token_ids.discard(tid)
            self._midpoints.pop(tid, None)

    def get_mid(self, token_id: str) -> Decimal | None:
        return self._midpoints.get(token_id)

    async def _fetch_one(self, client: httpx.AsyncClient, clob_host: str, token_id: str) -> None:
        """Fetch a single token's midpoint (rate-limited via semaphore)."""
        async with self._semaphore:
            try:
                resp = await client.get(
                    f"{clob_host}/midpoint",
                    params={"token_id": token_id},
                )
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 5.0))
                    self._backoff_until = time.monotonic() + retry_after
                    self._consecutive_429s += 1
                    # Exponential backoff: double interval on each consecutive 429
                    self._effective_interval = min(
                        self._base_interval * (2 ** self._consecutive_429s),
                        _MAX_POLL_INTERVAL,
                    )
                    logger.warning(
                        "429 rate-limited for %s — backoff %.1fs, interval now %.1fs",
                        token_id[:16], retry_after, self._effective_interval,
                    )
                    return
                if resp.status_code == 200:
                    # Successful response — reset adaptive interval
                    self._consecutive_429s = 0
                    self._effective_interval = self._base_interval
                    data = resp.json()
                    mid_str = data.get("mid") if isinstance(data, dict) else data
                    if mid_str is not None:
                        try:
                            self._midpoints[token_id] = Decimal(str(mid_str))
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.debug("Midpoint poll failed for %s: %s", token_id[:16], e)

    async def run(self, clob_host: str = "https://clob.polymarket.com", poll_interval: float = 3.0) -> None:
        self._running = True
        self._base_interval = poll_interval
        self._effective_interval = poll_interval
        async with httpx.AsyncClient(timeout=10) as client:
            while self._running:
                if self._token_ids and time.monotonic() >= self._backoff_until:
                    tasks = [
                        self._fetch_one(client, clob_host, tid)
                        for tid in list(self._token_ids)
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(self._effective_interval)

    async def stop(self) -> None:
        self._running = False

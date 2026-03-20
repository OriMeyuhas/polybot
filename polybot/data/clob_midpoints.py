"""CLOB midpoint poller — REST polling for canonical midpoint prices."""

import asyncio
import logging
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)


class ClobMidpointPoller:
    """Polls CLOB /midpoints endpoint for canonical midpoint prices."""

    def __init__(self):
        self._token_ids: set[str] = set()
        self._midpoints: dict[str, Decimal] = {}
        self._running = False

    def register_tokens(self, token_ids: list[str]) -> None:
        self._token_ids.update(token_ids)

    def remove_tokens(self, token_ids: list[str]) -> None:
        for tid in token_ids:
            self._token_ids.discard(tid)
            self._midpoints.pop(tid, None)

    def get_mid(self, token_id: str) -> Decimal | None:
        return self._midpoints.get(token_id)

    async def run(self, clob_host: str = "https://clob.polymarket.com", poll_interval: float = 2.0) -> None:
        self._running = True
        async with httpx.AsyncClient(timeout=10) as client:
            while self._running:
                if self._token_ids:
                    for token_id in list(self._token_ids):
                        try:
                            resp = await client.get(
                                f"{clob_host}/midpoint",
                                params={"token_id": token_id},
                            )
                            if resp.status_code == 200:
                                data = resp.json()
                                mid_str = data.get("mid") if isinstance(data, dict) else data
                                if mid_str is not None:
                                    try:
                                        self._midpoints[token_id] = Decimal(str(mid_str))
                                    except (ValueError, TypeError):
                                        pass
                        except Exception as e:
                            logger.debug("Midpoint poll failed for %s: %s", token_id[:16], e)
                await asyncio.sleep(poll_interval)

    async def stop(self) -> None:
        self._running = False

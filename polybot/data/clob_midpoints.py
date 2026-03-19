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
                    try:
                        resp = await client.post(
                            f"{clob_host}/midpoints",
                            json=list(self._token_ids),
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            for token_id, mid_str in data.items():
                                try:
                                    self._midpoints[token_id] = Decimal(str(mid_str))
                                except (ValueError, TypeError):
                                    pass
                    except Exception as e:
                        logger.warning("CLOB midpoint poll failed: %s", e)
                await asyncio.sleep(poll_interval)

    async def stop(self) -> None:
        self._running = False

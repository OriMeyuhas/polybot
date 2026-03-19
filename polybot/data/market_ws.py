"""Polymarket market WebSocket client — order book subscriptions."""

import asyncio
import json
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class MarketWSClient:
    """WebSocket client for Polymarket market data (order books)."""

    def __init__(
        self,
        url: str,
        on_message: Callable[[dict[str, Any]], None],
        ping_interval_sec: float = 10,
    ):
        self._url = url
        self._on_message = on_message
        self._ping_interval_sec = ping_interval_sec
        self._ws = None
        self._is_connected = False
        self._reconnect_count = 0
        self._token_ids: list[str] = []
        self._running = False
        self._tasks: list[asyncio.Task] = []

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def _build_subscribe_msg(self, token_ids: list[str]) -> dict:
        return {"assets_ids": list(token_ids), "type": "market"}

    def _backoff_delay(self) -> float:
        return min(2 ** self._reconnect_count, 60.0)

    def update_subscriptions(self, token_ids: list[str]) -> None:
        self._token_ids = list(token_ids)

    async def run(self, token_ids: list[str]) -> None:
        import websockets
        self._token_ids = list(token_ids)
        self._running = True

        while self._running:
            try:
                async with websockets.connect(self._url) as ws:
                    self._ws = ws
                    self._is_connected = True
                    self._reconnect_count = 0
                    logger.info("Market WS connected to %s", self._url)

                    if self._token_ids:
                        sub = self._build_subscribe_msg(self._token_ids)
                        await ws.send(json.dumps(sub))

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    self._tasks.append(ping_task)

                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            try:
                                msg = json.loads(raw)
                                self._on_message(msg)
                            except (json.JSONDecodeError, Exception) as e:
                                logger.warning("Market WS parse error: %s", e)
                    finally:
                        ping_task.cancel()
                        self._tasks.remove(ping_task)

            except Exception as e:
                self._is_connected = False
                if not self._running:
                    break
                delay = self._backoff_delay()
                self._reconnect_count += 1
                logger.warning("Market WS disconnected: %s — reconnecting in %.1fs", e, delay)
                await asyncio.sleep(delay)

        self._is_connected = False

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval_sec)
                await ws.ping()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._is_connected = False

"""Multi-asset price feed — Binance WS primary, CoinGecko fallback."""

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

# Asset symbol → Binance pair
_ASSET_TO_PAIR = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
}

# Default CoinGecko IDs
_DEFAULT_CG_IDS = ("bitcoin", "ethereum", "solana", "ripple")


class MultiAssetPriceFeed:
    """Streams spot prices for multiple crypto assets."""

    def __init__(
        self,
        assets: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP"),
        coingecko_ids: tuple[str, ...] = _DEFAULT_CG_IDS,
        ws_base_url: str = "wss://stream.binance.com:9443",
        fallback_interval_sec: float = 2.0,
        on_tick: Callable | None = None,
    ):
        self._assets = assets
        self._coingecko_ids = coingecko_ids
        self._ws_base_url = ws_base_url
        self._fallback_interval_sec = fallback_interval_sec
        self._on_tick = on_tick

        self._prices: dict[str, Decimal] = {}
        self._last_ts: dict[str, float] = {}
        self._running = False
        self._reconnect_count = 0

    def get_price(self, asset: str) -> Decimal | None:
        return self._prices.get(asset)

    def get_price_age(self, asset: str) -> float | None:
        """Age in seconds of the cached price, or None if never received."""
        ts = self._last_ts.get(asset)
        if ts is None:
            return None
        return time.time() - ts

    def get_price_if_fresh(self, asset: str, max_age_sec: float) -> Decimal | None:
        """Return cached price only if updated within *max_age_sec*; else None."""
        age = self.get_price_age(asset)
        if age is None or age > max_age_sec:
            return None
        return self._prices.get(asset)

    @staticmethod
    async def fetch_binance_candle_open(asset: str, interval: str, open_epoch: int) -> float | None:
        """Fetch the Binance candle open price for a specific timestamp.

        Args:
            asset: "BTC", "ETH", etc.
            interval: Binance kline interval ("1h", "15m", "5m")
            open_epoch: Unix epoch of the candle start

        Returns open price as float, or None if unavailable.
        """
        pair = _ASSET_TO_PAIR.get(asset)
        if not pair:
            return None
        url = f"https://api.binance.com/api/v3/klines?symbol={pair.upper()}&interval={interval}&startTime={open_epoch * 1000}&limit=1"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                if data and len(data) > 0:
                    # Kline format: [open_time, open, high, low, close, ...]
                    return float(data[0][1])
        except Exception as e:
            logger.warning("Failed to fetch Binance candle open for %s %s: %s", asset, interval, e)
        return None

    def is_fresh(self, max_age_sec: float) -> bool:
        """True if ALL configured assets have prices younger than *max_age_sec*."""
        now = time.time()
        for asset in self._assets:
            ts = self._last_ts.get(asset)
            if ts is None or (now - ts) > max_age_sec:
                return False
        return True

    def _update_price(self, asset: str, price: Decimal) -> None:
        self._prices[asset] = price
        self._last_ts[asset] = time.time()
        if self._on_tick:
            self._on_tick(asset, price)

    def _build_ws_url(self) -> str:
        streams = "/".join(
            f"{_ASSET_TO_PAIR[a]}@trade"
            for a in self._assets
            if a in _ASSET_TO_PAIR
        )
        # Strip trailing /ws — combined stream uses /stream?streams=
        base = self._ws_base_url.rstrip("/")
        if base.endswith("/ws"):
            base = base[:-3]
        return f"{base}/stream?streams={streams}"

    def _coingecko_id(self, asset: str) -> str | None:
        idx = list(self._assets).index(asset) if asset in self._assets else -1
        if 0 <= idx < len(self._coingecko_ids):
            return self._coingecko_ids[idx]
        return None

    def _pair_to_asset(self, stream_name: str) -> str | None:
        pair = stream_name.replace("@trade", "")
        for asset, p in _ASSET_TO_PAIR.items():
            if p == pair and asset in self._assets:
                return asset
        return None

    async def _bootstrap(self) -> None:
        """Fetch initial prices from Binance REST, fallback to CoinGecko."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for asset in self._assets:
                    pair = _ASSET_TO_PAIR.get(asset)
                    if not pair:
                        continue
                    try:
                        resp = await client.get(
                            f"https://api.binance.com/api/v3/ticker/price?symbol={pair.upper()}"
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            self._update_price(asset, Decimal(data["price"]))
                    except Exception as e:
                        logger.warning("Binance REST bootstrap failed for %s: %s", asset, e)
        except Exception:
            pass

        # CoinGecko fallback for any missing
        missing = [a for a in self._assets if a not in self._prices]
        if missing:
            await self._poll_coingecko(missing)

    async def _poll_coingecko(self, assets: list[str] | None = None) -> None:
        target = assets or list(self._assets)
        ids = [self._coingecko_id(a) for a in target if self._coingecko_id(a)]
        if not ids:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": ",".join(ids), "vs_currencies": "usd"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for asset in target:
                        cg_id = self._coingecko_id(asset)
                        if cg_id and cg_id in data and "usd" in data[cg_id]:
                            self._update_price(asset, Decimal(str(data[cg_id]["usd"])))
        except Exception as e:
            logger.warning("CoinGecko poll failed: %s", e)

    async def _fallback_poll_loop(self) -> None:
        """Poll CoinGecko for assets that haven't gotten a Binance tick in >10s."""
        while self._running:
            await asyncio.sleep(self._fallback_interval_sec)
            now = time.time()
            stale = [
                a for a in self._assets
                if now - self._last_ts.get(a, 0) > 10
            ]
            if stale:
                await self._poll_coingecko(stale)

    async def run(self) -> None:
        import websockets
        self._running = True
        await self._bootstrap()

        fallback_task = asyncio.create_task(self._fallback_poll_loop())

        while self._running:
            try:
                url = self._build_ws_url()
                async with websockets.connect(url) as ws:
                    self._reconnect_count = 0
                    logger.info("Binance WS connected: %s", url)
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            stream = msg.get("stream", "")
                            asset = self._pair_to_asset(stream)
                            if asset and "data" in msg:
                                price = Decimal(msg["data"]["p"])
                                self._update_price(asset, price)
                        except Exception:
                            pass
            except Exception as e:
                if not self._running:
                    break
                delay = min(2 ** self._reconnect_count, 60.0)
                self._reconnect_count += 1
                logger.warning("Binance WS error: %s — reconnecting in %.1fs", e, delay)
                await asyncio.sleep(delay)

        fallback_task.cancel()

    async def stop(self) -> None:
        self._running = False

"""Polymarket RTDS price feeds — Chainlink resolution source + live crypto prices.

Polymarket resolves BTC Up/Down on Chainlink Data Streams (crypto_prices_chainlink btc/usd).
On connection, RTDS sends a historical data dump — we use it to look up the Chainlink price
at a specific timestamp (eventStartTime), which is the "Price to Beat" shown on Polymarket.

Subscribes to both crypto_prices_chainlink (all 4 assets) and crypto_prices (Binance).
Chainlink is the authoritative source; Binance fills in when Chainlink updates are stale.
"""

import asyncio
import bisect
import json
import logging
import random
import time
from decimal import Decimal
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
RECONNECT_BASE = 1.0
RECONNECT_MAX = 60.0
RECONNECT_429_BASE = 30.0
MAX_HISTORY_POINTS = 2000

RTDS_CHAINLINK_MAP: dict[str, str] = {
    "btc/usd": "BTC",
    "eth/usd": "ETH",
    "sol/usd": "SOL",
    "xrp/usd": "XRP",
}

RTDS_BINANCE_MAP: dict[str, str] = {
    "btcusdt": "BTC",
    "ethusdt": "ETH",
    "solusdt": "SOL",
    "xrpusdt": "XRP",
}

CHAINLINK_STALE_SEC = 15.0
RTDS_MAX_AGE_SEC = 120.0


class RTDSChainlinkPriceFeed:
    """Chainlink BTC/USD + live crypto prices from Polymarket RTDS.

    Stores the initial historical data dump so we can look up the price at a specific
    timestamp (e.g. eventStartTime = "Price to Beat" on Polymarket).
    Provides real-time crypto prices preferring Chainlink; Binance fills stale gaps.
    """

    STALE_THRESHOLD_SEC = 30.0

    def __init__(self, on_tick=None):
        self._last_price: Decimal | None = None
        self._last_ts: float = 0
        self._ws: Any = None
        self._running = False
        self._reconnect_delay = RECONNECT_BASE
        self._on_tick = on_tick
        self._history: dict[str, tuple[list[float], list[Decimal]]] = {
            "BTC": ([], []), "ETH": ([], []), "SOL": ([], []), "XRP": ([], []),
        }
        self._chainlink_prices: dict[str, Decimal] = {}
        self._chainlink_ts: dict[str, float] = {}
        self._binance_prices: dict[str, Decimal] = {}
        self._binance_ts: dict[str, float] = {}

    def get_btc_usd(self) -> Decimal | None:
        """Return latest Chainlink BTC/USD from RTDS (sync, no await)."""
        return self._last_price

    def _best_price(self, symbol: str) -> Decimal | None:
        """Return Chainlink price if fresh, else Binance price, else stale Chainlink.

        Prices older than ``RTDS_MAX_AGE_SEC`` from both sources are discarded so
        the caller never displays a frozen stale value.
        """
        now = time.time()
        cl_price = self._chainlink_prices.get(symbol)
        cl_ts = self._chainlink_ts.get(symbol, 0)
        cl_age = now - cl_ts

        bn_price = self._binance_prices.get(symbol)
        bn_ts = self._binance_ts.get(symbol, 0)
        bn_age = now - bn_ts

        if cl_price is not None and cl_age <= CHAINLINK_STALE_SEC:
            return cl_price
        if bn_price is not None and bn_age <= RTDS_MAX_AGE_SEC:
            return bn_price
        if cl_price is not None and cl_age <= RTDS_MAX_AGE_SEC:
            return cl_price
        return None

    def get_crypto_price(self, symbol: str) -> Decimal | None:
        """Return best available crypto price for a symbol."""
        return self._best_price(symbol)

    def get_all_crypto_prices(self) -> dict[str, Decimal]:
        """Return best available crypto prices for all known symbols."""
        all_syms = set(self._chainlink_prices) | set(self._binance_prices)
        result: dict[str, Decimal] = {}
        for sym in all_syms:
            p = self._best_price(sym)
            if p is not None:
                result[sym] = p
        return result

    @property
    def last_update_ts(self) -> float:
        return self._last_ts

    def is_fresh(self) -> bool:
        """True if we have any Chainlink price updated within the staleness threshold."""
        now = time.time()
        for ts in self._chainlink_ts.values():
            if (now - ts) < self.STALE_THRESHOLD_SEC:
                return True
        return False

    def price_at_timestamp(self, symbol: str, target_epoch_sec: float) -> Decimal | None:
        """Look up the Chainlink price at (or closest before) a given UNIX timestamp.

        Uses the historical data dump received on RTDS connection plus any real-time
        updates appended since. Returns None if no data covers that timestamp.
        """
        hist = self._history.get(symbol)
        if not hist:
            return None
        ts_list, val_list = hist
        if not ts_list:
            return None
        target_ms = target_epoch_sec * 1000
        idx = bisect.bisect_right(ts_list, target_ms) - 1
        if idx < 0:
            return None
        return val_list[idx]

    def _append_history(self, symbol: str, ts_ms: float, value: Decimal) -> None:
        """Append a (timestamp_ms, value) pair for a symbol, keeping sorted and bounded."""
        if symbol not in self._history:
            self._history[symbol] = ([], [])
        ts_list, val_list = self._history[symbol]
        if ts_list and ts_ms <= ts_list[-1]:
            return
        ts_list.append(ts_ms)
        val_list.append(value)
        if len(ts_list) > MAX_HISTORY_POINTS:
            self._history[symbol] = (ts_list[-MAX_HISTORY_POINTS:], val_list[-MAX_HISTORY_POINTS:])

    def _classify_symbol(self, msg: dict) -> tuple[str | None, bool]:
        """Determine the canonical symbol and whether the message is from Chainlink.

        Uses the message ``topic`` to decide the source rather than inferring it
        from the symbol format.  The ``crypto_prices`` topic occasionally sends
        slash-delimited symbols (e.g. ``btc/usd``) which would be misclassified
        as Chainlink if we relied on format alone.
        """
        payload = msg.get("payload") or {}
        sym_raw: str = payload.get("symbol") or ""
        if not sym_raw:
            return None, False

        topic: str = msg.get("topic") or ""
        is_chainlink = topic == "crypto_prices_chainlink"

        # Try the Chainlink map first (slash-delimited like "btc/usd")
        mapped = RTDS_CHAINLINK_MAP.get(sym_raw)
        if mapped is not None:
            return mapped, is_chainlink

        # Try the Binance map (concatenated like "btcusdt")
        binance_key = sym_raw.lower().replace("/", "")
        mapped = RTDS_BINANCE_MAP.get(binance_key)
        if mapped is not None:
            return mapped, is_chainlink

        logger.debug("rtds_unmapped_symbol: %s (topic=%s)", sym_raw, topic)
        return None, False

    def _process_message(self, msg: dict) -> None:
        """Handle Chainlink + Binance data dumps and real-time updates."""
        payload = msg.get("payload") or {}
        sym_raw: str = payload.get("symbol") or ""
        mapped_sym, is_chainlink = self._classify_symbol(msg)

        # Initial data dump (both Chainlink and Binance send these)
        if isinstance(payload.get("data"), list) and mapped_sym:
            count = 0
            last_val = None
            for pt in payload["data"]:
                ts_ms = pt.get("timestamp") or pt.get("t")
                val = pt.get("value") or pt.get("v")
                if ts_ms is not None and val is not None:
                    last_val = Decimal(str(val))
                    if is_chainlink:
                        self._append_history(mapped_sym, float(ts_ms), last_val)
                    count += 1
            if count > 0 and last_val is not None:
                if is_chainlink:
                    self._chainlink_prices[mapped_sym] = last_val
                    self._chainlink_ts[mapped_sym] = time.time()
                else:
                    self._binance_prices[mapped_sym] = last_val
                    self._binance_ts[mapped_sym] = time.time()
                if is_chainlink and mapped_sym == "BTC":
                    self._last_price = last_val
                    self._last_ts = time.time()
                logger.info("rtds_history_loaded", extra={
                    "symbol": mapped_sym, "points": count,
                    "source": "chainlink" if is_chainlink else "binance",
                })
            return

        # Real-time update
        val = payload.get("value")
        if val is None or not mapped_sym:
            return

        try:
            price = Decimal(str(val))
        except Exception:
            return

        if is_chainlink:
            self._chainlink_prices[mapped_sym] = price
            self._chainlink_ts[mapped_sym] = time.time()
        else:
            self._binance_prices[mapped_sym] = price
            self._binance_ts[mapped_sym] = time.time()

        # Fire on_tick callback for data recording
        if self._on_tick:
            try:
                source = "chainlink" if is_chainlink else "rtds_binance"
                self._on_tick(mapped_sym, float(price), source)
            except Exception:
                pass

        if is_chainlink:
            ts_ms = payload.get("timestamp")
            if ts_ms is not None:
                self._append_history(mapped_sym, float(ts_ms), price)
            if mapped_sym == "BTC":
                self._last_price = price
                self._last_ts = time.time()

    async def _ping_loop(self) -> None:
        """Send text PING every 5 sec per RTDS protocol to keep connection alive."""
        while self._running:
            await asyncio.sleep(5)
            if not self._running:
                break
            if self._ws is not None:
                try:
                    await self._ws.send("PING")
                except Exception:
                    break

    async def run(self) -> None:
        """Connect to RTDS, subscribe to Chainlink + Binance for all assets."""
        self._running = True
        logger.info("rtds_starting")
        while self._running:
            try:
                logger.info("rtds_connecting", extra={"delay_was": self._reconnect_delay})
                self._ws = await websockets.connect(
                    RTDS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                )
                self._reconnect_delay = RECONNECT_BASE
                # Unfiltered subs for real-time Chainlink + Binance prices.
                # Filtered Chainlink subs don't deliver real-time updates or
                # historical dumps reliably, so we use unfiltered for both topics.
                for topic in ("crypto_prices_chainlink", "crypto_prices"):
                    await self._ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{"topic": topic, "type": "*", "filters": ""}],
                    }))
                logger.info("rtds_connected")
                ping_task = asyncio.create_task(self._ping_loop())
                try:
                    async for raw in self._ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            self._process_message(msg)
                        except (json.JSONDecodeError, TypeError, KeyError):
                            pass
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
            except ConnectionClosed as e:
                logger.info("rtds_closed", extra={"code": e.code, "reason": str(e.reason)})
            except Exception as e:
                is_429 = "429" in str(e)
                if is_429:
                    self._reconnect_delay = max(self._reconnect_delay, RECONNECT_429_BASE)
                logger.warning("rtds_error", extra={
                    "error": str(e), "type": type(e).__name__,
                    "next_retry_sec": round(self._reconnect_delay, 1),
                })
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None
            if not self._running:
                break
            jitter = random.uniform(0.5, 1.5)
            await asyncio.sleep(self._reconnect_delay * jitter)
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass
            self._ws = None

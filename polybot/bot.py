"""Main bot: wires ladder manager, position manager, risk manager, and order executor.

All synchronous CLOB client calls (order placement, book queries) are dispatched
via asyncio.to_thread() so the Binance WebSocket feed is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

import websockets

from polybot.config import BotConfig
from polybot.heartbeat import Heartbeat
from polybot.ladder_manager import LadderManager
from polybot.market_discovery import discover_active_markets
from polybot.order_executor import OrderExecutor
from polybot.order_tracker import OrderTracker
from polybot.position_manager import PositionManager
from polybot.redeemer import Redeemer
from polybot.risk_manager import RiskManager
from polybot.tick_size_cache import TickSizeCache
from polybot.types import MarketWindow, Side

logger = logging.getLogger(__name__)

# How often to print a status summary (seconds)
STATUS_INTERVAL_SEC = 30


class Bot:
    def __init__(self, cfg: BotConfig, clob_client, initial_bankroll: float):
        self.cfg = cfg
        self.clob_client = clob_client
        self.position_manager = PositionManager(cfg, bankroll=initial_bankroll)
        self.risk_manager = RiskManager(cfg, starting_bankroll=initial_bankroll)
        self.order_executor = OrderExecutor(cfg, clob_client=clob_client)
        self.order_tracker = OrderTracker()
        self.tick_size_cache = TickSizeCache(clob_client, ttl_sec=cfg.tick_size_ttl_sec)
        self.ladder_manager = LadderManager(
            cfg,
            order_executor=self.order_executor,
            order_tracker=self.order_tracker,
            position_manager=self.position_manager,
            risk_manager=self.risk_manager,
            tick_size_cache=self.tick_size_cache,
        )

        self._expired_market_cache: dict[str, MarketWindow] = {}
        self.spot_prices: dict[str, float] = {}
        self.window_open_prices: dict[str, float] = {}
        self.active_markets: list[MarketWindow] = []
        self._snapped_windows: set[str] = set()
        self._cancel_only_mode = False
        self.heartbeat = None  # set in run()
        self._last_status_time: float = 0.0
        self._trade_count: int = 0
        self._start_time: float = time.time()
        self._activity_log: deque = deque(maxlen=20)

    def compute_spot_delta(self, asset: str) -> float:
        current = self.spot_prices.get(asset, 0.0)
        open_price = self.window_open_prices.get(asset, 0.0)
        if open_price <= 0:
            return 0.0
        return (current - open_price) / open_price

    def _snapshot_window_open_prices(self):
        """Capture spot prices at the start of each new market window."""
        for market in self.active_markets:
            if market.market_id not in self._snapped_windows:
                spot = self.spot_prices.get(market.asset, 0.0)
                if spot > 0:
                    self.window_open_prices[market.asset] = spot
                    self._snapped_windows.add(market.market_id)
                    logger.info(
                        "SNAPSHOT: %s open price = $%.2f for window %s",
                        market.asset, spot, market.market_id,
                    )

    def _cleanup_expired_windows(self, now_epoch: int):
        """Remove window IDs and stale ladders no longer in the active market list."""
        active_ids = {m.market_id for m in self.active_markets}
        stale = self._snapped_windows - active_ids
        for mid in stale:
            self._snapped_windows.discard(mid)

        # Clean up ladders for markets that are no longer active
        stale_ladders = set(self.ladder_manager.ladders.keys()) - active_ids
        for mid in stale_ladders:
            if mid not in self.position_manager.get_pending_settlements():
                self.ladder_manager.cleanup_ladder(mid)

    def _find_market(self, market_id: str) -> MarketWindow | None:
        for m in self.active_markets:
            if m.market_id == market_id:
                return m
        return self._expired_market_cache.get(market_id)

    async def _redeem_tokens(self, condition_id: str, token_ids: list[str]) -> float:
        """Redeem winning tokens on-chain. Returns USDC.e received."""
        # TODO: Implement actual on-chain redemption via web3/polygon RPC
        logger.info("Redemption requested for %s (TODO: on-chain call)", condition_id)
        return 0.0

    def _log_status(self, now_epoch: int):
        """Print a periodic status summary."""
        if now_epoch - self._last_status_time < STATUS_INTERVAL_SEC:
            return
        self._last_status_time = now_epoch

        spot_parts = []
        for asset in self.cfg.assets:
            price = self.spot_prices.get(asset, 0.0)
            if price > 0:
                delta = self.compute_spot_delta(asset)
                spot_parts.append(f"{asset}=${price:,.2f}({delta:+.3%})")
        spot_str = " | ".join(spot_parts) if spot_parts else "waiting for prices..."

        pos_count = self.position_manager.active_position_count()
        ladder_count = len(self.ladder_manager.ladders)

        logger.info(
            "STATUS | bankroll=$%.2f | pnl=$%.2f | positions=%d | ladders=%d | trades=%d | %s",
            self.position_manager.bankroll,
            self.risk_manager.daily_pnl,
            pos_count,
            ladder_count,
            self._trade_count,
            spot_str,
        )

    def _record_activity(self, event_type: str, asset: str, detail: str, pnl: float | None = None):
        from polybot.types import ActivityEvent
        self._activity_log.append(ActivityEvent(
            timestamp=time.time(),
            event_type=event_type,
            asset=asset,
            detail=detail,
            pnl=pnl,
        ))

    def _settle_expired_windows(self, now_epoch: int):
        for market in list(self.active_markets):
            if market.is_active(now_epoch):
                continue
            mid = market.market_id
            pos = self.position_manager.positions.get(mid)
            if pos is None:
                continue
            if mid in self.position_manager.get_pending_settlements():
                continue

            # Cancel any remaining orders on exchange
            self.ladder_manager.cancel_ladder(mid)
            self.ladder_manager.cleanup_ladder(mid)

            # Clean up window state
            self._snapped_windows.discard(mid)

            # Mark for async settlement
            self.position_manager.mark_pending_settlement(mid)
            self._expired_market_cache[mid] = market
            logger.info("Window expired for %s — pending settlement", mid)

    def _on_connection_lost(self):
        logger.warning("Connection lost — resetting all state")
        for mid in list(self.ladder_manager.ladders.keys()):
            self.ladder_manager.cleanup_ladder(mid)
        self.order_tracker.mark_all_unknown()
        self._cancel_only_mode = False

    async def run_binance_ws(self):
        """Connect to Binance combined stream for real-time spot prices."""
        streams = [f"{a.lower()}usdt@ticker" for a in self.cfg.assets]
        base = self.cfg.binance_ws_url.replace("/ws", "/stream")
        url = f"{base}?streams={'/'.join(streams)}"

        while True:
            try:
                async with websockets.connect(url) as ws:
                    logger.info("Binance WS connected: %s", url)
                    async for msg in ws:
                        data = json.loads(msg)
                        payload = data.get("data", data)
                        if "s" in payload and "c" in payload:
                            symbol = payload["s"].replace("USDT", "")
                            self.spot_prices[symbol] = float(payload["c"])
            except Exception as e:
                logger.warning("Binance WS error: %s, reconnecting in 5s", e)
                await asyncio.sleep(5)

    async def run_market_discovery(self):
        """Periodically discover active crypto up/down markets."""
        while True:
            try:
                new_markets = await discover_active_markets(
                    self.clob_client, self.cfg.assets
                )
                old_ids = {m.market_id for m in self.active_markets}
                new_ids = {m.market_id for m in new_markets}
                arrived = new_ids - old_ids
                departed = old_ids - new_ids
                if arrived:
                    logger.info("NEW WINDOWS: %s", ", ".join(arrived))
                if departed:
                    logger.info("EXPIRED WINDOWS: %s", ", ".join(departed))

                self.active_markets = new_markets
                logger.info("Discovered %d active markets", len(self.active_markets))
            except Exception as e:
                logger.error("Market discovery error: %s", e)
            await asyncio.sleep(self.cfg.market_discovery_interval_sec)

    async def run_trading_loop(self):
        """Main trading loop: manage ladders on all active markets."""
        while True:
            # Heartbeat health gate
            if self.heartbeat and not self.heartbeat.is_healthy():
                await asyncio.sleep(self.cfg.poll_interval_ms / 1000.0)
                continue

            now = int(time.time())

            # Snapshot open prices for new windows
            self._snapshot_window_open_prices()

            # Status logging
            self._log_status(now)

            # Tick the mock client for fill simulation (no-op for real CLOB)
            if hasattr(self.clob_client, 'tick'):
                self.clob_client.tick()

            # Build market lookup
            market_map = {m.market_id: m for m in self.active_markets}

            if not self._cancel_only_mode:
                # 1. Post ladders on new markets
                for market in self.active_markets:
                    if not market.is_active(now):
                        continue
                    if self.ladder_manager.has_ladder(market.market_id):
                        continue
                    elapsed_pct = market.elapsed(now) / market.timeframe_sec
                    if elapsed_pct >= 0.10:
                        count = await asyncio.to_thread(
                            self.ladder_manager.post_ladder, market
                        )
                        if count > 0:
                            self._record_activity(
                                "LADDER", market.asset,
                                f"posted {count} rungs on {market.market_id}",
                            )

                # 2. Reprice if book moved
                await asyncio.to_thread(
                    self.ladder_manager.reprice_if_needed, market_map
                )

            # 3. Check fills on all active ladders
            fills = await asyncio.to_thread(self.ladder_manager.check_fills)
            if fills > 0:
                self._trade_count += fills
                # Record fill activity for the most recent fills
                for market in self.active_markets:
                    mid = market.market_id
                    stats = self.ladder_manager.get_ladder_stats(mid)
                    if stats["up_filled"] > 0 or stats["dn_filled"] > 0:
                        combined = stats["combined_vwap"]
                        if combined > 0:
                            self._record_activity(
                                "FILL", market.asset,
                                f"UP:{stats['up_filled']:.0f}@${stats['up_vwap']:.2f} "
                                f"DN:{stats['dn_filled']:.0f}@${stats['dn_vwap']:.2f} "
                                f"combined=${combined:.3f}",
                            )

            # 4. Imbalance guard
            self.ladder_manager.check_imbalance(now)

            # 5. Cancel rungs on expiring windows
            for market in self.active_markets:
                if market.is_active(now) and market.remaining(now) < self.cfg.no_trade_final_sec:
                    if self.ladder_manager.has_ladder(market.market_id):
                        cancelled = self.ladder_manager.cancel_ladder(market.market_id)
                        if cancelled > 0:
                            self._record_activity(
                                "CANCEL", market.asset,
                                f"cancelled {cancelled} unfilled rungs (window expiring)",
                            )

            # 6. Settlement
            self._settle_expired_windows(now)

            # 7. Cleanup expired window snapshots
            self._cleanup_expired_windows(now)

            await asyncio.sleep(self.cfg.poll_interval_ms / 1000.0)

    async def run_web_server(self):
        """Start the FastAPI web dashboard."""
        import uvicorn
        from polybot.web.server import create_app

        app = create_app(self)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.cfg.web_port,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_server.install_signal_handlers = lambda: None

        broadcast_task = asyncio.create_task(app._broadcast_loop())
        balance_task = asyncio.create_task(self._poll_wallet_balance())
        try:
            logger.info("Web dashboard at http://127.0.0.1:%d", self.cfg.web_port)
            await self._uvicorn_server.serve()
        except Exception as e:
            logger.warning("Web server stopped: %s", e)
        finally:
            broadcast_task.cancel()
            balance_task.cancel()

    async def _poll_wallet_balance(self):
        """Poll wallet USDC balance every 60s. Stores in _wallet_balance."""
        while True:
            try:
                if not self.cfg.dry_run:
                    result = await asyncio.to_thread(
                        self.clob_client.get_balance_allowance
                    )
                    self._wallet_balance = float(result.get("balance", 0)) / 1e6
                else:
                    self._wallet_balance = self.position_manager.bankroll
            except Exception as e:
                logger.debug("Balance poll failed: %s", e)
            await asyncio.sleep(60)

    def _settle_position(self, mid: str, market: MarketWindow, outcome: str):
        """Settle a single position: compute PnL, update risk, queue redemption."""
        pos = self.position_manager.positions.get(mid)
        if pos:
            if outcome in ("UP", "YES"):
                pnl = pos.profit_if_up()
            else:
                pnl = pos.profit_if_down()

            logger.info("Settled %s: %s, PnL=$%.2f", mid, outcome, pnl)
            self.risk_manager.update_pnl(pnl)
            self._record_activity("SETTLE", market.asset, f"{outcome} PnL=${pnl:.2f}", pnl=pnl)

            self.redeemer.queue_redemption(
                market.condition_id,
                [market.up_token_id, market.dn_token_id],
            )

        self.position_manager.complete_settlement(mid)
        self.position_manager.remove_position(mid)
        self._expired_market_cache.pop(mid, None)

    def _dry_run_resolve(self, market: MarketWindow) -> str | None:
        """In dry-run mode, resolve using the last known spot delta."""
        delta = self.compute_spot_delta(market.asset)
        if delta > 0:
            return "UP"
        elif delta < 0:
            return "DOWN"
        # If delta is exactly 0, flip a coin
        import random
        return random.choice(["UP", "DOWN"])

    async def run_settlement_poller(self):
        """Poll pending settlements for resolution."""
        import httpx
        from polybot.settlement import try_resolve_once

        async with httpx.AsyncClient() as client:
            while True:
                for mid in list(self.position_manager.get_pending_settlements()):
                    market = self._find_market(mid)
                    if market is None:
                        continue

                    # Dry-run: resolve immediately using spot delta
                    if self.cfg.dry_run:
                        outcome = self._dry_run_resolve(market)
                        if outcome:
                            self._settle_position(mid, market, outcome)
                        continue

                    # Live mode: poll Polymarket for resolution
                    now = time.time()
                    elapsed = now - market.close_epoch
                    if elapsed > self.cfg.bot_settlement_give_up_sec:
                        logger.error("Settlement timeout for %s after %.0fs", mid, elapsed)
                        self.position_manager.mark_failed_settlement(mid)
                        continue

                    result = await try_resolve_once(
                        client, self.cfg.polymarket_host,
                        mid, market.condition_id,
                    )

                    if result is not None:
                        self._settle_position(mid, market, result["outcome"])

                await asyncio.sleep(30)

    async def run_daily_reset(self):
        """Reset daily PnL at midnight UTC."""
        from datetime import datetime, timezone, timedelta
        while True:
            now = datetime.now(timezone.utc)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            seconds_until_midnight = (tomorrow - now).total_seconds()
            await asyncio.sleep(seconds_until_midnight)
            self.risk_manager.reset_daily()

    async def run(self):
        """Start all concurrent tasks."""
        logger.info(
            "Bot starting — bankroll: $%.2f, assets: %s, ladder: %d rungs @ $%.2f spacing",
            self.position_manager.bankroll, self.cfg.assets,
            self.cfg.ladder_rungs, self.cfg.ladder_spacing,
        )
        self.heartbeat = Heartbeat(
            interval_sec=self.cfg.heartbeat_interval_sec,
            max_failures=self.cfg.heartbeat_max_failures,
        )
        self.redeemer = Redeemer(
            max_retries=self.cfg.redemption_retry_max,
            backoff_sec=self.cfg.redemption_retry_backoff_sec,
        )
        tasks = [
            asyncio.create_task(self.run_binance_ws()),
            asyncio.create_task(self.run_market_discovery()),
            asyncio.create_task(self.run_trading_loop()),
            asyncio.create_task(self.run_web_server()),
            asyncio.create_task(self.heartbeat.run(self.clob_client, self._on_connection_lost)),
            asyncio.create_task(self.run_settlement_poller()),
            asyncio.create_task(self.redeemer.run(self._redeem_tokens)),
            asyncio.create_task(self.run_daily_reset()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        finally:
            if hasattr(self, '_uvicorn_server'):
                self._uvicorn_server.should_exit = True
            self.order_executor.cancel_all()
            for t in tasks:
                t.cancel()

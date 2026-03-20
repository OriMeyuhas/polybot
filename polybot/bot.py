"""Central bot orchestrator: wires data layer, OMS, strategy, and web UI.

All synchronous CLOB client calls (order placement, book queries) are dispatched
via asyncio.to_thread() so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque

from polybot.config import BotConfig
from polybot.data.price_feed import MultiAssetPriceFeed
from polybot.data.book_manager import BookManager
from polybot.data.market_ws import MarketWSClient
from polybot.data.clob_midpoints import ClobMidpointPoller
from polybot.data.gamma import discover_crypto_updown_markets, to_market_window
from polybot.oms.clob_client import create_clob_client
from polybot.oms.order_executor import OrderExecutor
from polybot.oms.heartbeat import Heartbeat
from polybot.strategy.ladder_manager import LadderManager
from polybot.strategy.order_tracker import OrderTracker
from polybot.strategy.position_manager import PositionManager
from polybot.strategy.risk_stub import RiskStub
from polybot.tick_size_cache import TickSizeCache
from polybot.redeemer import Redeemer
from polybot.errors import ClobApiError
from polybot.types import MarketWindow, Side, ActivityEvent
from polybot.web.state import GuiStateHolder

logger = logging.getLogger(__name__)

# How often to print a status summary (seconds)
STATUS_INTERVAL_SEC = 30


class Bot:
    """Central coordinator: owns all subsystems and runs the main trading loop."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.running = False
        self.mode = "dry_run" if cfg.dry_run else "live"

        # Data layer
        self.price_feed = MultiAssetPriceFeed(
            assets=cfg.assets,
            coingecko_ids=cfg.coingecko_ids,
            ws_base_url=cfg.binance_ws_url,
            fallback_interval_sec=cfg.binance_fallback_interval_sec,
        )
        self.book_manager = BookManager()
        self.market_ws = MarketWSClient(
            url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
            on_message=self.book_manager.process_message,
            ping_interval_sec=cfg.market_ws_ping_sec,
        )
        self.midpoint_poller = ClobMidpointPoller()

        # OMS
        self.clob_client = create_clob_client(cfg, book_manager=self.book_manager)
        self.order_executor = OrderExecutor(cfg, self.clob_client)
        self.heartbeat = Heartbeat(cfg.heartbeat_interval_sec, cfg.heartbeat_max_failures)

        # Strategy
        self.order_tracker = OrderTracker()
        self.position_manager = PositionManager(cfg, bankroll=cfg.bankroll)
        self.risk = RiskStub()
        self.tick_cache = TickSizeCache(self.clob_client, cfg.tick_size_ttl_sec)
        self.ladder_manager = LadderManager(
            cfg,
            order_executor=self.order_executor,
            order_tracker=self.order_tracker,
            position_manager=self.position_manager,
            risk_manager=self.risk,
            tick_size_cache=self.tick_cache,
        )

        # Web UI
        self.gui_state = GuiStateHolder()

        # Redeemer
        self.redeemer = Redeemer(
            max_retries=cfg.redemption_retry_max,
            backoff_sec=cfg.redemption_retry_backoff_sec,
        )

        # Internal state
        self._active_markets: dict[str, MarketWindow] = {}
        self._expired_market_cache: dict[str, MarketWindow] = {}
        self._start_time = 0.0
        self._trade_count = 0
        self._total_pnl = 0.0
        self._realized_pnl = 0.0
        self._cancel_only_mode = cfg.start_paused
        self._prev_cancel_only = cfg.start_paused
        self._pending_cancel_all = False
        self._last_discovery_time = 0.0
        self._last_status_time = 0.0
        self._snapped_windows: set[str] = set()
        self.spot_prices: dict[str, float] = {}
        self.window_open_prices: dict[str, float] = {}
        self._activity_log: deque = deque(maxlen=20)
        self._tasks: list[asyncio.Task] = []
        self._wallet_balance: float = cfg.bankroll
        self._wallet_address: str | None = self._derive_wallet_address(cfg)

    @staticmethod
    def _derive_wallet_address(cfg: BotConfig) -> str | None:
        """Derive wallet address from private key without exposing key material."""
        if cfg.dry_run or not cfg.private_key:
            return None
        try:
            from eth_account import Account
            acct = Account.from_key(cfg.private_key)
            return acct.address
        except Exception:
            # If eth_account not installed, return a safe placeholder
            return "live-wallet"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start all subsystems."""
        self.running = True
        self._start_time = time.time()
        self.gui_state.update(running=True, mode=self.mode)
        logger.info("Bot started in %s mode", self.mode)

    async def stop(self):
        """Graceful shutdown."""
        self.running = False

        # Cancel all resting orders in live mode
        if not self.cfg.dry_run:
            try:
                self.order_executor.cancel_all()
            except Exception as e:
                logger.error("Error cancelling orders on shutdown: %s", e)

        # Stop subsystems
        await self.price_feed.stop()
        await self.midpoint_poller.stop()
        await self.market_ws.stop()
        await self.heartbeat.stop()

        # Cancel async tasks
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        self.gui_state.update(running=False)
        logger.info(
            "Bot stopped — trades: %d, pnl: $%.2f, runtime: %ds",
            self._trade_count,
            self._realized_pnl,
            int(time.time() - self._start_time) if self._start_time else 0,
        )

    async def run(self):
        """Main entry point — start all concurrent tasks."""
        logger.info(
            "Bot starting — bankroll: $%.2f, assets: %s",
            self.position_manager.bankroll,
            self.cfg.assets,
        )
        await self.start()
        tasks = [
            asyncio.create_task(self.price_feed.run()),
            asyncio.create_task(
                self.midpoint_poller.run(
                    self.cfg.polymarket_host, self.cfg.clob_midpoint_poll_sec
                )
            ),
            asyncio.create_task(self.market_ws.run([])),
            asyncio.create_task(
                self.heartbeat.run(self.clob_client, self._on_connection_lost)
            ),
            asyncio.create_task(self._run_trading_loop()),
            asyncio.create_task(self._run_settlement_poller()),
            asyncio.create_task(self.redeemer.run(self._redeem_tokens)),
            asyncio.create_task(self._run_daily_reset()),
            asyncio.create_task(self._poll_wallet_balance()),
        ]
        self._tasks = tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Web UI hooks
    # ------------------------------------------------------------------

    async def ui_start(self):
        """Called from web UI POST /api/start."""
        self._cancel_only_mode = False
        logger.info("Trading resumed via UI")

    async def ui_stop(self):
        """Called from web UI POST /api/stop."""
        self._cancel_only_mode = True
        self._pending_cancel_all = True
        logger.info("Trading paused via UI")

    # ------------------------------------------------------------------
    # Connection lost callback
    # ------------------------------------------------------------------

    def _on_connection_lost(self):
        logger.warning("Connection lost — resetting all state")
        for mid in list(self.ladder_manager.ladders.keys()):
            self.ladder_manager.cleanup_ladder(mid)
        self.order_tracker.mark_all_unknown()

    # ------------------------------------------------------------------
    # Main trading loop (500ms)
    # ------------------------------------------------------------------

    async def _run_trading_loop(self):
        """Main trading loop: manage ladders on all active markets."""
        while self.running:
            try:
                await self._trading_loop_tick()
            except ClobApiError as e:
                if e.status_code == 429:
                    backoff = e.retry_after or 5.0
                    logger.warning("Rate limited (429) — backing off %.1fs", backoff)
                    await asyncio.sleep(backoff)
                    continue
                elif e.cancel_only:
                    logger.warning("Exchange in cancel-only mode — pausing trading")
                    self._cancel_only_mode = True
                else:
                    logger.error("CLOB API error in trading loop: %s", e)
            except Exception as e:
                logger.error("Unexpected error in trading loop: %s", e, exc_info=True)

            await asyncio.sleep(self.cfg.poll_interval_ms / 1000.0)

    async def _trading_loop_tick(self):
        """Single iteration of the trading loop."""
        # Heartbeat health gate
        if not self.heartbeat.is_healthy():
            return

        # Handle pending cancel-all from /api/stop
        if self._pending_cancel_all:
            await asyncio.to_thread(self.ladder_manager.cancel_all_ladders)
            self._pending_cancel_all = False

        # Detect resume transition (stopped -> running)
        if self._prev_cancel_only and not self._cancel_only_mode:
            self.ladder_manager.clear_cancelled_ladders()
        self._prev_cancel_only = self._cancel_only_mode

        now = int(time.time())

        # Snapshot open prices for new windows
        self._snapshot_window_open_prices()

        # Update spot prices from price feed
        self._update_spot_prices()

        # Status logging
        self._log_status(now)

        # Paper fill simulation
        if self.cfg.dry_run and hasattr(self.clob_client, "tick"):
            self.clob_client.tick()

        # Market discovery (every N seconds)
        if time.time() - self._last_discovery_time > self.cfg.market_discovery_interval_sec:
            await self._discover_markets()

        # Build market lookup
        active_list = list(self._active_markets.values())
        market_map = {m.market_id: m for m in active_list}

        if not self._cancel_only_mode:
            # Post ladders on new markets
            for market in active_list:
                if not market.is_active(now):
                    continue
                if self.ladder_manager.has_ladder(market.market_id):
                    continue
                elapsed_pct = (
                    market.elapsed(now) / market.timeframe_sec
                    if market.timeframe_sec > 0
                    else 1.0
                )
                if elapsed_pct >= 0.10:
                    count = await asyncio.to_thread(
                        self.ladder_manager.post_ladder, market
                    )
                    if count > 0:
                        self._record_activity(
                            "LADDER",
                            market.asset,
                            f"posted {count} rungs on {market.market_id}",
                        )

            # Reprice if book moved
            await asyncio.to_thread(
                self.ladder_manager.reprice_if_needed, market_map
            )

        # Check fills on all active ladders
        filled_orders = await asyncio.to_thread(self.ladder_manager.check_fills)
        if filled_orders:
            self._trade_count += len(filled_orders)
            # Group new fills by market for activity log
            fills_by_market: dict[str, list] = {}
            for order in filled_orders:
                fills_by_market.setdefault(order.market_id, []).append(order)
            for mid, orders in fills_by_market.items():
                market = market_map.get(mid)
                if not market:
                    continue
                up_qty = sum(o.size for o in orders if o.side == Side.UP)
                dn_qty = sum(o.size for o in orders if o.side == Side.DOWN)
                up_cost = sum(o.size * o.price for o in orders if o.side == Side.UP)
                dn_cost = sum(o.size * o.price for o in orders if o.side == Side.DOWN)
                parts = []
                if up_qty > 0:
                    parts.append(f"UP:{up_qty:.0f}@${up_cost / up_qty:.2f}")
                if dn_qty > 0:
                    parts.append(f"DN:{dn_qty:.0f}@${dn_cost / dn_qty:.2f}")
                if up_qty > 0 and dn_qty > 0:
                    parts.append(
                        f"combined=${up_cost / up_qty + dn_cost / dn_qty:.3f}"
                    )
                self._record_activity("FILL", market.asset, " ".join(parts))

        # Imbalance guard
        await asyncio.to_thread(self.ladder_manager.check_imbalance, now)

        # Cancel rungs on expiring windows
        for market in active_list:
            if (
                market.is_active(now)
                and market.remaining(now) < self.cfg.no_trade_final_sec
            ):
                if self.ladder_manager.has_ladder(market.market_id):
                    cancelled = await asyncio.to_thread(
                        self.ladder_manager.cancel_ladder, market.market_id
                    )
                    if cancelled > 0:
                        self._record_activity(
                            "CANCEL",
                            market.asset,
                            f"cancelled {cancelled} unfilled rungs (window expiring)",
                        )

        # Settlement
        await self._settle_expired_windows(now)

        # Cleanup expired window snapshots
        self._cleanup_expired_windows(now)

        # Update GUI
        self.gui_state.update(**self.build_state_snapshot())

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def _discover_markets(self):
        """Discover active crypto up/down markets via Gamma API."""
        try:
            # Build slug patterns from enabled assets
            patterns = []
            for asset in self.cfg.assets:
                a = asset.lower()
                patterns.append(f"{a}-updown-5m-")
                patterns.append(f"{a}-updown-15m-")
            results = await discover_crypto_updown_markets(
                slug_patterns=patterns,
            )
            new_markets: dict[str, MarketWindow] = {}
            all_token_ids: list[str] = []
            for info, asset in results:
                mw = to_market_window(info, asset)
                new_markets[mw.market_id] = mw
                all_token_ids.extend([mw.up_token_id, mw.dn_token_id])

            if not new_markets and self._active_markets:
                logger.error(
                    "Discovery returned 0 markets — preserving %d existing markets",
                    len(self._active_markets),
                )
            else:
                old_ids = set(self._active_markets.keys())
                new_ids = set(new_markets.keys())
                arrived = new_ids - old_ids
                departed = old_ids - new_ids
                if arrived:
                    logger.info("NEW WINDOWS: %s", ", ".join(arrived))
                if departed:
                    logger.info("EXPIRED WINDOWS: %s", ", ".join(departed))
                self._active_markets = new_markets

            # Update subscriptions
            self.midpoint_poller.register_tokens(all_token_ids)
            self.market_ws.update_subscriptions(all_token_ids)
            self.book_manager.update_assets(all_token_ids)

            self._last_discovery_time = time.time()
            logger.info("Discovered %d active markets", len(self._active_markets))
        except Exception as e:
            logger.error("Market discovery failed: %s", e)

    # ------------------------------------------------------------------
    # Spot price sync
    # ------------------------------------------------------------------

    def _update_spot_prices(self):
        """Sync spot prices from the price feed into self.spot_prices."""
        for asset in self.cfg.assets:
            price = self.price_feed.get_price(asset)
            if price is not None:
                self.spot_prices[asset] = float(price)

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def _settle_expired_windows(self, now_epoch: int):
        """Mark expired windows for settlement."""
        for market in list(self._active_markets.values()):
            if market.is_active(now_epoch):
                continue
            mid = market.market_id
            pos = self.position_manager.positions.get(mid)
            if pos is None:
                continue
            if mid in self.position_manager.get_pending_settlements():
                continue

            # Cancel any remaining orders on exchange
            await asyncio.to_thread(self.ladder_manager.cancel_ladder, mid)
            self.ladder_manager.cleanup_ladder(mid)

            # Clean up window state
            self._snapped_windows.discard(mid)

            # Mark for async settlement
            self.position_manager.mark_pending_settlement(mid)
            self._expired_market_cache[mid] = market
            logger.info("Window expired for %s — pending settlement", mid)

    def _settle_position(self, mid: str, market: MarketWindow, outcome: str):
        """Settle a single position: compute PnL, update risk/bankroll, queue redemption."""
        pos = self.position_manager.positions.get(mid)
        if pos:
            if outcome in ("UP", "YES"):
                pnl = pos.profit_if_up()
                winning = pos.up_qty - pos.up_cost if pos.up_qty > 0 else 0.0
                losing = pos.dn_cost
            else:
                pnl = pos.profit_if_down()
                winning = pos.dn_qty - pos.dn_cost if pos.dn_qty > 0 else 0.0
                losing = pos.up_cost

            logger.info("Settled %s: %s, PnL=$%.2f", mid, outcome, pnl)
            self.risk.update_pnl(pnl)
            self._realized_pnl += pnl
            self.position_manager.bankroll += pnl

            if losing > 0:
                detail = f"{outcome} won \u2192 \u2191 +${winning:.2f} \u2193 -${losing:.2f} = net ${pnl:+.2f}"
            else:
                detail = f"{outcome} won \u2192 \u2191 +${winning:.2f} = net ${pnl:+.2f}"
            self._record_activity("SETTLE", market.asset, detail, pnl=pnl)

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
        return random.choice(["UP", "DOWN"])

    async def _redeem_tokens(self, condition_id: str, token_ids: list[str]) -> float:
        """Redeem winning tokens on-chain. Returns USDC.e received."""
        # TODO: Implement actual on-chain redemption via web3/polygon RPC
        logger.info("Redemption requested for %s (TODO: on-chain call)", condition_id)
        return 0.0

    async def _run_settlement_poller(self):
        """Poll pending settlements for resolution (every 30s)."""
        import httpx
        from polybot.settlement import try_resolve_once

        async with httpx.AsyncClient() as client:
            while self.running:
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
                        logger.error(
                            "Settlement timeout for %s after %.0fs", mid, elapsed
                        )
                        self.position_manager.mark_failed_settlement(mid)
                        continue

                    result = await try_resolve_once(
                        client,
                        self.cfg.polymarket_host,
                        mid,
                        market.condition_id,
                    )

                    if result is not None:
                        self._settle_position(mid, market, result["outcome"])

                await asyncio.sleep(30)

    async def _run_daily_reset(self):
        """Reset daily PnL at midnight UTC."""
        from datetime import datetime, timezone, timedelta

        while self.running:
            now = datetime.now(timezone.utc)
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            seconds_until_midnight = (tomorrow - now).total_seconds()
            await asyncio.sleep(seconds_until_midnight)
            self.risk.reset_daily()
            self._realized_pnl = 0.0
            logger.info("Daily PnL reset")

    # ------------------------------------------------------------------
    # Wallet balance polling
    # ------------------------------------------------------------------

    async def _poll_wallet_balance(self):
        """Poll wallet USDC balance every 60s. Stores in _wallet_balance."""
        while self.running:
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_unrealized_pnl(self) -> float:
        """Compute unrealized PnL across all open positions. Returns 0.0 for now."""
        return 0.0

    def compute_spot_delta(self, asset: str) -> float:
        current = self.spot_prices.get(asset, 0.0)
        open_price = self.window_open_prices.get(asset, 0.0)
        if open_price <= 0:
            return 0.0
        return (current - open_price) / open_price

    def _snapshot_window_open_prices(self):
        """Capture spot prices at the start of each new market window."""
        for market in self._active_markets.values():
            if market.market_id not in self._snapped_windows:
                spot = self.spot_prices.get(market.asset, 0.0)
                if spot > 0:
                    self.window_open_prices[market.asset] = spot
                    self._snapped_windows.add(market.market_id)
                    logger.info(
                        "SNAPSHOT: %s open price = $%.2f for window %s",
                        market.asset,
                        spot,
                        market.market_id,
                    )

    def _cleanup_expired_windows(self, now_epoch: int):
        """Remove window IDs and stale ladders no longer in the active market list."""
        active_ids = {m.market_id for m in self._active_markets.values()}
        stale = self._snapped_windows - active_ids
        for mid in stale:
            self._snapped_windows.discard(mid)

        # Clean up ladders for markets that are no longer active
        stale_ladders = set(self.ladder_manager.ladders.keys()) - active_ids
        for mid in stale_ladders:
            if mid not in self.position_manager.get_pending_settlements():
                self.ladder_manager.cleanup_ladder(mid)

    def _find_market(self, market_id: str) -> MarketWindow | None:
        market = self._active_markets.get(market_id)
        if market is not None:
            return market
        return self._expired_market_cache.get(market_id)

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
            self._realized_pnl,
            pos_count,
            ladder_count,
            self._trade_count,
            spot_str,
        )

    def _record_activity(
        self, event_type: str, asset: str, detail: str, pnl: float | None = None
    ):
        self._activity_log.append(
            ActivityEvent(
                timestamp=time.time(),
                event_type=event_type,
                asset=asset,
                detail=detail,
                pnl=pnl,
            )
        )

    # ------------------------------------------------------------------
    # State snapshot for GUI
    # ------------------------------------------------------------------

    def build_state_snapshot(self) -> dict:
        """Returns dict matching GUI state spec with all 23 fields."""
        now = int(time.time())
        active_list = list(self._active_markets.values())

        # Build prices dicts
        prices: dict[str, float] = {}
        binance_prices: dict[str, float] = {}
        spots: dict[str, float] = {}
        for asset in self.cfg.assets:
            bp = self.price_feed.get_price(asset)
            if bp is not None:
                binance_prices[asset] = float(bp)
                spots[asset] = float(bp)
            sp = self.spot_prices.get(asset)
            if sp:
                spots[asset] = sp

        # CLOB midpoint prices
        for mid, mkt in self._active_markets.items():
            for token_id in [mkt.up_token_id, mkt.dn_token_id]:
                mp = self.midpoint_poller.get_mid(token_id)
                if mp is not None:
                    prices[token_id] = float(mp)

        # Active markets info
        active_markets_info = []
        for mkt in active_list:
            pos = self.position_manager.positions.get(mkt.market_id)
            ladder = self.ladder_manager.ladders.get(mkt.market_id)
            info: dict = {
                "market_id": mkt.market_id,
                "asset": mkt.asset,
                "timeframe": mkt.timeframe_sec,
                "up_token_id": mkt.up_token_id,
                "dn_token_id": mkt.dn_token_id,
                "remaining_sec": mkt.remaining(now),
                "position": (
                    {
                        "up_qty": pos.up_qty if pos else 0,
                        "dn_qty": pos.dn_qty if pos else 0,
                        "pair_cost": pos.pair_cost() if pos else 0,
                    }
                    if pos
                    else None
                ),
                "ladder": (
                    {
                        "rungs_filled": 0,  # computed from ladder state
                        "rungs_total": self.cfg.get_ladder_params(
                            mkt.timeframe_sec
                        ).rungs,
                    }
                    if ladder
                    else None
                ),
            }
            active_markets_info.append(info)

        pairs_completed = sum(
            1
            for p in self.position_manager.positions.values()
            if p.min_qty() > 0
        )

        return {
            "mode": self.mode,
            "running": self.running,
            "connected": self.running,
            "heartbeat_healthy": self.heartbeat.is_healthy(),
            "cancel_only_mode": self._cancel_only_mode,
            "total_pnl": self._realized_pnl + self._compute_unrealized_pnl(),
            "realized_pnl": self._realized_pnl,
            "unrealized_pnl": self._compute_unrealized_pnl(),
            "trade_count": self._trade_count,
            "position_count": self.position_manager.active_position_count(),
            "pairs_completed": pairs_completed,
            "avg_pair_cost": 0.0,  # TODO
            "imbalance_ratio": 0.0,  # TODO
            "runtime_sec": (
                int(time.time() - self._start_time) if self._start_time else 0
            ),
            "markets_active": len(self._active_markets),
            "win_rate": 0.0,  # TODO
            "prices": prices,
            "binance_prices": binance_prices,
            "spots": spots,
            "active_markets": active_markets_info,
            "activity_feed": [
                {
                    "timestamp": e.timestamp,
                    "type": e.event_type,
                    "asset": e.asset,
                    "detail": e.detail,
                    "pnl": e.pnl,
                }
                for e in self._activity_log
            ],
            "trades": [],
            "pending_settlements": self.position_manager.get_pending_settlements(),
            "wallet": (
                None
                if self.cfg.dry_run
                else self._wallet_address
            ),
        }

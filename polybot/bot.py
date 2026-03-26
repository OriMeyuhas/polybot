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

from polybot.config import BotConfig, get_trading_rules
from polybot.data.price_feed import MultiAssetPriceFeed
from polybot.data.rtds_chainlink import RTDSChainlinkPriceFeed
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
from polybot.risk_manager import RiskManager
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
        self.rtds_feed = RTDSChainlinkPriceFeed()

        # OMS
        self.clob_client = create_clob_client(cfg, book_manager=self.book_manager)
        self.order_executor = OrderExecutor(cfg, self.clob_client)
        self.heartbeat = Heartbeat(
            cfg.heartbeat_interval_sec,
            cfg.heartbeat_max_failures,
            recovery_threshold=cfg.heartbeat_recovery_threshold,
        )

        # Strategy
        self.order_tracker = OrderTracker()
        self.position_manager = PositionManager(cfg, bankroll=cfg.bankroll)
        self.risk = RiskManager(cfg, starting_bankroll=cfg.bankroll)
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
        self._settled_markets: set[str] = set()  # markets that have been settled — no more trading
        self.spot_prices: dict[str, float] = {}
        self.window_open_prices: dict[str, float] = {}
        self._activity_log: deque = deque(maxlen=20)
        self._tasks: list[asyncio.Task] = []
        self._wallet_balance: float = cfg.bankroll
        self._balance_poll_failures: int = 0
        self._wallet_address: str | None = self._derive_wallet_address(cfg)
        self._connection_loss_at: float = 0.0
        self._cancel_only_reason: str = ""

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

    def _fetch_live_balance(self) -> dict:
        """Call get_balance_allowance with correct params for paper vs live."""
        if hasattr(self.clob_client, '_resting'):  # PaperClobClient
            return self.clob_client.get_balance_allowance()
        from py_clob_client.clob_types import BalanceAllowanceParams
        return self.clob_client.get_balance_allowance(BalanceAllowanceParams())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start all subsystems."""
        self.running = True
        self._start_time = time.time()
        self.gui_state.update(running=True, mode=self.mode)

        # Initial balance fetch so first ladder is sized correctly
        if not self.cfg.dry_run:
            try:
                result = await asyncio.to_thread(self._fetch_live_balance)
                raw = result.get("balance")
                if raw is not None:
                    balance = float(raw) / 1e6
                    if balance > 0:
                        self._wallet_balance = balance
                        self.position_manager.update_bankroll(balance)
                        self._balance_poll_failures = 0
                        logger.info("Initial wallet balance: $%.2f", balance)
                    else:
                        self._balance_poll_failures += 1
                        logger.warning("Initial balance returned $0 — using configured bankroll $%.2f", self._wallet_balance)
                else:
                    self._balance_poll_failures += 1
                    logger.warning("Initial balance malformed — using configured bankroll $%.2f", self._wallet_balance)
            except Exception as e:
                logger.warning("Initial balance fetch failed: %s", e)

            # Cancel stale orders from previous session
            try:
                logger.info("Cancelling stale orders from previous session...")
                await asyncio.to_thread(self.order_executor.cancel_all)
                logger.info("Stale orders cancelled — clean slate")
            except Exception as e:
                logger.error("Failed to cancel stale orders: %s — proceed with caution", e)

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

    async def run_standby(self):
        """Start data feeds only — wait for UI Start button to begin trading.

        This keeps the dashboard alive with live prices and market discovery,
        but does NOT post any orders until ui_start_full() is called.
        """
        self.running = True
        self._cancel_only_mode = True  # No trading until Start pressed
        self._start_time = time.time()
        self.gui_state.update(running=True, mode=self.mode)
        logger.info("Bot in standby — waiting for Start button (bankroll: $%.2f, assets: %s)",
                     self.position_manager.bankroll, self.cfg.assets)

        # Start data feeds so dashboard shows live prices
        tasks = [
            asyncio.create_task(self.price_feed.run()),
            asyncio.create_task(self.rtds_feed.run()),
            asyncio.create_task(
                self.midpoint_poller.run(
                    self.cfg.polymarket_host, self.cfg.clob_midpoint_poll_sec
                )
            ),
            asyncio.create_task(self.market_ws.run([])),
            asyncio.create_task(self._run_trading_loop()),
            asyncio.create_task(self._run_settlement_poller()),
            asyncio.create_task(self._poll_wallet_balance()),
            asyncio.create_task(self._run_gui_broadcast()),
        ]
        self._tasks = tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        finally:
            await self.stop()

    async def run(self):
        """Main entry point — start all concurrent tasks (auto-trade immediately)."""
        logger.info(
            "Bot starting — bankroll: $%.2f, assets: %s",
            self.position_manager.bankroll,
            self.cfg.assets,
        )
        await self.start()
        tasks = [
            asyncio.create_task(self.price_feed.run()),
            asyncio.create_task(self.rtds_feed.run()),
            asyncio.create_task(
                self.midpoint_poller.run(
                    self.cfg.polymarket_host, self.cfg.clob_midpoint_poll_sec
                )
            ),
            asyncio.create_task(self.market_ws.run([])),
            asyncio.create_task(
                self.heartbeat.run(
                    self.clob_client,
                    self._on_connection_lost,
                    on_connection_recovered=self._on_connection_recovered,
                )
            ),
            asyncio.create_task(self._run_trading_loop()),
            asyncio.create_task(self._run_settlement_poller()),
            asyncio.create_task(self.redeemer.run(self._redeem_tokens)),
            asyncio.create_task(self._run_daily_reset()),
            asyncio.create_task(self._poll_wallet_balance()),
            asyncio.create_task(self._run_gui_broadcast()),
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
        """Called from web UI POST /api/start — resume trading."""
        self._cancel_only_mode = False
        self._cancel_only_reason = ""
        logger.info("Trading resumed via UI")

    async def ui_start_full(self):
        """Called from web UI POST /api/start — full start including balance fetch."""
        # Initial balance fetch
        if not self.cfg.dry_run:
            try:
                result = await asyncio.to_thread(self._fetch_live_balance)
                raw = result.get("balance")
                if raw is not None:
                    balance = float(raw) / 1e6
                    if balance > 0:
                        self._wallet_balance = balance
                        self.position_manager.update_bankroll(balance)
                        self._balance_poll_failures = 0
                        logger.info("Initial wallet balance: $%.2f", balance)
                    else:
                        self._balance_poll_failures += 1
                        logger.warning("Initial balance returned $0 — using configured bankroll $%.2f", self._wallet_balance)
                else:
                    self._balance_poll_failures += 1
                    logger.warning("Initial balance malformed — using configured bankroll $%.2f", self._wallet_balance)
            except Exception as e:
                logger.warning("Initial balance fetch failed: %s", e)

            # Cancel stale orders from previous session
            try:
                logger.info("Cancelling stale orders from previous session...")
                await asyncio.to_thread(self.order_executor.cancel_all)
                logger.info("Stale orders cancelled — clean slate")
            except Exception as e:
                logger.error("Failed to cancel stale orders: %s — proceed with caution", e)

        self._cancel_only_mode = False
        self._cancel_only_reason = ""
        self._start_time = time.time()
        self._record_activity(
            "INFO", "SYSTEM",
            f"trading started — {'LIVE' if not self.cfg.dry_run else 'PAPER'} mode, bankroll=${self.position_manager.bankroll:,.2f}",
        )
        logger.info("Trading started via UI")

    async def ui_stop(self):
        """Called from web UI POST /api/stop."""
        self._cancel_only_mode = True
        self._cancel_only_reason = "user_stop"
        self._pending_cancel_all = True
        self._record_activity("INFO", "SYSTEM", "trading stopped")
        logger.info("Trading paused via UI")

    # ------------------------------------------------------------------
    # Connection lost callback
    # ------------------------------------------------------------------

    def _on_connection_lost(self):
        self._connection_loss_at = time.time()
        logger.warning("Connection lost — entering cancel-only mode, preserving local state")
        # 1. Enter cancel-only mode — no new ladders
        self._cancel_only_mode = True
        self._cancel_only_reason = "connection_loss"
        # 2. Mark all resting orders as unknown — reconcile() will fix on recovery
        self.order_tracker.mark_all_unknown()
        # 3. Best-effort cancel all on exchange
        try:
            self.order_executor.cancel_all()
        except Exception as exc:
            logger.warning("Best-effort cancel_all failed (expected during outage): %s", exc)
            # 4. Set pending flag so cancel retries on next healthy tick
            self._pending_cancel_all = True
        self._record_activity("WARN", "SYSTEM", "connection lost — cancel-only mode")

    def _on_connection_recovered(self):
        duration = time.time() - self._connection_loss_at if self._connection_loss_at else 0
        logger.info("Connection recovered after %.1fs — running reconciliation", duration)
        # Auto-exit cancel-only mode ONLY if it was set by connection loss
        # (not by user pressing Stop or balance safety)
        if self._cancel_only_reason == "connection_loss":
            self._cancel_only_mode = False
            self._cancel_only_reason = ""
            self._connection_loss_at = 0.0
            self._record_activity(
                "INFO", "SYSTEM",
                f"connection recovered after {duration:.0f}s — resuming trading",
            )
        logger.info("Cancel-only mode: %s (reason: %s)", self._cancel_only_mode, self._cancel_only_reason)

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

    async def _run_gui_broadcast(self):
        """Periodically build state snapshot and push to WebSocket clients."""
        while self.running:
            try:
                snapshot = self.build_state_snapshot()
                self.gui_state.replace(snapshot)
            except Exception as e:
                logger.error("GUI broadcast error: %s", e)
            await asyncio.sleep(0.5)

    async def _trading_loop_tick(self):
        """Single iteration of the trading loop."""
        # Heartbeat health gate
        if not self.heartbeat.is_healthy():
            # Retry pending cancel-all while unhealthy (best-effort)
            if self._pending_cancel_all:
                try:
                    await asyncio.to_thread(self.order_executor.cancel_all)
                    self._pending_cancel_all = False
                    logger.info("Pending cancel-all succeeded during unhealthy state")
                except Exception:
                    pass  # Will retry next tick
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

        # Paper fill simulation — use CLOB midpoint prices (same as dashboard)
        paper_fills = []
        if self.cfg.dry_run and hasattr(self.clob_client, "tick"):
            midpoints = {}
            for mkt in self._active_markets.values():
                if mkt.market_id in self._settled_markets:
                    continue  # no fills on settled markets
                for tid in [mkt.up_token_id, mkt.dn_token_id]:
                    mp = self.midpoint_poller.get_mid(tid)
                    if mp is not None:
                        midpoints[tid] = float(mp)
            paper_fills = self.clob_client.tick(midpoints)

        # Market discovery (every N seconds)
        if time.time() - self._last_discovery_time > self.cfg.market_discovery_interval_sec:
            await self._discover_markets()

        # Build market lookup — exclude settled markets
        active_list = [m for m in self._active_markets.values()
                       if m.market_id not in self._settled_markets]
        market_map = {m.market_id: m for m in active_list}

        if not self._cancel_only_mode:
            # Apply bankroll-adaptive position limits
            rules = get_trading_rules(self.cfg.assets, self.position_manager.bankroll)
            self.risk._max_concurrent_override = rules.max_concurrent

            # Overleverage protection (live mode only)
            overleveraged = (
                not self.cfg.dry_run
                and self._wallet_balance < self.ladder_manager.total_committed()
            )
            if overleveraged:
                logger.warning(
                    "OVERLEVERAGED: wallet $%.2f < committed $%.2f — skipping new ladders",
                    self._wallet_balance, self.ladder_manager.total_committed(),
                )

            # Post ladders on new markets (active + pre-open)
            if not overleveraged:
                for market in active_list:
                    if self.ladder_manager.has_ladder(market.market_id):
                        continue
                    # Don't post on markets that are pending or already settled
                    if market.market_id in self.position_manager.get_pending_settlements():
                        continue
                    if market.market_id in self._settled_markets:
                        continue

                    count = 0
                    pre_open = False

                    if market.is_pre_open(now):
                        # Pre-open: books are live, skip timing/elapsed guards
                        pre_open = True
                        count = await asyncio.to_thread(
                            self.ladder_manager.post_ladder_pre_open, market
                        )
                    elif market.is_active(now):
                        count = await asyncio.to_thread(
                            self.ladder_manager.post_ladder, market
                        )

                    if count > 0:
                        label = "(pre-open) " if pre_open else ""
                        self._record_activity(
                            "LADDER",
                            market.asset,
                            f"{label}posted {count} rungs on {market.market_id}",
                        )

            # Reprice if book moved
            await asyncio.to_thread(
                self.ladder_manager.reprice_if_needed, market_map
            )

        # Check fills — paper mode uses tick() results directly to avoid
        # reconcile misdetecting cancelled orders as fills
        if self.cfg.dry_run:
            filled_orders = await asyncio.to_thread(
                self.ladder_manager.process_paper_fills, paper_fills
            )
        else:
            filled_orders = await asyncio.to_thread(self.ladder_manager.check_fills)
        if filled_orders:
            self._trade_count += len(filled_orders)
            # Log each fill individually with correct side and price
            for order in filled_orders:
                market = market_map.get(order.market_id)
                if not market:
                    continue
                side_label = "UP" if order.side == Side.UP else "DN"
                self._record_activity(
                    "FILL", market.asset,
                    f"{side_label} {order.size:.0f}@${order.price:.2f}",
                )

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
            # Build slug patterns from bankroll-adaptive trading rules
            _ASSET_LONG = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp"}
            _TF_LABELS = {300: "5m", 900: "15m", 3600: "1h"}
            rules = get_trading_rules(self.cfg.assets, self.position_manager.bankroll)
            tradeable = rules.assets
            if len(tradeable) < len(self.cfg.assets) or len(rules.timeframes) < 3:
                tf_names = [_TF_LABELS.get(tf, f"{tf}s") for tf in rules.timeframes]
                logger.info(
                    "TRADING RULES: bankroll=$%.0f -> %s assets, %s timeframes, %d max positions, %.0f%% fraction",
                    self.position_manager.bankroll, tradeable, tf_names,
                    rules.max_concurrent, rules.position_fraction * 100,
                )
            patterns = []
            for asset in tradeable:
                a = asset.lower()
                for tf in rules.timeframes:
                    tf_label = _TF_LABELS.get(tf, f"{tf}s")
                    patterns.append(f"{a}-updown-{tf_label}-")
                long_name = _ASSET_LONG.get(asset, a)
                patterns.append(f"{long_name}-up-or-down-")
            results = await discover_crypto_updown_markets(
                slug_patterns=patterns,
            )
            # Keep up to 2 windows per (asset, timeframe): current + next
            # Filter by allowed timeframes from trading rules
            from collections import defaultdict
            windows_by_key: dict[tuple[str, int], list[MarketWindow]] = defaultdict(list)
            for info, asset in results:
                mw = to_market_window(info, asset)
                if mw.timeframe_sec not in rules.timeframes:
                    continue
                windows_by_key[(mw.asset, mw.timeframe_sec)].append(mw)

            new_markets: dict[str, MarketWindow] = {}
            all_token_ids: list[str] = []
            for key, windows in windows_by_key.items():
                windows.sort(key=lambda w: w.remaining(int(time.time())))
                for mw in windows[:2]:  # current + next
                    new_markets[mw.market_id] = mw
                    all_token_ids.extend([mw.up_token_id, mw.dn_token_id])

            arrived: set[str] = set()
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
                    # Cache departing markets so orphaned positions can still be settled
                    for mid in departed:
                        old_mkt = self._active_markets.get(mid)
                        if old_mkt and mid not in self._expired_market_cache:
                            self._expired_market_cache[mid] = old_mkt
                self._active_markets = new_markets

            # Update subscriptions
            self.midpoint_poller.register_tokens(all_token_ids)
            self.market_ws.update_subscriptions(all_token_ids)
            self.book_manager.update_assets(all_token_ids)

            # Seed books via HTTP for newly arrived tokens (faster than waiting for WS)
            for mid in arrived:
                mw = new_markets.get(mid)
                if mw:
                    for tid in [mw.up_token_id, mw.dn_token_id]:
                        await self.book_manager.seed_book_http(tid, self.cfg.polymarket_host)

            self._last_discovery_time = time.time()
            for mid in arrived:
                mw = new_markets.get(mid)
                if mw:
                    tf_label = f"{mw.timeframe_sec // 60}m" if mw.timeframe_sec < 3600 else f"{mw.timeframe_sec // 3600}h"
                    self._record_activity(
                        "INFO", mw.asset,
                        f"new {tf_label} window — {mw.remaining(int(time.time()))}s remaining",
                    )
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
        """Mark expired windows for settlement.

        Checks both active markets AND orphaned positions (markets removed
        by discovery before settlement could run).
        """
        # 1. Check active markets for expired windows
        for market in list(self._active_markets.values()):
            if market.is_active(now_epoch):
                continue
            # Don't settle pre-open windows — they haven't started yet
            if now_epoch < market.close_epoch:
                continue
            mid = market.market_id
            pos = self.position_manager.positions.get(mid)
            if pos is None:
                continue
            if mid in self.position_manager.get_pending_settlements():
                continue

            # Cancel any remaining orders on exchange
            try:
                await asyncio.to_thread(self.ladder_manager.cancel_ladder, mid)
            except Exception as exc:
                logger.warning("Cancel ladder failed during settlement of %s: %s", mid, exc)
            self.ladder_manager.cleanup_ladder(mid)

            # Clean up window state
            self._snapped_windows.discard(mid)

            # Mark for async settlement
            self.position_manager.mark_pending_settlement(mid)
            self._expired_market_cache[mid] = market
            logger.info("Window expired for %s — pending settlement", mid)

        # 2. Check for orphaned positions — markets removed by discovery
        # that still have open positions. These must be settled to free capital.
        active_ids = set(self._active_markets.keys())
        for mid in list(self.position_manager.positions.keys()):
            if mid in active_ids:
                continue  # still tracked
            if mid in self.position_manager.get_pending_settlements():
                continue  # already pending
            # Market was removed from active list but position remains — orphaned
            market = self._expired_market_cache.get(mid)
            if market is None:
                # Try to reconstruct minimal market info from the position
                # Force settlement using spot delta
                logger.warning(
                    "ORPHANED POSITION: %s has no market data — forcing settlement", mid,
                )
                # Create a minimal placeholder so settlement poller can resolve it
                pos = self.position_manager.positions[mid]
                # Determine asset from market_id (e.g., "btc-updown-5m-...")
                asset = mid.split("-")[0].upper() if "-" in mid else "BTC"
                from polybot.types import MarketWindow as MW
                market = MW(
                    market_id=mid,
                    condition_id="orphaned",
                    asset=asset,
                    timeframe_sec=300,
                    up_token_id="",
                    dn_token_id="",
                    open_epoch=0,
                    close_epoch=now_epoch - 1,
                )
                self._expired_market_cache[mid] = market

            # Cancel any lingering orders
            try:
                await asyncio.to_thread(self.ladder_manager.cancel_ladder, mid)
            except Exception as exc:
                logger.warning("Cancel ladder failed during orphan settlement of %s: %s", mid, exc)
            self.ladder_manager.cleanup_ladder(mid)
            self._snapped_windows.discard(mid)
            self.position_manager.mark_pending_settlement(mid)
            logger.warning(
                "ORPHANED POSITION settled: %s — market removed by discovery but position remained",
                mid,
            )

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

            # Only update bankroll from PnL in paper mode.
            # In live mode, on-chain balance is the source of truth.
            if self.cfg.dry_run:
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
        self._settled_markets.add(mid)  # prevent re-posting ladders

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
        """Redeem winning tokens on-chain. Returns USDC.e received.

        Full on-chain redemption via web3 + CTF Exchange is Phase 3.
        For now, log the details so the user can redeem manually via Polymarket UI.
        """
        logger.critical(
            "REDEMPTION NEEDED: condition_id=%s, token_ids=%s — "
            "redeem manually via Polymarket UI until on-chain implementation is added",
            condition_id, token_ids,
        )
        self._record_activity(
            "REDEEM", "SYSTEM",
            f"manual redemption needed: {condition_id} tokens={len(token_ids)}",
        )
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
                        pos = self.position_manager.positions.get(mid)
                        locked = (pos.up_cost + pos.dn_cost) if pos else 0.0
                        logger.critical(
                            "SETTLEMENT TIMEOUT: %s after %.0fs — $%.2f capital locked. "
                            "Manual recovery: check condition_id=%s on Polymarket",
                            mid, elapsed, locked, market.condition_id,
                        )
                        self._record_activity(
                            "SETTLEMENT_FAIL", market.asset,
                            f"timeout after {elapsed:.0f}s — ${locked:.2f} locked",
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
        """Poll wallet USDC balance every 60s. Syncs into position_manager.bankroll."""
        while self.running:
            try:
                if not self.cfg.dry_run:
                    result = await asyncio.to_thread(self._fetch_live_balance)
                    raw = result.get("balance")
                    if raw is not None:
                        balance = float(raw) / 1e6
                        if balance > 0:
                            self._wallet_balance = balance
                            self.position_manager.update_bankroll(balance)
                            self._balance_poll_failures = 0
                        else:
                            self._balance_poll_failures += 1
                            logger.warning("Balance poll returned $0 — keeping $%.2f (fail #%d)",
                                           self._wallet_balance, self._balance_poll_failures)
                    else:
                        self._balance_poll_failures += 1
                        logger.warning("Balance poll malformed — keeping $%.2f (fail #%d)",
                                       self._wallet_balance, self._balance_poll_failures)

                    if self._balance_poll_failures >= 5:
                        logger.error("SAFETY: 5 consecutive balance failures — entering cancel-only mode")
                        self._cancel_only_mode = True
                        self._cancel_only_reason = "balance_safety"
                else:
                    self._wallet_balance = self.position_manager.bankroll
            except Exception as e:
                logger.warning("Balance poll failed (keeping last known): %s", e)
            await asyncio.sleep(self.cfg.balance_poll_sec)

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
        """Capture spot prices at the start of each new market window.

        Only snap for windows that are currently active (open_epoch <= now).
        Pre-open windows must wait until they actually open — snapping early
        would record the wrong open price and corrupt settlement outcome.
        """
        now = int(time.time())
        for market in self._active_markets.values():
            if market.market_id not in self._snapped_windows:
                if not market.is_active(now):
                    continue  # pre-open: wait until window opens
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

        # Clean up settled market markers when markets leave active list
        stale_settled = self._settled_markets - active_ids
        self._settled_markets -= stale_settled

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

        # Build prices dicts — RTDS primary, Binance fallback (matches polytrader)
        prices: dict[str, float] = {}
        binance_prices: dict[str, float] = {}
        spots: dict[str, float] = {}
        rtds_prices = self.rtds_feed.get_all_crypto_prices()
        for asset in self.cfg.assets:
            # RTDS (Chainlink + Binance via Polymarket) — primary
            rtds_p = rtds_prices.get(asset)
            if rtds_p is not None:
                spots[asset] = float(rtds_p)
            # Direct Binance WS — fallback
            bp = self.price_feed.get_price(asset)
            if bp is not None:
                binance_prices[asset] = float(bp)
                if asset not in spots:
                    spots[asset] = float(bp)

        # CLOB midpoint prices (keyed by token_id for market cards)
        for mid, mkt in self._active_markets.items():
            for token_id in [mkt.up_token_id, mkt.dn_token_id]:
                mp = self.midpoint_poller.get_mid(token_id)
                if mp is not None:
                    prices[token_id] = float(mp)

        # Polymarket asset-level spot prices (RTDS primary, Binance fallback)
        polymarket_prices: dict[str, float] = dict(spots)

        # Active markets info — flat format matching frontend expectations
        active_markets_info = []
        for mkt in active_list:
            pos = self.position_manager.positions.get(mkt.market_id)
            ladder = self.ladder_manager.ladders.get(mkt.market_id)

            # Build human-readable label
            tf_label = f"{mkt.timeframe_sec // 60}m" if mkt.timeframe_sec < 3600 else f"{mkt.timeframe_sec // 3600}h"
            label = f"{mkt.asset} {tf_label}"

            # Midpoint prices from CLOB poller
            up_mid = prices.get(mkt.up_token_id)
            dn_mid = prices.get(mkt.dn_token_id)

            # Book prices from book manager (bid + ask, like polytrader)
            up_ask = ladder.current_ask_up if ladder else None
            dn_ask = ladder.current_ask_dn if ladder else None
            up_bid: float | None = None
            dn_bid: float | None = None
            up_book = self.book_manager.get_book(mkt.up_token_id)
            dn_book = self.book_manager.get_book(mkt.dn_token_id)
            if up_book:
                if up_book.best_ask is not None and up_ask is None:
                    up_ask = float(up_book.best_ask)
                if up_book.best_bid is not None:
                    up_bid = float(up_book.best_bid)
            if dn_book:
                if dn_book.best_ask is not None and dn_ask is None:
                    dn_ask = float(dn_book.best_ask)
                if dn_book.best_bid is not None:
                    dn_bid = float(dn_book.best_bid)

            # Current spot price for this asset (RTDS primary, Binance fallback)
            current_price = spots.get(mkt.asset)

            # Spread width
            spread_width = None
            if up_mid is not None and dn_mid is not None:
                spread_width = 1.0 - (up_mid + dn_mid)

            # Ladder stats (flat, not nested)
            lp = self.cfg.get_ladder_params(
                mkt.timeframe_sec,
                current_bankroll=self.position_manager.bankroll,
            )
            rungs_total = lp.rungs
            rungs_filled = 0
            imbalance = None
            if ladder:
                try:
                    stats = self.ladder_manager.get_ladder_stats(mkt.market_id)
                    if stats:
                        rungs_filled = stats.get("up_filled_count", 0) + stats.get("dn_filled_count", 0)
                        imbalance = stats.get("imbalance")
                except Exception:
                    pass

            # Position (both sides for frontend)
            position = None
            if pos and (pos.up_qty > 0 or pos.dn_qty > 0):
                position = {
                    "unrealized_pnl": 0.0,
                    "up_qty": pos.up_qty,
                    "up_avg": pos.up_cost / pos.up_qty if pos.up_qty > 0 else 0,
                    "up_cost": pos.up_cost,
                    "dn_qty": pos.dn_qty,
                    "dn_avg": pos.dn_cost / pos.dn_qty if pos.dn_qty > 0 else 0,
                    "dn_cost": pos.dn_cost,
                    "pair_cost": pos.pair_cost(),
                }

            # Compute window status for frontend
            if mkt.is_active(now):
                window_status = "active"
            elif mkt.is_pre_open(now):
                window_status = "pre_open"
            elif now < mkt.open_epoch:
                window_status = "upcoming"
            else:
                window_status = "expired"

            # Time until window opens (None if already open/expired)
            opens_in_sec = max(0, mkt.open_epoch - now) if now < mkt.open_epoch else None

            info: dict = {
                "market_id": mkt.market_id,
                "slug": mkt.market_id,
                "label": label,
                "asset": mkt.asset,
                "timeframe": mkt.timeframe_sec,
                "up_token_id": mkt.up_token_id,
                "dn_token_id": mkt.dn_token_id,
                "remaining_sec": mkt.remaining(now),
                "current_price": current_price,
                "up_mid": up_mid,
                "down_mid": dn_mid,
                "up_ask": up_ask,
                "down_ask": dn_ask,
                "up_bid": up_bid,
                "down_bid": dn_bid,
                "spread_width": spread_width,
                "rungs_filled": rungs_filled,
                "rungs_total": rungs_total,
                "imbalance": imbalance,
                "position": position,
                "window_status": window_status,
                "opens_in_sec": opens_in_sec,
            }
            active_markets_info.append(info)

        # Sort by asset name, then timeframe, then status (active before pre_open/upcoming)
        _asset_order = {"BTC": 0, "ETH": 1, "SOL": 2, "XRP": 3}
        _status_order = {"active": 0, "pre_open": 1, "upcoming": 2, "expired": 3}
        active_markets_info.sort(
            key=lambda m: (
                _asset_order.get(m.get("asset", ""), 99),
                m.get("timeframe", 0),
                _status_order.get(m.get("window_status", ""), 99),
            )
        )

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
            "prices": polymarket_prices,
            "binance_prices": binance_prices,
            "spots": spots,
            "active_markets": active_markets_info,
            "activity_feed": [
                {
                    "ts": e.timestamp,
                    "kind": e.event_type,
                    "msg": f"[{e.asset}] {e.detail}" if e.asset else e.detail,
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

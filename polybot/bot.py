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

from polybot.config import BotConfig, get_trading_rules, filter_rules_by_config
from polybot.data.data_recorder import DataRecorder
from polybot.data.price_feed import MultiAssetPriceFeed
from polybot.data.rtds_chainlink import RTDSChainlinkPriceFeed
from polybot.data.book_manager import BookManager
from polybot.data.market_ws import MarketWSClient
from polybot.data.clob_midpoints import ClobMidpointPoller
from polybot.data.gamma import discover_crypto_updown_markets, to_market_window
from polybot.oms.clob_client import create_clob_client
from polybot.oms.order_executor import OrderExecutor
from polybot.oms.heartbeat import Heartbeat
from polybot.strategy.fair_value import p_fair_up, certainty as fv_certainty
from polybot.strategy.ladder_manager import LadderManager
from polybot.strategy.order_tracker import OrderTracker
from polybot.strategy.position_manager import PositionManager
from polybot.strategy.vol_estimator import VolEstimator
from polybot.risk_manager import RiskManager
from polybot.tick_size_cache import TickSizeCache
from polybot.redeemer import Redeemer
from polybot.errors import ClobApiError
from polybot.types import MarketWindow, Side, ActivityEvent
from polybot.web.state import GuiStateHolder
from polybot import market_logger


def _ui_price_to_beat(mkt, chainlink_price, binance_snapshot) -> float:
    """Resolve the value displayed in the UI as "Target" / Price to Beat.

    Must match Polymarket's displayed "Price to Beat" exactly. That value is the
    Gamma API's `priceToBeat` field (stored on MarketWindow.price_to_beat), so use
    it whenever present. Chainlink/Binance are fallbacks only — they drift by a few
    dollars from the strike that Polymarket publishes and resolves against.
    """
    gamma = getattr(mkt, "price_to_beat", None)
    if gamma:
        try:
            val = float(gamma)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return float(chainlink_price or binance_snapshot or 0)


def _ui_binance_spot_values(binance_prices: dict, spots: dict) -> dict:
    """Resolve the values displayed in the UI's "BINANCE SPOT" price strip.

    The arbitrage edge is the *lag* between live Binance spot and Polymarket's
    Chainlink-derived reference. Displaying a Chainlink-leaning number under a
    "BINANCE SPOT" label defeats the purpose — it shows the very thing we're
    racing against. Always prefer the direct Binance WS feed (`binance_prices`)
    and fall back to the blended `spots` dict only when Binance WS is offline.

    Returns a dict of {symbol: price}.
    """
    return dict(binance_prices) if binance_prices else dict(spots or {})

logger = logging.getLogger(__name__)

# How often to print a status summary (seconds)
STATUS_INTERVAL_SEC = 30


class Bot:
    """Central coordinator: owns all subsystems and runs the main trading loop."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.running = False
        self.mode = "dry_run" if cfg.dry_run else "live"

        # Vol estimators (one per asset — fed by Binance ticks)
        self._vol_estimators: dict[str, VolEstimator] = {
            asset: VolEstimator(
                min_samples=cfg.vol_min_samples,
                fallback_vol_annual=cfg.vol_fallback_annual,
            )
            for asset in cfg.assets
        }

        # Data recorder (must be created before components that use it)
        self.data_recorder = DataRecorder(data_dir="data")

        # Data layer
        self.price_feed = MultiAssetPriceFeed(
            assets=cfg.assets,
            coingecko_ids=cfg.coingecko_ids,
            ws_base_url=cfg.binance_ws_url,
            fallback_interval_sec=cfg.binance_fallback_interval_sec,
            on_tick=self._on_price_tick,
        )
        self.book_manager = BookManager(data_recorder=self.data_recorder)
        self.market_ws = MarketWSClient(
            url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
            on_message=self.book_manager.process_message,
            ping_interval_sec=cfg.market_ws_ping_sec,
        )
        self.midpoint_poller = ClobMidpointPoller()
        self.rtds_feed = RTDSChainlinkPriceFeed(
            on_tick=lambda asset, price, src: self.data_recorder.log_price(time.time(), asset, price, src)
        )

        # OMS
        self.clob_client = create_clob_client(cfg, book_manager=self.book_manager)
        self.order_executor = OrderExecutor(cfg, self.clob_client, data_recorder=self.data_recorder)
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
        self._stopped = False
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
        self._ptb_logged: set[str] = set()  # markets where PTB source was already logged
        self._settled_markets: set[str] = set()  # markets that have been settled — no more trading
        self._settled_wins = 0
        self._settled_losses = 0
        self._settled_pair_costs: list[float] = []
        self._per_asset_pnl: dict[str, float] = {}  # asset -> cumulative realized PnL
        self._per_asset_pairs: dict[str, int] = {}  # asset -> settlement count
        self._settlement_history: list[dict] = []  # all settlements for UI
        self._pnl_series: list[dict] = []  # {"ts": epoch, "pnl": cumulative} for graph
        self.spot_prices: dict[str, float] = {}
        self.ladder_manager._spot_prices = self.spot_prices  # share by reference
        self.window_open_prices: dict[str, float] = {}  # keyed by market_id, NOT asset
        self._activity_log: deque = deque(maxlen=50)
        self._tasks: list[asyncio.Task] = []
        self._wallet_balance: float = cfg.bankroll
        self._balance_poll_failures: int = 0
        self._wallet_address: str | None = self._derive_wallet_address(cfg)
        self._connection_loss_at: float = 0.0
        self._cancel_only_reason: str = ""
        self._price_stale_logged: bool = False
        self._last_strategy_log_ts: dict[str, float] = {}  # market_id -> last log ts

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
        from py_clob_client_v2.clob_types import BalanceAllowanceParams
        return self.clob_client.get_balance_allowance(BalanceAllowanceParams())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start all subsystems."""
        self.running = True
        self._start_time = time.time()
        self.gui_state.update(running=True, mode=self.mode)

        # Initial balance fetch so first ladder is sized correctly.
        # In live mode the on-chain USDC balance is AUTHORITATIVE — .env BANKROLL is
        # informational only (stale from last paper run) and must be overridden. If the
        # balance query fails or returns 0 we refuse to proceed rather than silently
        # trading with a mis-sized paper bankroll against real money.
        if not self.cfg.dry_run:
            env_bankroll = self.cfg.bankroll  # snapshot what .env said, for logging
            try:
                result = await asyncio.to_thread(self._fetch_live_balance)
            except Exception as e:
                raise RuntimeError(
                    f"[LIVE] Initial on-chain balance fetch failed: {e}. "
                    f"Refusing to start live with stale .env BANKROLL=${env_bankroll:.2f}."
                ) from e
            raw = result.get("balance") if isinstance(result, dict) else None
            if raw is None:
                raise RuntimeError(
                    f"[LIVE] On-chain balance response malformed (no 'balance' field): {result!r}. "
                    f"Refusing to start live with stale .env BANKROLL=${env_bankroll:.2f}."
                )
            balance = float(raw) / 1e6
            if balance <= 0:
                raise RuntimeError(
                    f"[LIVE] On-chain USDC balance is ${balance:.2f} — wallet not funded. "
                    f"Refusing to start live with stale .env BANKROLL=${env_bankroll:.2f}."
                )
            self._wallet_balance = balance
            self.position_manager.update_bankroll(balance)
            self.risk.starting_bankroll = balance
            self._balance_poll_failures = 0
            logger.warning(
                "[LIVE] Bankroll from on-chain: $%.2f (ignoring .env BANKROLL=$%.2f)",
                balance, env_bankroll,
            )

            # Cancel stale orders from previous session (gate on failure)
            try:
                logger.info("Cancelling stale orders from previous session...")
                await asyncio.to_thread(self.order_executor.cancel_all)
                logger.info("Stale orders cancelled — clean slate")
            except Exception as e:
                logger.error(
                    "Failed to cancel stale orders: %s — blocking trading", e
                )
                self._cancel_only_mode = True
                self._cancel_only_reason = "cancel_all_failed"

            # Pre-flight stale fill audit
            if not self._cancel_only_mode:
                try:
                    matched = await asyncio.to_thread(
                        self.order_executor.get_recent_matched_orders
                    )
                    if matched:
                        count = len(matched)
                        logger.error(
                            "STALE FILL DETECTED: %d matched orders from previous session — "
                            "staying in cancel_only_mode", count,
                        )
                        self._cancel_only_mode = True
                        self._cancel_only_reason = f"stale_fills:{count}"
                except Exception as e:
                    logger.warning("Stale fill audit failed: %s — proceeding cautiously", e)

        logger.info("Bot started in %s mode", self.mode)

    async def stop(self):
        """Graceful shutdown — idempotent, non-blocking, resilient."""
        if self._stopped:
            return
        self._stopped = True
        self.running = False

        # Cancel all resting orders in live mode (non-blocking with timeout)
        if not self.cfg.dry_run:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.order_executor.cancel_all),
                    timeout=10.0,
                )
                logger.info("All orders cancelled on shutdown")
            except asyncio.TimeoutError:
                logger.error("cancel_all timed out after 10s on shutdown")
            except Exception as e:
                logger.error("Error cancelling orders on shutdown: %s", e)

        # Stop subsystems — each in try/except so one failure does not block others
        for coro in [
            self.price_feed.stop(),
            self.midpoint_poller.stop(),
            self.market_ws.stop(),
            self.heartbeat.stop(),
            self.rtds_feed.stop(),
        ]:
            try:
                await asyncio.wait_for(coro, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Subsystem stop timed out: %s", coro)
            except Exception as e:
                logger.warning("Subsystem stop error: %s", e)

        # Cancel async tasks with grace period
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5.0)
        self._tasks.clear()

        # Close data recorder
        try:
            self.data_recorder.close()
        except Exception:
            pass

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
            asyncio.create_task(
                self.heartbeat.run(
                    self.clob_client,
                    self._on_connection_lost,
                    on_connection_recovered=self._on_connection_recovered,
                )
            ),
            asyncio.create_task(self.redeemer.run(self._redeem_tokens)),
            asyncio.create_task(self._run_trading_loop()),
            asyncio.create_task(self._run_settlement_poller()),
            asyncio.create_task(self._run_daily_reset()),
            asyncio.create_task(self._poll_wallet_balance()),
            asyncio.create_task(self._run_gui_broadcast()),
        ]
        self._tasks = tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")

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
        # Clear stale alert from previous attempt (Step 6)
        self.gui_state.update(stale_order_alert="")

        # Credential pre-flight + initial balance fetch
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
                        logger.info("Credential check passed — wallet balance: $%.2f", balance)
                    else:
                        raise ValueError("Wallet balance is $0 — check funding")
                else:
                    raise ValueError("Balance response malformed — check API credentials")
            except Exception as e:
                logger.error("Credential/balance check failed: %s — blocking trading", e)
                self._cancel_only_mode = True
                self._cancel_only_reason = "credential_check_failed"
                self._record_activity(
                    "ALERT", "SYSTEM",
                    f"Credential check failed: {e}. Fix credentials and hit Start again.",
                )
                self.gui_state.update(
                    stale_order_alert=f"Credential check failed: {e}"
                )
                return

            # Cancel stale orders from previous session (Step 3 — gate on failure)
            try:
                logger.info("Cancelling stale orders from previous session...")
                await asyncio.to_thread(self.order_executor.cancel_all)
                logger.info("Stale orders cancelled — clean slate")
            except Exception as e:
                logger.error(
                    "Failed to cancel stale orders: %s — blocking trading", e
                )
                self._cancel_only_mode = True
                self._cancel_only_reason = "cancel_all_failed"
                self._record_activity(
                    "ALERT", "SYSTEM",
                    f"cancel_all failed: {e}. Trading blocked — fix connection and hit Start again.",
                )
                self.gui_state.update(
                    stale_order_alert="cancel_all failed — trading blocked"
                )
                return  # Do NOT proceed to trading

            # Pre-flight: detect matched orders from before this session (Step 2)
            try:
                matched = await asyncio.to_thread(
                    self.order_executor.get_recent_matched_orders
                )
                if matched:
                    count = len(matched)
                    logger.error(
                        "STALE FILL DETECTED: %d matched orders from previous session — "
                        "staying in cancel_only_mode", count,
                    )
                    self._cancel_only_mode = True
                    self._cancel_only_reason = f"stale_fills:{count}"
                    self._record_activity(
                        "ALERT", "SYSTEM",
                        f"Stale fills detected: {count} matched orders from previous session. "
                        "Manual review required — hit Start again after confirming positions.",
                    )
                    self.gui_state.update(
                        stale_order_alert=f"{count} stale fills from previous session"
                    )
                    return  # Do NOT proceed to trading
            except Exception as e:
                logger.warning("Stale fill audit failed: %s — proceeding cautiously", e)

        self._cancel_only_mode = False
        self._cancel_only_reason = ""
        self._start_time = time.time()
        self._trading_active = True
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
        # 3. Schedule best-effort cancel in a thread (don't block event loop)
        async def _async_cancel():
            try:
                await asyncio.to_thread(self.order_executor.cancel_all)
            except Exception as exc:
                logger.warning("Best-effort cancel_all failed (expected during outage): %s", exc)
                self._pending_cancel_all = True
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_async_cancel())
            else:
                self._pending_cancel_all = True
        except RuntimeError:
            self._pending_cancel_all = True
        self._record_activity("WARN", "SYSTEM", "connection lost — cancel-only mode")

    def _on_connection_recovered(self):
        duration = time.time() - self._connection_loss_at if self._connection_loss_at else 0
        logger.info("Connection recovered after %.1fs — scheduling reconciliation", duration)
        # Auto-exit cancel-only mode ONLY if it was set by connection loss
        if self._cancel_only_reason == "connection_loss":
            self._cancel_only_mode = False
            self._cancel_only_reason = ""
            self._connection_loss_at = 0.0
            self._record_activity(
                "INFO", "SYSTEM",
                f"connection recovered after {duration:.0f}s — resuming trading",
            )
        # Schedule order resolution in a thread (sync HTTP calls, don't block event loop)
        async def _async_resolve():
            try:
                unknown_ids = self.order_tracker.get_unknown_ids()
                if unknown_ids:
                    statuses = {}
                    for oid in unknown_ids:
                        try:
                            resp = await asyncio.to_thread(self.clob_client.get_order, oid)
                            statuses[oid] = resp.get("status", "CANCELLED")
                        except Exception:
                            statuses[oid] = "CANCELLED"
                    result = self.order_tracker.resolve_unknowns(statuses)
                    logger.info(
                        "Resolved %d unknown orders: %d reverted, %d filled, %d cancelled",
                        len(unknown_ids),
                        len(result["reverted"]),
                        len(result["filled"]),
                        len(result["cancelled"]),
                    )
            except Exception:
                logger.exception("Failed to resolve unknown orders during recovery")

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_async_resolve())
        except RuntimeError:
            logger.warning("Could not schedule order resolution — no event loop")

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

        # Snapshot open prices for new windows (uses Binance klines API)
        await self._snapshot_window_open_prices()

        # Update spot prices from price feed
        self._update_spot_prices()

        # Price staleness gate — check ALL enabled assets
        price_stale = not self.price_feed.is_fresh(self.cfg.price_stale_sec)
        if price_stale and not self._price_stale_logged:
            logger.warning(
                "PRICE FEED STALE: one or more assets >%.0fs old — blocking new ladders and repricing",
                self.cfg.price_stale_sec,
            )
            self._price_stale_logged = True
        elif not price_stale and self._price_stale_logged:
            logger.info("PRICE FEED RECOVERED: all assets fresh — resuming normal trading")
            self._price_stale_logged = False

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

        fair_values: dict[str, tuple[float, float]] = {}

        if not self._cancel_only_mode and not price_stale:
            # Apply bankroll-adaptive position limits
            rules = filter_rules_by_config(
                get_trading_rules(self.cfg.assets, self.position_manager.bankroll), self.cfg
            )
            self.risk._max_concurrent_override = rules.max_concurrent

            # Overleverage protection (live mode only)
            overleveraged = (
                not self.cfg.dry_run
                and self._wallet_balance < self.ladder_manager.resting_order_cost()
            )
            if overleveraged:
                logger.warning(
                    "OVERLEVERAGED: wallet $%.2f < resting orders $%.2f — skipping new ladders",
                    self._wallet_balance, self.ladder_manager.resting_order_cost(),
                )

            # Compute fair values for all active markets
            if self.cfg.fair_value_enabled:
                for market in active_list:
                    fair_values[market.market_id] = self.compute_fair_value(market)

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
                    fv_up, fv_vol = fair_values.get(market.market_id, (0.5, None))

                    if market.is_pre_open(now):
                        pre_open = True
                        sd = self.compute_spot_delta(market.asset, market.market_id)
                        count = await asyncio.to_thread(
                            self.ladder_manager.post_ladder_pre_open, market, sd,
                            fair_up=fv_up, vol_annualized=fv_vol,
                        )
                    elif market.is_active(now):
                        sd = self.compute_spot_delta(market.asset, market.market_id)
                        count = await asyncio.to_thread(
                            self.ladder_manager.post_ladder, market, sd,
                            fair_up=fv_up, vol_annualized=fv_vol,
                        )

                    if count > 0:
                        label = "(pre-open) " if pre_open else ""
                        self._record_activity(
                            "LADDER",
                            market.asset,
                            f"{label}posted {count} rungs on {market.market_id}",
                        )
                        # Log real order book snapshot for verification
                        if self.cfg.dry_run and getattr(self, '_trading_active', False):
                            try:
                                market_logger.log_book_snapshot(
                                    market.market_id, market.asset,
                                    market.up_token_id, market.dn_token_id,
                                    reason="ladder_post",
                                )
                            except Exception:
                                pass

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
            filled_orders = await asyncio.to_thread(
                self.ladder_manager.check_fills, self._settled_markets
            )
        if filled_orders:
            self._trade_count += len(filled_orders)
            # Log each fill individually with correct side and price
            for order in filled_orders:
                market = market_map.get(order.market_id)
                if not market:
                    continue
                side_label = "UP" if order.side == Side.UP else "DN"
                # Distinguish BUY fills from FV-exit SELL fills so downstream
                # analyzers can correctly attribute direction without mis-reading.
                fill_dir = getattr(order, 'fill_direction', 'BUY')
                fill_event = "sell_fill" if fill_dir == "SELL" else "buy_fill"
                self.data_recorder.log_order(
                    time.time(), fill_event, market.market_id,
                    side_label, order.price, order.size,
                    order.order_id, "detected",
                )
                self._record_activity(
                    "FILL", market.asset,
                    f"{side_label} {order.size:.0f}@${order.price:.2f} on {market.market_id}",
                )
                # Log real book state at fill time for verification
                if self.cfg.dry_run and getattr(self, '_trading_active', False):
                    try:
                        token_id = market.up_token_id if order.side == Side.UP else market.dn_token_id
                        market_logger.log_fill(
                            market.market_id, market.asset, side_label,
                            order.price, order.size, token_id,
                        )
                    except Exception:
                        pass

        # Fair value: cancel losing side's resting orders when certainty > 70%
        for market in active_list:
            if market.is_active(now) and self.ladder_manager.has_ladder(market.market_id):
                fv_up, _ = fair_values.get(market.market_id, (0.5, None))
                cancelled = await asyncio.to_thread(
                    self.ladder_manager.cancel_losing_side_orders, market, fair_up=fv_up
                )
                if cancelled > 0:
                    losing = "DN" if fv_up > 0.5 else "UP"
                    self._record_activity(
                        "FV_CANCEL", market.asset,
                        f"cancelled {cancelled} {losing} resting orders (certainty {fv_certainty(fv_up)*100:.0f}%) on {market.market_id}",
                    )

        # Imbalance lock DISABLED — data shows lock + boost + chase INVERTS the imbalance
        # instead of fixing it. 32/100 settlements affected, -$136 drag. The pair math works
        # when both sides fill naturally. FV cancel at 60% is the only protection needed.
        # See: polybot/tasks/lessons.md, researcher imbalance_lock_analysis.md

        # Loss cap: cancel remaining orders on one-sided positions exceeding 3% bankroll
        loss_cap_fires = await asyncio.to_thread(
            self.ladder_manager.check_loss_cap, self.spot_prices, self.window_open_prices
        )
        # Proposal #54: surface LOSS_CAP fires to activity log (previously discarded)
        for mid in loss_cap_fires:
            mkt = market_map.get(mid)
            asset = mkt.asset if mkt is not None else "UNKNOWN"
            self._record_activity("LOSS_CAP", asset, f"loss cap fired on {mid}")

        # Proposal #54: drain ONE-SIDED ABORT fires queued by check_one_sided_abort
        for abort in self.ladder_manager._recent_aborts:
            self._record_activity(
                "ONE_SIDED_ABORT", abort["asset"],
                f"one-sided abort: up={abort['up_qty']:.1f} dn={abort['dn_qty']:.1f} "
                f"cost=${abort['cost']:.2f} on {abort['market_id']}",
            )
        self.ladder_manager._recent_aborts.clear()

        # Proposal #54: drain FV CANCEL CIRCUIT BREAKER fires queued by cancel_losing_side_orders
        for cb in self.ladder_manager._recent_circuit_breaker_fires:
            self._record_activity(
                "FV_CIRCUIT_BREAKER", cb["asset"],
                f"FV cancel circuit breaker: {cb['cancel_count']} cancels in 60s on {cb['market_id']}",
            )
        self.ladder_manager._recent_circuit_breaker_fires.clear()

        # Boost DISABLED — floods light side with 16-22 rungs after lock, causing 3:1 inversion
        # chase_pair DISABLED — same problem, adds 6+ rungs on top of boost
        # Force-buy DISABLED — data shows 67% net harmful

        # Directional buy: late-window high-certainty purchase of winning side
        for market in active_list:
            if market.is_active(now) and self.ladder_manager.has_ladder(market.market_id):
                fv_up, _ = fair_values.get(market.market_id, (0.5, None))
                dir_result = await asyncio.to_thread(
                    self.ladder_manager.directional_buy, market, now, fair_up=fv_up
                )
                if dir_result:
                    self._record_activity(
                        "DIRECTIONAL", market.asset,
                        f"buying {dir_result['side'].value} {dir_result['qty']:.0f} @ ${dir_result['price']:.3f} "
                        f"(EV=${dir_result['ev_per_share']:.3f}) on {market.market_id}",
                    )

        # Exit: sell losing one-sided positions (certainty-based when FV enabled)
        for market in active_list:
            if market.is_active(now) and self.ladder_manager.has_ladder(market.market_id):
                fv_up, _ = fair_values.get(market.market_id, (0.5, None))
                exit_result = await asyncio.to_thread(
                    self.ladder_manager.sell_losing_side, market, now, fair_up=fv_up
                )
                if exit_result:
                    self._record_activity(
                        "EXIT", market.asset,
                        f"selling {exit_result['side'].value} {exit_result['qty']:.0f} @ ${exit_result['price']:.3f} on {market.market_id}",
                    )

        # Cancel rungs on expiring windows (hold winning side near expiry if high certainty)
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

        # Strategy state logging (every 5 seconds per market)
        self._log_strategy_states(now, active_list, fair_values if not self._cancel_only_mode and not price_stale else {})

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
            rules = filter_rules_by_config(
                get_trading_rules(self.cfg.assets, self.position_manager.bankroll), self.cfg
            )
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
                    for mid in arrived:
                        mw = new_markets[mid]
                        self.data_recorder.log_market_event(
                            time.time(), "discovered", mid, mw.asset, mw.timeframe_sec,
                            {
                                "open_epoch": mw.open_epoch,
                                "close_epoch": mw.close_epoch,
                                "up_token_id": mw.up_token_id,
                                "dn_token_id": mw.dn_token_id,
                            },
                        )
                if departed:
                    logger.info("EXPIRED WINDOWS: %s", ", ".join(departed))
                    # Cache departing markets so orphaned positions can still be settled
                    for mid in departed:
                        old_mkt = self._active_markets.get(mid)
                        if old_mkt and mid not in self._expired_market_cache:
                            self._expired_market_cache[mid] = old_mkt
                        if old_mkt:
                            self.data_recorder.log_market_event(
                                time.time(), "dropped", mid, old_mkt.asset, old_mkt.timeframe_sec,
                                {
                                    "open_epoch": old_mkt.open_epoch,
                                    "close_epoch": old_mkt.close_epoch,
                                    "up_token_id": old_mkt.up_token_id,
                                    "dn_token_id": old_mkt.dn_token_id,
                                },
                            )
                self._active_markets = new_markets

            # Collect tokens still needed for pending settlements
            settlement_token_ids: list[str] = []
            for mid in self.position_manager.get_pending_settlements():
                mkt = self._expired_market_cache.get(mid)
                if mkt:
                    settlement_token_ids.extend([mkt.up_token_id, mkt.dn_token_id])
            # Also include tokens from positions (covers orphaned position detection)
            for mid in self.position_manager.positions:
                mkt = self._find_market(mid)
                if mkt:
                    settlement_token_ids.extend([mkt.up_token_id, mkt.dn_token_id])

            all_protected = all_token_ids + settlement_token_ids

            # Update subscriptions — set-based to prune stale entries
            self.midpoint_poller.set_tokens(all_protected)
            self.market_ws.update_subscriptions(all_token_ids)
            self.book_manager.set_active_tokens(all_protected)

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
                logger.info(
                    "EXPIRED_UNFILLED: %s closed with no position (no fills in window)",
                    mid,
                )
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

    _VALID_OUTCOMES = {"UP", "DOWN", "YES", "NO"}

    def _settle_position(self, mid: str, market: MarketWindow, outcome: str):
        """Settle a single position: compute PnL, update risk/bankroll, queue redemption."""
        # --- Outcome validation guard ---
        if outcome.upper() not in self._VALID_OUTCOMES:
            logger.critical(
                "INVALID OUTCOME '%s' for %s -- skipping settlement to prevent PnL corruption",
                outcome, mid,
            )
            return

        # --- Normalize: YES->UP, NO->DOWN ---
        normalized = outcome.upper()
        if normalized == "YES":
            normalized = "UP"
        elif normalized == "NO":
            normalized = "DOWN"

        pos = self.position_manager.positions.get(mid)
        if pos:
            # Pop any realized PnL from FV-exit SELLs that fired mid-window.
            # SELL proceeds already credited bankroll at fill time; this tracks the PnL
            # component (proceeds - cost_basis) so settlement records are accurate.
            realized_prior = self.ladder_manager._realized_in_window.pop(mid, 0.0)

            if normalized == "UP":
                settle_pnl = pos.profit_if_up()
                winning = pos.up_qty - pos.up_cost if pos.up_qty > 0 else 0.0
                losing = pos.dn_cost
            else:
                settle_pnl = pos.profit_if_down()
                winning = pos.dn_qty - pos.dn_cost if pos.dn_qty > 0 else 0.0
                losing = pos.up_cost

            # Total reported PnL = end-of-window delta + prior SELL realized PnL
            pnl = settle_pnl + realized_prior

            logger.info("Settled %s: %s, PnL=$%.2f (settle=$%.2f + prior_realized=$%.2f)",
                        mid, normalized, pnl, settle_pnl, realized_prior)
            self.risk.update_pnl(pnl)
            self._realized_pnl += pnl
            if not hasattr(self, '_per_asset_pnl'):
                self._per_asset_pnl = {}
                self._per_asset_pairs = {}
            self._per_asset_pnl[market.asset] = self._per_asset_pnl.get(market.asset, 0.0) + pnl
            self._per_asset_pairs[market.asset] = self._per_asset_pairs.get(market.asset, 0) + 1
            if pnl > 0:
                self._settled_wins += 1
            elif pnl < 0:
                self._settled_losses += 1
            # Only report pair_cost when position is reasonably balanced (< 3:1 ratio)
            min_qty = min(pos.up_qty, pos.dn_qty)
            max_qty = max(pos.up_qty, pos.dn_qty)
            is_balanced = min_qty > 0 and (max_qty / min_qty) < 3.0
            pair_cost_val = round(pos.pair_cost(), 3) if is_balanced else None
            if is_balanced:
                self._settled_pair_costs.append(pos.pair_cost())

            # Only update bankroll from end-of-window settle_pnl in paper mode.
            # SELL proceeds were already credited to bankroll at fill time;
            # adding settle_pnl here accounts only for the remaining position value.
            # In live mode, on-chain balance is the source of truth.
            if self.cfg.dry_run:
                self.position_manager.bankroll += settle_pnl

            if losing > 0:
                detail = f"{normalized} won \u2192 \u2191 +${winning:.2f} \u2193 -${losing:.2f} = net ${pnl:+.2f}"
            else:
                detail = f"{normalized} won \u2192 \u2191 +${winning:.2f} = net ${pnl:+.2f}"
            tf_label = {300: "5m", 900: "15m", 3600: "1h"}.get(
                market.timeframe_sec, f"{market.timeframe_sec}s"
            )
            settle_meta = {
                "timeframe": tf_label,
                "outcome": normalized,
                "up_qty": round(pos.up_qty, 2),
                "dn_qty": round(pos.dn_qty, 2),
                "up_cost": round(pos.up_cost, 2),
                "dn_cost": round(pos.dn_cost, 2),
                "total_cost": round(pos.up_cost + pos.dn_cost, 2),
                "revenue": round(pos.up_cost + pos.dn_cost + pnl, 2),
                "winning": round(winning, 2),
                "losing": round(losing, 2),
                "pair_cost": pair_cost_val,
            }
            self._record_activity("SETTLE", market.asset, detail, pnl=pnl, meta=settle_meta)

            # Track settlement history and PnL series for UI
            if not hasattr(self, '_settlement_history'):
                self._settlement_history = []
                self._pnl_series = []
            self._settlement_history.append({
                "ts": time.time(),
                "asset": market.asset,
                "timeframe": tf_label,
                "outcome": normalized,
                "pnl": round(pnl, 2),
                "up_qty": round(pos.up_qty, 1),
                "dn_qty": round(pos.dn_qty, 1),
                "total_cost": round(pos.up_cost + pos.dn_cost, 2),
                "pair_cost": pair_cost_val,
            })
            self._pnl_series.append({
                "ts": time.time(),
                "pnl": round(self._realized_pnl, 2),
            })

            # Persist settlement record to JSONL for future analysis
            self._log_settlement(market, settle_meta, pnl)

            # Log real market data for paper mode verification
            if self.cfg.dry_run and getattr(self, '_trading_active', False):
                try:
                    market_logger.log_settlement(
                        market_id=mid, asset=market.asset,
                        timeframe_sec=market.timeframe_sec,
                        up_token_id=market.up_token_id,
                        dn_token_id=market.dn_token_id,
                        paper_outcome=normalized,
                        spot_price=self.spot_prices.get(market.asset, 0.0),
                        open_price=self.window_open_prices.get(mid, 0.0),
                        pnl=pnl,
                        pair_cost=settle_meta.get("pair_cost"),
                        up_qty=pos.up_qty, dn_qty=pos.dn_qty,
                    )
                except Exception:
                    pass

            self.redeemer.queue_redemption(
                market.condition_id,
                [market.up_token_id, market.dn_token_id],
            )

            # Log market settlement event (inside pos block so pnl is available)
            try:
                self.data_recorder.log_market_event(
                    time.time(), "settled", mid, market.asset, market.timeframe_sec,
                    {
                        "open_epoch": market.open_epoch,
                        "close_epoch": market.close_epoch,
                        "outcome": normalized,
                        "pnl": round(pnl, 4),
                    },
                )
            except Exception:
                pass

        self.position_manager.complete_settlement(mid)
        self.position_manager.remove_position(mid)
        self._expired_market_cache.pop(mid, None)
        self._settled_markets.add(mid)  # prevent re-posting ladders

    def _log_settlement(self, market: MarketWindow, meta: dict, pnl: float):
        """Append settlement record to data/settlement_log.jsonl for analysis."""
        if not getattr(self, '_trading_active', False):
            return
        import json as _json
        record = {
            "ts": time.time(),
            "market_id": market.market_id,
            "asset": market.asset,
            "timeframe_sec": market.timeframe_sec,
            "pnl": round(pnl, 4),
            "bankroll": round(self.position_manager.bankroll, 2),
            "exposure_factor": self.risk.exposure_factor(),
            "consecutive_losses": self.risk.consecutive_losses,
            **meta,
        }
        try:
            import pathlib
            log_path = pathlib.Path("data/settlement_log.jsonl")
            log_path.parent.mkdir(exist_ok=True)
            with open(log_path, "a") as f:
                f.write(_json.dumps(record) + "\n")
        except Exception as e:
            logger.debug("Settlement log write failed: %s", e)

    def _dry_run_resolve(self, market: MarketWindow) -> str:
        """In dry-run mode, resolve using CLOB midpoint (matches Polymarket's Chainlink oracle).

        Primary: if UP midpoint > 0.5, outcome is UP (market agrees).
        Fallback: Binance spot delta if midpoints unavailable.
        """
        # Primary: use CLOB midpoint — this converges to Chainlink resolution
        up_mid = self.midpoint_poller.get_mid(market.up_token_id)
        if up_mid is not None:
            up_mid_f = float(up_mid)
            if up_mid_f > 0.5:
                return "UP"
            elif up_mid_f < 0.5:
                return "DOWN"
            # exactly 0.5 — fall through to spot delta

        # Fallback: Binance spot delta
        delta = self.compute_spot_delta(market.asset, market.market_id)
        if delta > 0:
            return "UP"
        elif delta < 0:
            return "DOWN"
        # If delta is exactly 0, flip a coin
        return random.choice(["UP", "DOWN"])

    async def _redeem_tokens(self, condition_id: str, token_ids: list[str]) -> float:
        """Redeem winning tokens on-chain. Returns USDC.e received.

        Paper mode: no-op (no real tokens). Live mode: log for manual redemption.
        """
        if self.cfg.dry_run:
            return 0.0  # paper mode — no real tokens to redeem
        # Live mode: log for manual redemption, don't raise (prevents retry spam)
        logger.warning(
            "REDEMPTION NEEDED: condition_id=%s — redeem manually via Polymarket UI",
            condition_id,
        )
        self._record_activity(
            "REDEEM", "SYSTEM",
            f"manual redemption needed: {condition_id}",
        )
        return 0.0  # operator redeems manually; balance poller picks up the USDC

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
                        # Back-propagate condition_id if returned by resolver
                        new_cid = result.get("condition_id")
                        if new_cid and new_cid != market.condition_id:
                            logger.info("Back-propagated condition_id for %s: %s", mid, new_cid)
                            market.condition_id = new_cid
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
                        logger.warning("Balance fetch failed 5x — entering cancel-only mode")
                        self._record_activity("ERROR", "SYSTEM", "Balance fetch failed 5x — entering cancel-only mode")
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

    def _log_strategy_states(self, now: int, active_list: list, fair_values: dict):
        """Log strategy state for each active market, throttled to every 5 seconds."""
        try:
            now_f = float(now)
            for market in active_list:
                mid = market.market_id
                last = self._last_strategy_log_ts.get(mid, 0)
                if now_f - last < 5.0:
                    continue
                self._last_strategy_log_ts[mid] = now_f

                pos = self.position_manager.positions.get(mid)
                fv_up, fv_vol = fair_values.get(mid, (0.5, None))

                # Count resting orders per side
                resting_up = len(self.order_tracker.get_resting_side(mid, Side.UP))
                resting_dn = len(self.order_tracker.get_resting_side(mid, Side.DOWN))

                elapsed_pct = 0.0
                if market.timeframe_sec > 0:
                    elapsed_pct = market.elapsed(now) / market.timeframe_sec

                data = {
                    "p_up": round(fv_up, 4),
                    "certainty": round(fv_certainty(fv_up), 4),
                    "vol": round(fv_vol, 4) if fv_vol is not None else None,
                    "phase": self._get_strategy_phase(market),
                    "up_qty": round(pos.up_qty, 2) if pos else 0,
                    "dn_qty": round(pos.dn_qty, 2) if pos else 0,
                    "up_cost": round(pos.up_cost, 2) if pos else 0,
                    "dn_cost": round(pos.dn_cost, 2) if pos else 0,
                    "resting_up": resting_up,
                    "resting_dn": resting_dn,
                    "spot_price": self.spot_prices.get(market.asset, 0.0),
                    "spot_delta": self.compute_spot_delta(market.asset, mid),
                    "elapsed_pct": round(elapsed_pct, 3),
                    "bankroll": round(self.position_manager.bankroll, 2),
                }
                self.data_recorder.log_strategy_state(now_f, mid, market.asset, data)
        except Exception as e:
            logger.debug("strategy_log failed: %s", e, exc_info=True)  # never crash the bot on logging

    def compute_spot_delta(self, asset: str, market_id: str) -> float:
        current = self.spot_prices.get(asset, 0.0)
        open_price = self.window_open_prices.get(market_id, 0.0)
        if open_price <= 0:
            return 0.0
        return (current - open_price) / open_price

    def _on_price_tick(self, asset: str, price) -> None:
        """Feed price ticks to vol estimator and data recorder."""
        now = time.time()
        ve = self._vol_estimators.get(asset)
        if ve:
            ve.push(now, price)
        self.data_recorder.log_price(now, asset, float(price), "binance")

    def compute_fair_value(self, market) -> tuple[float, float]:
        """Return (p_up, vol_annualized) for the given market window.

        PTB fallback chain: Chainlink RTDS → market.price_to_beat → window_open_prices.
        Chainlink is authoritative because Polymarket resolves on Chainlink, not Binance.
        Returns (0.5, fallback_vol) when data is insufficient.
        """
        fallback_vol = self.cfg.vol_fallback_annual

        # Get Price to Beat — prefer Chainlink (resolution source)
        ptb = None
        ptb_source = None

        # 1. Chainlink RTDS historical lookup at window open
        if hasattr(market, 'open_epoch') and market.open_epoch:
            cl_price = self.rtds_feed.price_at_timestamp(market.asset, market.open_epoch)
            if cl_price is not None and float(cl_price) > 0:
                ptb = float(cl_price)
                ptb_source = "chainlink"

        # 2. Gamma API price_to_beat
        if ptb is None and hasattr(market, 'price_to_beat') and market.price_to_beat:
            try:
                ptb = float(market.price_to_beat)
                if ptb <= 0:
                    ptb = None
                else:
                    ptb_source = "gamma"
            except (ValueError, TypeError):
                ptb = None

        # 3. Binance spot snapshot at window open
        if ptb is None:
            ptb = self.window_open_prices.get(market.market_id)
            if ptb is not None and ptb > 0:
                ptb_source = "binance"

        if ptb is None or ptb <= 0:
            return (0.5, fallback_vol)

        # Log PTB source once per market for monitoring
        if market.market_id not in self._ptb_logged:
            self._ptb_logged.add(market.market_id)
            binance_snap = self.window_open_prices.get(market.market_id, 0.0)
            diff = ptb - binance_snap if binance_snap > 0 else 0.0
            logger.info(
                "PTB SOURCE: %s src=%s ptb=$%.2f (binance=$%.2f, diff=$%.2f)",
                market.market_id, ptb_source, ptb, binance_snap, diff,
            )

        # Get current price
        current = self.spot_prices.get(market.asset, 0.0)
        if current <= 0:
            return (0.5, fallback_vol)

        # Get vol
        ve = self._vol_estimators.get(market.asset)
        vol = ve.vol_annualized(self.cfg.vol_window_sec) if ve else fallback_vol

        # Get seconds left
        now = int(time.time())
        secs_left = market.remaining(now)

        p_up = p_fair_up(ptb, current, secs_left, vol)
        return (p_up, vol)

    def _get_strategy_phase(self, market) -> str:
        """Return 'bilateral', 'skewed', or 'directional' based on elapsed fraction."""
        now = int(time.time())
        if market.timeframe_sec <= 0:
            return "bilateral"
        elapsed_frac = market.elapsed(now) / market.timeframe_sec
        if elapsed_frac < self.cfg.skew_phase_pct:
            return "bilateral"
        elif elapsed_frac < self.cfg.directional_phase_pct:
            return "skewed"
        else:
            return "directional"

    def _snapshot_window_open_prices(self):
        """Capture open prices for each new market window.

        Immediately applies the spot-price fallback (synchronous). Also returns an
        awaitable coroutine that attempts to upgrade snapshots with Binance candle
        open prices. Callers that run in an async context should await the return
        value; synchronous callers can ignore it.

        Returns an awaitable for backward compatibility with ``asyncio.run()`` callers.
        """
        # --- Synchronous spot-price fallback (runs immediately) ---
        now = int(time.time())
        for market in list(self._active_markets.values()):
            if market.market_id in self._snapped_windows:
                continue
            if not market.is_active(now):
                continue

            # Use spot price from our feed (gated on freshness)
            fresh_price = self.price_feed.get_price_if_fresh(
                market.asset, self.cfg.price_snap_stale_sec
            )
            spot = float(fresh_price) if fresh_price is not None else 0.0
            if spot > 0:
                self.window_open_prices[market.market_id] = spot
                self._snapped_windows.add(market.market_id)
                logger.info(
                    "SNAPSHOT: %s spot = $%.2f for window %s",
                    market.asset, spot, market.market_id,
                )
            else:
                age = self.price_feed.get_price_age(market.asset)
                logger.warning(
                    "SNAP DEFERRED: %s price stale (age=%.1fs, max=%.1fs) for window %s",
                    market.asset,
                    age if age is not None else -1,
                    self.cfg.price_snap_stale_sec,
                    market.market_id,
                )

        # --- Async upgrade with Binance candle opens ---
        return self._snapshot_upgrade_candles()

    async def _snapshot_upgrade_candles(self):
        """Attempt to upgrade spot snapshots with authoritative Binance candle opens.

        Called via the return value of _snapshot_window_open_prices() in async contexts.
        """
        now = int(time.time())
        for market in list(self._active_markets.values()):
            if market.market_id in self._snapped_windows:
                # Already snapped — no need to re-fetch
                continue
            else:
                if not market.is_active(now):
                    continue

            # Determine Binance interval from timeframe
            if market.timeframe_sec <= 300:
                interval = "5m"
            elif market.timeframe_sec <= 900:
                interval = "15m"
            else:
                interval = "1h"

            # Try Binance candle open (authoritative for resolution)
            candle_open = await self.price_feed.fetch_binance_candle_open(
                market.asset, interval, market.open_epoch
            )
            if candle_open and candle_open > 0:
                self.window_open_prices[market.market_id] = candle_open
                self._snapped_windows.add(market.market_id)
                logger.info(
                    "SNAPSHOT: %s candle open = $%.2f for window %s (%s)",
                    market.asset, candle_open, market.market_id, interval,
                )

    def _cleanup_expired_windows(self, now_epoch: int):
        """Remove window IDs and stale ladders no longer in the active market list."""
        active_ids = {m.market_id for m in self._active_markets.values()}
        stale = self._snapped_windows - active_ids
        for mid in stale:
            self._snapped_windows.discard(mid)
            self._ptb_logged.discard(mid)
            self.window_open_prices.pop(mid, None)

        # Clean up settled market markers when markets leave active list
        stale_settled = self._settled_markets - active_ids
        self._settled_markets -= stale_settled

        # Clean up ladders for markets that are no longer active
        stale_ladders = set(self.ladder_manager.ladders.keys()) - active_ids
        for mid in stale_ladders:
            if mid not in self.position_manager.get_pending_settlements():
                self.ladder_manager.cleanup_ladder(mid)

        # Prune expired market cache: remove entries with no position and not pending
        pending = set(self.position_manager.get_pending_settlements())
        has_position = set(self.position_manager.positions.keys())
        stale_expired = [
            mid for mid in self._expired_market_cache
            if mid not in pending and mid not in has_position
        ]
        for mid in stale_expired:
            self._expired_market_cache.pop(mid)

        # Evict stale tick size cache entries
        self.tick_cache.evict_stale()

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
                # Find nearest-expiry active window for this asset to compute delta
                best_mid = None
                best_remaining = float("inf")
                for mkt in self._active_markets.values():
                    if mkt.asset == asset and mkt.is_active(now_epoch):
                        rem = mkt.remaining(now_epoch)
                        if rem < best_remaining:
                            best_remaining = rem
                            best_mid = mkt.market_id
                if best_mid is not None:
                    delta = self.compute_spot_delta(asset, best_mid)
                    spot_parts.append(f"{asset}=${price:,.2f}({delta:+.3%})")
                else:
                    spot_parts.append(f"{asset}=${price:,.2f}")
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
        self, event_type: str, asset: str, detail: str, pnl: float | None = None, meta: dict | None = None
    ):
        ts = time.time()
        self._activity_log.append(
            ActivityEvent(
                timestamp=ts,
                event_type=event_type,
                asset=asset,
                detail=detail,
                pnl=pnl,
                meta=meta,
            )
        )
        # Persist all activity to JSONL for analysis
        self._persist_activity(ts, event_type, asset, detail, pnl, meta)

    def _persist_activity(self, ts: float, event_type: str, asset: str, detail: str,
                          pnl: float | None, meta: dict | None):
        """Append activity event to data/activity_log.jsonl."""
        if not getattr(self, '_trading_active', False):
            return  # don't log during tests or before trading starts
        import json as _json
        import pathlib
        record = {
            "ts": ts,
            "type": event_type,
            "asset": asset,
            "detail": detail,
            "bankroll": round(self.position_manager.bankroll, 2),
        }
        if pnl is not None:
            record["pnl"] = round(pnl, 4)
        if meta:
            record.update(meta)
        try:
            log_path = pathlib.Path("data/activity_log.jsonl")
            log_path.parent.mkdir(exist_ok=True)
            with open(log_path, "a") as f:
                f.write(_json.dumps(record) + "\n")
        except Exception:
            pass  # never block trading on log writes

    # ------------------------------------------------------------------
    # State snapshot for GUI
    # ------------------------------------------------------------------

    def build_state_snapshot(self) -> dict:
        """Returns dict matching GUI state spec with all 24 fields."""
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

            # Per-window spot delta (keyed by market_id)
            spot_delta = None
            if mkt.market_id in self.window_open_prices and current_price:
                spot_delta = self.compute_spot_delta(mkt.asset, mkt.market_id)

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
            budget = round(lp.position_size_fraction * self.position_manager.bankroll, 2)
            rungs_filled = 0
            imbalance = None
            resting_up = 0
            resting_dn = 0
            if ladder:
                try:
                    stats = self.ladder_manager.get_ladder_stats(mkt.market_id)
                    if stats:
                        rungs_filled = stats.get("up_filled_count", 0) + stats.get("dn_filled_count", 0)
                        imbalance = stats.get("imbalance")
                        resting_up = stats.get("up_resting", 0)
                        resting_dn = stats.get("dn_resting", 0)
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
                    "profit_if_up": pos.profit_if_up(),
                    "profit_if_down": pos.profit_if_down(),
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
                "resting_up": resting_up,
                "resting_dn": resting_dn,
                "budget": budget,
                "imbalance": imbalance,
                "position": position,
                "window_status": window_status,
                "opens_in_sec": opens_in_sec,
                "spot_delta": spot_delta,
                "price_to_beat": _ui_price_to_beat(
                    mkt,
                    self.rtds_feed.price_at_timestamp(mkt.asset, mkt.open_epoch),
                    self.window_open_prices.get(mkt.market_id, 0),
                ),
            }

            # Fair value data
            if self.cfg.fair_value_enabled:
                fv_up, fv_vol = self.compute_fair_value(mkt)
                info["fair_value_up"] = round(fv_up, 4)
                info["fair_value_vol"] = round(fv_vol, 4) if fv_vol else None
                info["fair_value_certainty"] = round(fv_certainty(fv_up), 4)
                info["strategy_phase"] = self._get_strategy_phase(mkt)

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
            "per_asset_pnl": {k: round(v, 2) for k, v in getattr(self, '_per_asset_pnl', {}).items()},
            "per_asset_pairs": dict(getattr(self, '_per_asset_pairs', {})),
            "settlement_history": getattr(self, '_settlement_history', []),
            "pnl_series": getattr(self, '_pnl_series', []),
            "trade_count": self._trade_count,
            "position_count": self.position_manager.active_position_count(),
            "pairs_completed": self._settled_wins + self._settled_losses,
            "avg_pair_cost": (
                sum(self._settled_pair_costs) / len(self._settled_pair_costs)
                if self._settled_pair_costs else 0.0
            ),
            "best_pair_cost": (
                min(self._settled_pair_costs)
                if self._settled_pair_costs else 0.0
            ),
            "imbalance_ratio": 0.0,
            "runtime_sec": (
                int(time.time() - self._start_time) if self._start_time else 0
            ),
            "markets_active": len(self._active_markets),
            "win_rate": (
                self._settled_wins / (self._settled_wins + self._settled_losses)
                if (self._settled_wins + self._settled_losses) > 0 else 0.0
            ),
            "settled_wins": self._settled_wins,
            "settled_losses": self._settled_losses,
            "consecutive_losses": self.risk.consecutive_losses,
            "exposure_factor": self.risk.exposure_factor(),
            "daily_pnl": round(self.risk.daily_pnl, 2),
            "is_halted": self.risk.is_halted(),
            "capital_at_risk_pct": round(
                self.ladder_manager.total_committed() / max(self.position_manager.bankroll, 1) * 100, 1
            ),
            "prices": polymarket_prices,
            "binance_prices": binance_prices,
            "spots": spots,
            "binance_spot_values": _ui_binance_spot_values(binance_prices, spots),
            "active_markets": active_markets_info,
            "activity_feed": [
                {
                    "ts": e.timestamp,
                    "kind": e.event_type,
                    "asset": e.asset,
                    "msg": f"[{e.asset}] {e.detail}" if e.asset else e.detail,
                    "pnl": e.pnl,
                    "meta": e.meta,
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
            "usdc_balance": (
                self.position_manager.bankroll
                if self.cfg.dry_run
                else self._wallet_balance
            ),
            "price_feed_stale": not self.price_feed.is_fresh(self.cfg.price_stale_sec),
        }

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
from polybot.ladder_manager import LadderManager
from polybot.market_discovery import discover_active_markets
from polybot.order_executor import OrderExecutor
from polybot.order_tracker import OrderTracker
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
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
        self.ladder_manager = LadderManager(
            cfg,
            order_executor=self.order_executor,
            order_tracker=self.order_tracker,
            position_manager=self.position_manager,
            risk_manager=self.risk_manager,
        )

        self.spot_prices: dict[str, float] = {}
        self.window_open_prices: dict[str, float] = {}
        self.active_markets: list[MarketWindow] = []
        self._snapped_windows: set[str] = set()
        self._exited_markets: set[str] = set()  # markets we already exited — don't re-enter
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
        """Remove window IDs no longer in the active market list."""
        active_ids = {m.market_id for m in self.active_markets}
        stale = self._snapped_windows - active_ids
        for mid in stale:
            self._snapped_windows.discard(mid)
        self._exited_markets -= stale  # allow re-entry on future windows

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
        from polybot.display import ActivityEvent
        self._activity_log.append(ActivityEvent(
            timestamp=time.time(),
            event_type=event_type,
            asset=asset,
            detail=detail,
            pnl=pnl,
        ))

    def _settle_expired_windows(self, now_epoch: int):
        """Handle settlement for expired windows with positions."""
        settled_ids = []
        for market in list(self.active_markets):
            if market.is_active(now_epoch):
                continue
            if market.market_id not in self.position_manager.positions:
                # No position but might have a ladder — clean it up
                self.ladder_manager.cleanup_ladder(market.market_id)
                continue

            pos = self.position_manager.positions[market.market_id]
            spot_delta = self.compute_spot_delta(market.asset)
            if spot_delta > 0:
                pnl = pos.profit_if_up()
                winner = "UP"
            elif spot_delta < 0:
                pnl = pos.profit_if_down()
                winner = "DOWN"
            else:
                pnl = min(pos.profit_if_up(), pos.profit_if_down())
                winner = "FLAT"

            old_bankroll = self.position_manager.bankroll
            self.position_manager.update_bankroll(old_bankroll + pnl)
            self.risk_manager.update_pnl(pnl)
            self.position_manager.remove_position(market.market_id)
            self.ladder_manager.cleanup_ladder(market.market_id)
            settled_ids.append(market.market_id)

            logger.info(
                "SETTLEMENT: %s | winner=%s delta=%.4f | "
                "up_qty=%.1f dn_qty=%.1f | pnl=$%.2f | "
                "bankroll: $%.2f -> $%.2f",
                market.market_id, winner, spot_delta,
                pos.up_qty, pos.dn_qty, pnl,
                old_bankroll, self.position_manager.bankroll,
            )
            self._record_activity(
                "SETTLE", market.asset,
                f"winner={winner} delta={spot_delta:+.4f} bankroll=${self.position_manager.bankroll:,.2f}",
                pnl=pnl,
            )
            self._trade_count += 1

        # Cleanup snapshots for settled markets
        for mid in settled_ids:
            self._snapped_windows.discard(mid)

    def _check_stop_losses(self, now_epoch: int):
        """Stop-loss check — removed, will be replaced by settlement-aware logic."""
        pass

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

            # 1. Post ladders on new markets (no existing ladder, not already exited)
            for market in self.active_markets:
                if not market.is_active(now):
                    continue
                if self.ladder_manager.has_ladder(market.market_id):
                    continue
                if market.market_id in self._exited_markets:
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

            # 2. Check fills on all active ladders
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

            # 3. Reprice if book moved
            await asyncio.to_thread(
                self.ladder_manager.reprice_if_needed, market_map
            )

            # 4. Imbalance guard
            self.ladder_manager.check_imbalance(now)

            # 5. Early exit check
            exits = await asyncio.to_thread(
                self.ladder_manager.check_early_exits, market_map
            )
            for ex in exits:
                self._exited_markets.add(ex["market_id"])
                self._record_activity(
                    "EARLY_EXIT", ex["asset"],
                    f"sold {ex['exit_side'].value}@${ex['sell_price']:.2f}",
                    pnl=ex["pnl"],
                )
                self._trade_count += 1

            # 6. Cancel rungs on expiring windows
            for market in self.active_markets:
                if market.is_active(now) and market.remaining(now) < self.cfg.no_trade_final_sec:
                    if self.ladder_manager.has_ladder(market.market_id):
                        cancelled = self.ladder_manager.cancel_ladder(market.market_id)
                        if cancelled > 0:
                            self._record_activity(
                                "CANCEL", market.asset,
                                f"cancelled {cancelled} unfilled rungs (window expiring)",
                            )

            # 7. Settlement
            self._settle_expired_windows(now)

            # 8. Cleanup expired window snapshots
            self._cleanup_expired_windows(now)

            # 9. Stop-loss on one-sided positions
            self._check_stop_losses(now)

            await asyncio.sleep(self.cfg.poll_interval_ms / 1000.0)

    async def run_display(self):
        """Live Rich dashboard, refreshing at 1 Hz."""
        from rich.console import Console
        from rich.live import Live
        from polybot.display import build_display

        console = Console()
        with Live(build_display(self), console=console, refresh_per_second=1) as live:
            while True:
                await asyncio.sleep(1)
                live.update(build_display(self))

    async def run(self):
        """Start all concurrent tasks."""
        logger.info(
            "Bot starting — bankroll: $%.2f, assets: %s, ladder: %d rungs @ $%.2f spacing",
            self.position_manager.bankroll, self.cfg.assets,
            self.cfg.ladder_rungs, self.cfg.ladder_spacing,
        )
        tasks = [
            asyncio.create_task(self.run_binance_ws()),
            asyncio.create_task(self.run_market_discovery()),
            asyncio.create_task(self.run_trading_loop()),
            asyncio.create_task(self.run_display()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        finally:
            self.order_executor.cancel_all()
            for t in tasks:
                t.cancel()

"""Main bot: wires signal engine, position manager, risk manager, and order executor.

All synchronous CLOB client calls (order placement, book queries) are dispatched
via asyncio.to_thread() so the Binance WebSocket feed is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from polybot.config import BotConfig
from polybot.market_discovery import discover_active_markets
from polybot.order_executor import OrderExecutor
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.signal_engine import SignalEngine
from polybot.types import MarketWindow, Side, StrategyType

logger = logging.getLogger(__name__)

# Fee model: fee = baseRate * min(P, 1-P). Polymarket 15m crypto markets.
FEE_BASE_RATE = 0.02

# How often to print a status summary (seconds)
STATUS_INTERVAL_SEC = 30


def compute_fee(price: float) -> float:
    """Compute per-share fee using Polymarket's fee schedule."""
    return FEE_BASE_RATE * min(price, 1.0 - price)


class Bot:
    def __init__(self, cfg: BotConfig, clob_client, initial_bankroll: float):
        self.cfg = cfg
        self.clob_client = clob_client
        self.signal_engine = SignalEngine(cfg)
        self.position_manager = PositionManager(cfg, bankroll=initial_bankroll)
        self.risk_manager = RiskManager(cfg, starting_bankroll=initial_bankroll)
        self.order_executor = OrderExecutor(cfg, clob_client=clob_client)

        self.spot_prices: dict[str, float] = {}
        self.window_open_prices: dict[str, float] = {}
        self.active_markets: list[MarketWindow] = []
        self._snapped_windows: set[str] = set()
        self._last_status_time: float = 0.0
        self._trade_count: int = 0

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

    def _log_status(self, now_epoch: int):
        """Print a periodic status summary."""
        if now_epoch - self._last_status_time < STATUS_INTERVAL_SEC:
            return
        self._last_status_time = now_epoch

        # Spot prices
        spot_parts = []
        for asset in self.cfg.assets:
            price = self.spot_prices.get(asset, 0.0)
            if price > 0:
                delta = self.compute_spot_delta(asset)
                spot_parts.append(f"{asset}=${price:,.2f}({delta:+.3%})")
        spot_str = " | ".join(spot_parts) if spot_parts else "waiting for prices..."

        # Positions
        pos_count = self.position_manager.active_position_count()
        pos_parts = []
        for mid, pos in self.position_manager.positions.items():
            sides = []
            if pos.up_qty > 0:
                sides.append(f"UP:{pos.up_qty:.0f}@{pos.up_cost/max(pos.up_qty,1):.2f}")
            if pos.dn_qty > 0:
                sides.append(f"DN:{pos.dn_qty:.0f}@{pos.dn_cost/max(pos.dn_qty,1):.2f}")
            short_id = mid.split("_")[-1] if "_" in mid else mid[-8:]
            pos_parts.append(f"{short_id}[{','.join(sides)}]")
        pos_str = " ".join(pos_parts) if pos_parts else "none"

        logger.info(
            "STATUS | bankroll=$%.2f | pnl=$%.2f | positions=%d: %s | trades=%d | %s",
            self.position_manager.bankroll,
            self.risk_manager.daily_pnl,
            pos_count,
            pos_str,
            self._trade_count,
            spot_str,
        )

    def evaluate_market(
        self, market: MarketWindow, now_epoch: int
    ) -> list[dict]:
        """Evaluate a single market for trading opportunities. Returns list of actions taken."""
        actions = []

        if self.risk_manager.is_halted():
            return actions
        if not self.risk_manager.can_trade_in_window(market, now_epoch):
            return actions
        if not self.risk_manager.can_open_position(
            self.position_manager.active_position_count()
        ):
            return actions

        spot_delta = self.compute_spot_delta(market.asset)

        # Fetch best asks for both sides
        best_ask_up = self.order_executor.get_best_ask(market.up_token_id)
        best_ask_dn = self.order_executor.get_best_ask(market.dn_token_id)
        best_asks = {"UP": best_ask_up, "DOWN": best_ask_dn}

        existing_pos = self.position_manager.positions.get(market.market_id)
        has_spread = existing_pos and existing_pos.up_qty > 0 and existing_pos.dn_qty > 0
        has_directional = existing_pos and (existing_pos.up_qty > 0) != (existing_pos.dn_qty > 0)

        # Priority 1: Directional (latency arb)
        # Can fire if no position, or if only a spread position exists (adds directional on top)
        if not has_directional:
            dir_opp = self.signal_engine.check_directional(
                market, spot_delta, best_asks, now_epoch
            )
            if dir_opp is not None:
                fee = compute_fee(dir_opp.price)
                net_edge = dir_opp.edge - fee
                if net_edge > 0:
                    token_id = (
                        market.up_token_id
                        if dir_opp.side == Side.UP
                        else market.dn_token_id
                    )
                    book_depth = self.order_executor.get_book_depth_at_price(
                        token_id, dir_opp.price
                    )
                    sizing = self.position_manager.compute_order_size(dir_opp, book_depth)
                    if sizing is not None:
                        side, qty = sizing
                        record = self.order_executor.place_limit_buy(
                            token_id=token_id,
                            price=dir_opp.price,
                            size=qty,
                            market_id=market.market_id,
                            side=side,
                        )
                        if record.status != "error":
                            cost = qty * dir_opp.price
                            self.position_manager.update_position(
                                market.market_id, side, qty, cost
                            )
                            actions.append({
                                "type": "directional",
                                "side": side,
                                "price": dir_opp.price,
                                "qty": qty,
                                "order_id": record.order_id,
                            })
                            self._trade_count += 1
                    return actions  # Don't also do spread if directional fires

        # Priority 2: Spread capture — only if no existing position at all
        if existing_pos is None:
            spread_opp = self.signal_engine.check_spread(market, best_asks, now_epoch)
            if spread_opp is not None:
                worst_fee = compute_fee(0.50)
                net_edge = spread_opp.edge - worst_fee
                if net_edge > 0:
                    sizing = self.position_manager.compute_spread_size(spread_opp)
                    if sizing is not None:
                        up_qty, dn_qty = sizing
                        record_up = self.order_executor.place_limit_buy(
                            token_id=market.up_token_id,
                            price=spread_opp.up_price,
                            size=up_qty,
                            market_id=market.market_id,
                            side=Side.UP,
                        )
                        record_dn = self.order_executor.place_limit_buy(
                            token_id=market.dn_token_id,
                            price=spread_opp.dn_price,
                            size=dn_qty,
                            market_id=market.market_id,
                            side=Side.DOWN,
                        )
                        if record_up.status != "error":
                            self.position_manager.update_position(
                                market.market_id, Side.UP, up_qty,
                                up_qty * spread_opp.up_price,
                            )
                        if record_dn.status != "error":
                            self.position_manager.update_position(
                                market.market_id, Side.DOWN, dn_qty,
                                dn_qty * spread_opp.dn_price,
                            )
                        actions.append({
                            "type": "spread",
                            "up_price": spread_opp.up_price,
                            "dn_price": spread_opp.dn_price,
                            "qty": up_qty,
                        })
                        self._trade_count += 1

        return actions

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
                # Log when windows rotate
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
        """Main trading loop: evaluate all active markets on each tick."""
        while True:
            now = int(time.time())

            # Snapshot open prices for new windows
            self._snapshot_window_open_prices()

            # Status logging
            self._log_status(now)

            for market in self.active_markets:
                if not market.is_active(now):
                    continue
                try:
                    actions = await asyncio.to_thread(
                        self.evaluate_market, market, now
                    )
                    for action in actions:
                        logger.info("ACTION: %s on %s", action, market.market_id)
                except Exception as e:
                    logger.error(
                        "Error evaluating %s: %s", market.market_id, e
                    )

            # Handle settlement: estimate PnL and update bankroll
            settled_ids = []
            for market in list(self.active_markets):
                now = int(time.time())
                if not market.is_active(now) and market.market_id in self.position_manager.positions:
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
                    settled_ids.append(market.market_id)

                    logger.info(
                        "SETTLEMENT: %s | winner=%s delta=%.4f | "
                        "up_qty=%.1f dn_qty=%.1f | pnl=$%.2f | "
                        "bankroll: $%.2f -> $%.2f",
                        market.market_id, winner, spot_delta,
                        pos.up_qty, pos.dn_qty, pnl,
                        old_bankroll, self.position_manager.bankroll,
                    )

            # Cleanup expired window snapshots
            self._cleanup_expired_windows(now)
            # Also cleanup snapped windows for settled markets so new windows get fresh snapshots
            for mid in settled_ids:
                self._snapped_windows.discard(mid)

            # Stop-loss check: for directional positions, check reversal
            for market in self.active_markets:
                if not market.is_active(int(time.time())):
                    continue
                if market.market_id not in self.position_manager.positions:
                    continue
                pos = self.position_manager.positions[market.market_id]
                is_directional = (pos.up_qty > 0) != (pos.dn_qty > 0)
                if not is_directional:
                    continue
                spot_delta = self.compute_spot_delta(market.asset)
                holding_up = pos.up_qty > 0
                if holding_up and spot_delta < -self.cfg.stop_loss_reversal:
                    logger.warning(
                        "STOP LOSS: %s — holding UP but delta=%.4f, exiting",
                        market.market_id, spot_delta,
                    )
                    self.position_manager.remove_position(market.market_id)
                elif not holding_up and spot_delta > self.cfg.stop_loss_reversal:
                    logger.warning(
                        "STOP LOSS: %s — holding DOWN but delta=+%.4f, exiting",
                        market.market_id, spot_delta,
                    )
                    self.position_manager.remove_position(market.market_id)

            await asyncio.sleep(self.cfg.poll_interval_ms / 1000.0)

    async def run(self):
        """Start all concurrent tasks."""
        logger.info(
            "Bot starting — bankroll: $%.2f, assets: %s",
            self.position_manager.bankroll, self.cfg.assets,
        )
        tasks = [
            asyncio.create_task(self.run_binance_ws()),
            asyncio.create_task(self.run_market_discovery()),
            asyncio.create_task(self.run_trading_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        finally:
            self.order_executor.cancel_all()
            for t in tasks:
                t.cancel()

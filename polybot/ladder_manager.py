"""Ladder manager: builds and maintains passive limit order ladders on every active market."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.order_executor import OrderExecutor
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.tick_size_cache import round_to_tick
from polybot.types import MarketWindow, Side

logger = logging.getLogger(__name__)


MIN_REPRICE_INTERVAL = 5.0  # seconds between reprices for the same market


@dataclass
class LadderState:
    market_id: str
    asset: str
    anchor_up: float
    anchor_dn: float
    posted_at: float
    last_reprice_at: float = 0.0
    imbalance_alert_at: float | None = None
    boosted_side: Side | None = None
    imbalance_accepted: bool = False


def build_ladder_rungs(
    best_ask: float,
    budget: float,
    rungs: int,
    spacing: float,
    width: float,
    size_skew: float,
    tick_size: float = 0.01,
) -> list[tuple[float, float]]:
    """Build ladder rungs as (price, size) pairs.

    Returns rungs from cheapest (farthest from market) to most expensive (near market).
    """
    anchor = max(0.01, best_ask - width)
    prices = [round_to_tick(anchor + i * spacing, tick_size) for i in range(rungs)]
    # Clamp prices to valid range
    prices = [max(0.01, min(0.99, p)) for p in prices]

    # Linear size skew: cheapest gets weight 1.0, most expensive gets weight size_skew
    weights = [1.0 + (size_skew - 1.0) * (i / max(rungs - 1, 1)) for i in range(rungs)]

    # Compute sizes so total cost = budget
    total_weighted_cost = sum(w * p for w, p in zip(weights, prices))
    if total_weighted_cost <= 0:
        return []

    scale = budget / total_weighted_cost
    result = []
    for price, weight in zip(prices, weights):
        size = scale * weight
        if size >= 5.0:  # Polymarket minimum for GTC orders
            result.append((price, round(size, 1)))

    return result


class LadderManager:
    def __init__(
        self,
        cfg: BotConfig,
        order_executor: OrderExecutor,
        order_tracker: OrderTracker,
        position_manager: PositionManager,
        risk_manager: RiskManager,
        tick_size_cache=None,
    ):
        self.cfg = cfg
        self.executor = order_executor
        self.tracker = order_tracker
        self.positions = position_manager
        self.risk = risk_manager
        self.ladders: dict[str, LadderState] = {}
        self.tick_cache = tick_size_cache

    def has_ladder(self, market_id: str) -> bool:
        return market_id in self.ladders

    def total_committed(self) -> float:
        """Total capital committed: resting order cost + filled position cost."""
        resting = 0.0
        for mid in self.ladders:
            for order in self.tracker.get_resting(mid):
                resting += order.price * (order.size - order.filled)
        return resting + self.positions.total_position_cost()

    def post_ladder(self, market: MarketWindow) -> int:
        """Post a full ladder (both sides) for a market. Returns number of orders placed."""
        try:
            if self.risk.is_halted():
                return 0
            if not self.risk.can_open_position(self.positions.active_position_count()):
                return 0

            # Select timeframe-specific ladder parameters
            lp = self.cfg.get_ladder_params(market.timeframe_sec)

            # Don't commit more capital than available bankroll
            available = self.positions.bankroll - self.total_committed()
            budget = min(
                self.positions.bankroll * lp.position_size_fraction,
                available,
            )
            if budget < 1.0:
                return 0

            tick_size = self.tick_cache.get_tick_size(market.condition_id, token_id=market.up_token_id) if self.tick_cache else 0.01

            best_ask_up = self.executor.get_best_ask(market.up_token_id)
            best_ask_dn = self.executor.get_best_ask(market.dn_token_id)

            # Dynamic budget skew: allocate more to the cheaper side
            # Data shows whale puts ~60% on cheaper side, with higher skew at lower pair costs
            if best_ask_up > 0 and best_ask_dn > 0:
                total_ask = best_ask_up + best_ask_dn
                # Invert: cheaper side gets larger share
                up_weight = (1.0 - best_ask_up / total_ask)
                dn_weight = (1.0 - best_ask_dn / total_ask)
                total_weight = up_weight + dn_weight
                budget_up = budget * (up_weight / total_weight)
                budget_dn = budget * (dn_weight / total_weight)
            else:
                budget_up = budget / 2.0
                budget_dn = budget / 2.0

            up_rungs = build_ladder_rungs(
                best_ask_up, budget_up,
                lp.rungs, lp.spacing, lp.width, lp.size_skew,
                tick_size=tick_size,
            )
            dn_rungs = build_ladder_rungs(
                best_ask_dn, budget_dn,
                lp.rungs, lp.spacing, lp.width, lp.size_skew,
                tick_size=tick_size,
            )

            # Pair cost guard: check if worst-case combined VWAP exceeds max_pair_cost
            if up_rungs and dn_rungs:
                up_vwap = sum(p * s for p, s in up_rungs) / sum(s for _, s in up_rungs)
                dn_vwap = sum(p * s for p, s in dn_rungs) / sum(s for _, s in dn_rungs)
                if up_vwap + dn_vwap > lp.max_pair_cost:
                    logger.info(
                        "Pair cost guard: %s combined VWAP %.4f > %.4f, skipping",
                        market.market_id, up_vwap + dn_vwap, lp.max_pair_cost,
                    )
                    return 0

            now = time.time()
            count = 0

            # Build batch order list for UP side
            up_order_dicts = [
                {"token_id": market.up_token_id, "price": price, "size": size,
                 "market_id": market.market_id, "side": Side.UP}
                for price, size in up_rungs
            ]
            # Build batch order list for DN side
            dn_order_dicts = [
                {"token_id": market.dn_token_id, "price": price, "size": size,
                 "market_id": market.market_id, "side": Side.DOWN}
                for price, size in dn_rungs
            ]

            for record in self.executor.place_batch_limit_buys(up_order_dicts):
                if record.status != "error":
                    self.tracker.add(TrackedOrder(
                        order_id=record.order_id,
                        market_id=market.market_id,
                        token_id=market.up_token_id,
                        side=Side.UP,
                        price=record.price, size=record.size,
                        placed_at=now,
                    ))
                    count += 1

            for record in self.executor.place_batch_limit_buys(dn_order_dicts):
                if record.status != "error":
                    self.tracker.add(TrackedOrder(
                        order_id=record.order_id,
                        market_id=market.market_id,
                        token_id=market.dn_token_id,
                        side=Side.DOWN,
                        price=record.price, size=record.size,
                        placed_at=now,
                    ))
                    count += 1

            anchor_up = up_rungs[-1][0] if up_rungs else best_ask_up
            anchor_dn = dn_rungs[-1][0] if dn_rungs else best_ask_dn

            self.ladders[market.market_id] = LadderState(
                market_id=market.market_id,
                asset=market.asset,
                anchor_up=anchor_up,
                anchor_dn=anchor_dn,
                posted_at=now,
            )

            # Log pair cost for monitoring
            if up_rungs and dn_rungs:
                up_vwap = sum(p * s for p, s in up_rungs) / sum(s for _, s in up_rungs)
                dn_vwap = sum(p * s for p, s in dn_rungs) / sum(s for _, s in dn_rungs)
                pair_cost = up_vwap + dn_vwap
            else:
                pair_cost = 0.0
            logger.info(
                "LADDER POSTED: %s | %d UP rungs + %d DN rungs | budget=$%.2f (UP=$%.0f DN=$%.0f) | pair_cost=%.3f",
                market.market_id, len(up_rungs), len(dn_rungs), budget,
                budget_up, budget_dn, pair_cost,
            )
            return count
        except ClobApiError:
            return 0

    def check_fills(self) -> list[TrackedOrder]:
        """Check for fills by querying open orders. Returns newly filled orders."""
        try:
            open_orders = self.executor.get_open_orders()
        except ClobApiError:
            return []

        result = self.tracker.reconcile(open_orders)

        for order in result["filled"]:
            fill_qty = order.size
            self.positions.update_position(
                order.market_id, order.side, fill_qty, fill_qty * order.price,
            )
            logger.info("FILL: %s %s %.1f @ $%.2f on %s",
                         order.side.value, order.token_id[:16],
                         fill_qty, order.price, order.market_id)

        if result["orphaned"]:
            self.executor.cancel_batch(result["orphaned"])

        return result["filled"]

    def reprice_if_needed(self, markets: dict[str, MarketWindow]) -> int:
        """Reprice ladders where the book has moved beyond threshold. Returns reprice count."""
        repriced = 0
        now = time.time()
        for mid, state in list(self.ladders.items()):
            # Cooldown: don't reprice the same market too often
            if now - state.last_reprice_at < MIN_REPRICE_INTERVAL:
                continue

            market = markets.get(mid)
            if market is None:
                continue

            try:
                best_ask_up = self.executor.get_best_ask(market.up_token_id)
                best_ask_dn = self.executor.get_best_ask(market.dn_token_id)
            except ClobApiError:
                continue

            up_moved = abs(best_ask_up - state.anchor_up) > self.cfg.reprice_threshold
            dn_moved = abs(best_ask_dn - state.anchor_dn) > self.cfg.reprice_threshold

            if not up_moved and not dn_moved:
                continue

            # Select timeframe-specific ladder parameters
            lp = self.cfg.get_ladder_params(market.timeframe_sec)

            tick_size = self.tick_cache.get_tick_size(market.condition_id, token_id=market.up_token_id) if self.tick_cache else 0.01

            # Budget for reprice: only the REMAINING unfilled portion
            total_budget = self.positions.bankroll * lp.position_size_fraction
            available = self.positions.bankroll - self.total_committed()
            budget_per_side = min(total_budget / 2.0, max(0, available / 2.0))
            if budget_per_side < 1.0:
                continue

            try:
                if up_moved:
                    # Cancel unfilled UP rungs and repost
                    cancelled = self.tracker.cancel_side(mid, Side.UP)
                    self.executor.cancel_batch(cancelled)

                    # Only budget for unfilled portion of this side
                    already_filled_cost = self.tracker.filled_cost(mid, Side.UP)
                    side_budget = max(0, budget_per_side - already_filled_cost)
                    if side_budget >= 1.0:
                        up_rungs = build_ladder_rungs(
                            best_ask_up, side_budget,
                            lp.rungs, lp.spacing, lp.width, lp.size_skew,
                            tick_size=tick_size,
                        )
                        up_order_dicts = [
                            {"token_id": market.up_token_id, "price": price, "size": size,
                             "market_id": mid, "side": Side.UP}
                            for price, size in up_rungs
                        ]
                        for record in self.executor.place_batch_limit_buys(up_order_dicts):
                            if record.status != "error":
                                self.tracker.add(TrackedOrder(
                                    order_id=record.order_id,
                                    market_id=mid, token_id=market.up_token_id,
                                    side=Side.UP, price=record.price, size=record.size,
                                    placed_at=now,
                                ))
                    state.anchor_up = best_ask_up

                if dn_moved:
                    cancelled = self.tracker.cancel_side(mid, Side.DOWN)
                    self.executor.cancel_batch(cancelled)

                    already_filled_cost = self.tracker.filled_cost(mid, Side.DOWN)
                    side_budget = max(0, budget_per_side - already_filled_cost)
                    if side_budget >= 1.0:
                        dn_rungs = build_ladder_rungs(
                            best_ask_dn, side_budget,
                            lp.rungs, lp.spacing, lp.width, lp.size_skew,
                            tick_size=tick_size,
                        )
                        dn_order_dicts = [
                            {"token_id": market.dn_token_id, "price": price, "size": size,
                             "market_id": mid, "side": Side.DOWN}
                            for price, size in dn_rungs
                        ]
                        for record in self.executor.place_batch_limit_buys(dn_order_dicts):
                            if record.status != "error":
                                self.tracker.add(TrackedOrder(
                                    order_id=record.order_id,
                                    market_id=mid, token_id=market.dn_token_id,
                                    side=Side.DOWN, price=record.price, size=record.size,
                                    placed_at=now,
                                ))
                    state.anchor_dn = best_ask_dn

                state.imbalance_accepted = False
                repriced += 1
                state.last_reprice_at = now
                logger.info("REPRICE: %s (UP moved=%s, DN moved=%s)", mid, up_moved, dn_moved)
            except ClobApiError:
                continue

        return repriced

    def check_imbalance(self, now_epoch: int) -> list[str]:
        """Check fill imbalance on all ladders. Returns list of market_ids where action was taken."""
        acted = []
        for mid, state in list(self.ladders.items()):
            if state.imbalance_accepted:
                continue

            up_qty = self.tracker.filled_qty(mid, Side.UP)
            dn_qty = self.tracker.filled_qty(mid, Side.DOWN)
            total = up_qty + dn_qty
            if total < 1.0:
                continue

            max_qty = max(up_qty, dn_qty)
            imbalance = abs(up_qty - dn_qty) / max_qty

            if imbalance > self.cfg.max_imbalance_ratio:
                # Severe imbalance: cancel the heavy side's unfilled rungs
                heavy_side = Side.UP if up_qty > dn_qty else Side.DOWN
                if state.imbalance_alert_at is None:
                    state.imbalance_alert_at = now_epoch
                    cancelled = self.tracker.cancel_side(mid, heavy_side)
                    for oid in cancelled:
                        self.executor.cancel_order(oid)
                    if cancelled:
                        logger.warning(
                            "IMBALANCE >%.0f%%: %s — cancelled %d %s rungs, waiting for other side",
                            imbalance * 100, mid, len(cancelled), heavy_side.value,
                        )
                    acted.append(mid)
                elif now_epoch - state.imbalance_alert_at > self.cfg.imbalance_timeout_sec:
                    # Timeout: accept the imbalance as a directional position
                    logger.warning(
                        "IMBALANCE TIMEOUT: %s — accepting one-sided position", mid,
                    )
                    state.imbalance_accepted = True
                    state.imbalance_alert_at = None
                    acted.append(mid)

            elif imbalance > 0.30:
                # Moderate imbalance: already handled by natural fill dynamics
                # Could boost lagging side here in future
                pass
            else:
                # Clear any previous alert
                state.imbalance_alert_at = None

        return acted

    def cancel_ladder(self, market_id: str) -> int:
        """Cancel all unfilled orders for a market. Returns count cancelled."""
        cancelled = self.tracker.cancel_market(market_id)
        for oid in cancelled:
            self.executor.cancel_order(oid)
        if cancelled:
            logger.info("CANCEL LADDER: %s — %d unfilled rungs", market_id, len(cancelled))
        return len(cancelled)

    def cleanup_ladder(self, market_id: str) -> None:
        """Remove all state for a settled/expired market."""
        self.ladders.pop(market_id, None)
        self.tracker.cleanup_market(market_id)

    def cancel_all_ladders(self) -> int:
        """Cancel all resting orders across all ladders. Returns total cancelled count.

        Ladder entries are NOT removed — they remain visible on the dashboard.
        """
        total = 0
        for mid in list(self.ladders.keys()):
            total += self.cancel_ladder(mid)
        return total

    def clear_cancelled_ladders(self) -> None:
        """Remove ladder entries that have no resting orders (all filled/cancelled).

        Called when the bot resumes from paused state so fresh ladders can be posted.
        """
        to_remove = [
            mid for mid in self.ladders
            if not self.tracker.has_orders(mid)
        ]
        for mid in to_remove:
            self.cleanup_ladder(mid)

    def get_ladder_stats(self, market_id: str) -> dict:
        """Get ladder statistics for the dashboard."""
        up_resting = len(self.tracker.get_resting_side(market_id, Side.UP))
        dn_resting = len(self.tracker.get_resting_side(market_id, Side.DOWN))
        up_filled = self.tracker.filled_qty(market_id, Side.UP)
        dn_filled = self.tracker.filled_qty(market_id, Side.DOWN)
        up_cost = self.tracker.filled_cost(market_id, Side.UP)
        dn_cost = self.tracker.filled_cost(market_id, Side.DOWN)

        up_vwap = up_cost / up_filled if up_filled > 0 else 0.0
        dn_vwap = dn_cost / dn_filled if dn_filled > 0 else 0.0

        total = up_filled + dn_filled
        imbalance = abs(up_filled - dn_filled) / max(up_filled, dn_filled) if total > 0 else 0.0

        return {
            "up_resting": up_resting,
            "dn_resting": dn_resting,
            "up_filled": up_filled,
            "dn_filled": dn_filled,
            "up_vwap": up_vwap,
            "dn_vwap": dn_vwap,
            "combined_vwap": up_vwap + dn_vwap,
            "imbalance": imbalance,
        }

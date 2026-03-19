"""Ladder manager: builds and maintains passive limit order ladders on every active market."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from polybot.config import BotConfig
from polybot.order_executor import OrderExecutor
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
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
    committed_capital: float = 0.0  # total capital locked in resting orders


def build_ladder_rungs(
    best_ask: float,
    budget: float,
    rungs: int,
    spacing: float,
    width: float,
    size_skew: float,
) -> list[tuple[float, float]]:
    """Build ladder rungs as (price, size) pairs.

    Returns rungs from cheapest (farthest from market) to most expensive (near market).
    """
    anchor = max(0.01, best_ask - width)
    prices = [round(anchor + i * spacing, 4) for i in range(rungs)]
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
        if size >= 0.1:  # minimum viable order size
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
    ):
        self.cfg = cfg
        self.executor = order_executor
        self.tracker = order_tracker
        self.positions = position_manager
        self.risk = risk_manager
        self.ladders: dict[str, LadderState] = {}

    def has_ladder(self, market_id: str) -> bool:
        return market_id in self.ladders

    def _total_committed(self) -> float:
        """Total capital committed across all active ladders."""
        return sum(s.committed_capital for s in self.ladders.values())

    def post_ladder(self, market: MarketWindow) -> int:
        """Post a full ladder (both sides) for a market. Returns number of orders placed."""
        if self.risk.is_halted():
            return 0
        if not self.risk.can_open_position(self.positions.active_position_count()):
            return 0

        # Select timeframe-specific ladder parameters
        lp = self.cfg.get_ladder_params(market.timeframe_sec)

        # Don't commit more capital than available bankroll
        available = self.positions.bankroll - self._total_committed()
        budget = min(
            self.positions.bankroll * lp.position_size_fraction,
            available,
        )
        if budget < 1.0:
            return 0
        budget_per_side = budget / 2.0

        best_ask_up = self.executor.get_best_ask(market.up_token_id)
        best_ask_dn = self.executor.get_best_ask(market.dn_token_id)

        up_rungs = build_ladder_rungs(
            best_ask_up, budget_per_side,
            lp.rungs, lp.spacing, lp.width, lp.size_skew,
        )
        dn_rungs = build_ladder_rungs(
            best_ask_dn, budget_per_side,
            lp.rungs, lp.spacing, lp.width, lp.size_skew,
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

        for price, size in up_rungs:
            record = self.executor.place_limit_buy(
                token_id=market.up_token_id,
                price=price, size=size,
                market_id=market.market_id, side=Side.UP,
            )
            if record.status != "error":
                self.tracker.add(TrackedOrder(
                    order_id=record.order_id,
                    market_id=market.market_id,
                    token_id=market.up_token_id,
                    side=Side.UP,
                    price=price, size=size,
                    placed_at=now,
                ))
                count += 1

        for price, size in dn_rungs:
            record = self.executor.place_limit_buy(
                token_id=market.dn_token_id,
                price=price, size=size,
                market_id=market.market_id, side=Side.DOWN,
            )
            if record.status != "error":
                self.tracker.add(TrackedOrder(
                    order_id=record.order_id,
                    market_id=market.market_id,
                    token_id=market.dn_token_id,
                    side=Side.DOWN,
                    price=price, size=size,
                    placed_at=now,
                ))
                count += 1

        anchor_up = up_rungs[-1][0] if up_rungs else best_ask_up
        anchor_dn = dn_rungs[-1][0] if dn_rungs else best_ask_dn
        committed = sum(p * s for p, s in up_rungs) + sum(p * s for p, s in dn_rungs)

        self.ladders[market.market_id] = LadderState(
            market_id=market.market_id,
            asset=market.asset,
            anchor_up=anchor_up,
            anchor_dn=anchor_dn,
            posted_at=now,
            committed_capital=committed,
        )

        logger.info(
            "LADDER POSTED: %s | %d UP rungs + %d DN rungs | budget=$%.2f",
            market.market_id, len(up_rungs), len(dn_rungs), budget,
        )
        return count

    def check_fills(self) -> int:
        """Check for fills by querying open orders. Returns number of new fills detected."""
        open_orders = self.executor.get_open_orders()
        open_ids = {o.get("id", o.get("orderID", "")) for o in open_orders}

        fills = 0
        for order in list(self.tracker.orders.values()):
            if order.status not in ("resting", "partial"):
                continue
            if order.order_id not in open_ids:
                # Order disappeared from open orders -> it filled
                fill_qty = order.size - order.filled
                if fill_qty > 0:
                    self.tracker.update_fill(order.order_id, fill_qty)
                    self.positions.update_position(
                        order.market_id, order.side, fill_qty, fill_qty * order.price,
                    )
                    # Reduce committed capital as it converts to position
                    state = self.ladders.get(order.market_id)
                    if state:
                        state.committed_capital = max(0, state.committed_capital - fill_qty * order.price)
                    fills += 1
                    logger.info(
                        "FILL: %s %s %.1f @ $%.2f on %s",
                        order.side.value, order.token_id[:16],
                        fill_qty, order.price, order.market_id,
                    )
        return fills

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

            best_ask_up = self.executor.get_best_ask(market.up_token_id)
            best_ask_dn = self.executor.get_best_ask(market.dn_token_id)

            up_moved = abs(best_ask_up - state.anchor_up) > self.cfg.reprice_threshold
            dn_moved = abs(best_ask_dn - state.anchor_dn) > self.cfg.reprice_threshold

            if not up_moved and not dn_moved:
                continue

            # Select timeframe-specific ladder parameters
            lp = self.cfg.get_ladder_params(market.timeframe_sec)

            # Budget for reprice: only the REMAINING unfilled portion
            total_budget = self.positions.bankroll * lp.position_size_fraction
            available = self.positions.bankroll - self._total_committed()
            budget_per_side = min(total_budget / 2.0, max(0, available / 2.0))
            if budget_per_side < 1.0:
                continue

            if up_moved:
                # Cancel unfilled UP rungs (returns capital) and repost
                cancelled = self.tracker.cancel_side(mid, Side.UP)
                for oid in cancelled:
                    self.executor.cancel_order(oid)
                # Reclaim committed capital from cancelled orders
                for oid in cancelled:
                    o = self.tracker.orders.get(oid)
                    if o:
                        state.committed_capital = max(0, state.committed_capital - (o.size - o.filled) * o.price)

                # Only budget for unfilled portion of this side
                already_filled_cost = self.tracker.filled_cost(mid, Side.UP)
                side_budget = max(0, budget_per_side - already_filled_cost)
                if side_budget >= 1.0:
                    up_rungs = build_ladder_rungs(
                        best_ask_up, side_budget,
                        lp.rungs, lp.spacing, lp.width, lp.size_skew,
                    )
                    new_committed = 0.0
                    for price, size in up_rungs:
                        record = self.executor.place_limit_buy(
                            token_id=market.up_token_id,
                            price=price, size=size,
                            market_id=mid, side=Side.UP,
                        )
                        if record.status != "error":
                            self.tracker.add(TrackedOrder(
                                order_id=record.order_id,
                                market_id=mid, token_id=market.up_token_id,
                                side=Side.UP, price=price, size=size,
                                placed_at=now,
                            ))
                            new_committed += price * size
                    state.committed_capital += new_committed
                state.anchor_up = best_ask_up

            if dn_moved:
                cancelled = self.tracker.cancel_side(mid, Side.DOWN)
                for oid in cancelled:
                    self.executor.cancel_order(oid)
                for oid in cancelled:
                    o = self.tracker.orders.get(oid)
                    if o:
                        state.committed_capital = max(0, state.committed_capital - (o.size - o.filled) * o.price)

                already_filled_cost = self.tracker.filled_cost(mid, Side.DOWN)
                side_budget = max(0, budget_per_side - already_filled_cost)
                if side_budget >= 1.0:
                    dn_rungs = build_ladder_rungs(
                        best_ask_dn, side_budget,
                        lp.rungs, lp.spacing, lp.width, lp.size_skew,
                    )
                    new_committed = 0.0
                    for price, size in dn_rungs:
                        record = self.executor.place_limit_buy(
                            token_id=market.dn_token_id,
                            price=price, size=size,
                            market_id=mid, side=Side.DOWN,
                        )
                        if record.status != "error":
                            self.tracker.add(TrackedOrder(
                                order_id=record.order_id,
                                market_id=mid, token_id=market.dn_token_id,
                                side=Side.DOWN, price=price, size=size,
                                placed_at=now,
                            ))
                            new_committed += price * size
                    state.committed_capital += new_committed
                state.anchor_dn = best_ask_dn

            repriced += 1
            state.last_reprice_at = now
            logger.info("REPRICE: %s (UP moved=%s, DN moved=%s)", mid, up_moved, dn_moved)

        return repriced

    def check_imbalance(self, now_epoch: int) -> list[str]:
        """Check fill imbalance on all ladders. Returns list of market_ids where action was taken."""
        acted = []
        for mid, state in list(self.ladders.items()):
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

    def check_early_exits(self, markets: dict[str, MarketWindow]) -> list[dict]:
        """Early exit removed — settlement handles position closure."""
        return []

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

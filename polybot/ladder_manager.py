"""Ladder manager: builds and maintains passive limit order ladders on every active market."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.fees import compute_fee
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
    heavy_side_locked: str | None = None
    up_token_id: str = ""
    dn_token_id: str = ""
    current_ask_up: float = 0.0
    current_ask_dn: float = 0.0
    timeframe_sec: int = 900


MIN_ORDER_SIZE = 5.0  # Polymarket minimum for GTC orders


def build_ladder_rungs(
    best_ask: float,
    budget: float,
    rungs: int,
    spacing: float,
    width: float,
    size_skew: float,
    tick_size: float = 0.01,
    max_rung_price: float = 1.0,
    fee_rate: float = 0.0,
) -> list[tuple[float, float]]:
    """Build ladder rungs as (price, size) pairs.

    Returns rungs from cheapest (farthest from market) to most expensive (near market).
    The top rung is offset 1 tick below best_ask (passive limit, not marketable).
    Automatically reduces rung count if budget is too small for the configured amount.
    """
    if best_ask <= 0 or budget <= 0:
        return []

    # Estimate max affordable rungs: ensure the cheapest rung (weight=1.0)
    # gets at least MIN_ORDER_SIZE shares.
    avg_price = max(tick_size, best_ask - width / 2)
    min_cost_per_rung = MIN_ORDER_SIZE * avg_price
    max_affordable = max(1, int(budget / min_cost_per_rung))
    effective_rungs = min(rungs, max_affordable)

    # Offset anchor by 1 tick so top rung is passive (best_ask - tick_size), not marketable
    anchor = max(tick_size, best_ask - width - tick_size)
    # Spread rungs evenly across the width with the effective count
    effective_spacing = spacing if effective_rungs == rungs else width / max(effective_rungs, 1)
    prices = [round_to_tick(anchor + i * effective_spacing, tick_size) for i in range(effective_rungs)]
    # Clamp prices to valid range using tick_size as floor/ceiling
    prices = [max(tick_size, min(min(1.0 - tick_size, max_rung_price), p)) for p in prices]

    # Linear size skew: cheapest gets weight 1.0, most expensive gets weight size_skew
    weights = [1.0 + (size_skew - 1.0) * (i / max(effective_rungs - 1, 1)) for i in range(effective_rungs)]

    # Compute sizes so total cost = budget
    total_weighted_cost = sum(w * p for w, p in zip(weights, prices))
    if total_weighted_cost <= 0:
        return []

    scale = budget / total_weighted_cost
    result = []
    for price, weight in zip(prices, weights):
        size = scale * weight
        if size >= MIN_ORDER_SIZE:
            result.append((price, round(size, 1)))

    # Rescale sizes so total cost matches budget (rungs may have been dropped)
    if result:
        actual_cost = sum(p * s for p, s in result)  # fee_rate not used in this copy
        if actual_cost > 0 and abs(actual_cost - budget) / budget > 0.01:
            rescale = budget / actual_cost
            result = [(p, round(s * rescale, 1)) for p, s in result]
            result = [(p, s) for p, s in result if s >= MIN_ORDER_SIZE]

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
        self._killed_ladders: set[str] = set()
        self.tick_cache = tick_size_cache
        self.fee_rate: float = getattr(cfg, "maker_fee_rate", 0.0)

    def _fill_cost(self, price: float, qty: float) -> float:
        """Fee-inclusive cost for qty shares at price."""
        return qty * (price + compute_fee(price, self.fee_rate))

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
            if market.market_id in self._killed_ladders:
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
            if best_ask_up is not None and best_ask_dn is not None and best_ask_up > 0 and best_ask_dn > 0:
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

            # Pair cost guard: check VWAP and top-rung pair cost
            if up_rungs and dn_rungs:
                up_vwap = sum(p * s for p, s in up_rungs) / sum(s for _, s in up_rungs)
                dn_vwap = sum(p * s for p, s in dn_rungs) / sum(s for _, s in dn_rungs)
                if up_vwap + dn_vwap > lp.max_pair_cost:
                    logger.info(
                        "Pair cost guard: %s combined VWAP %.4f > %.4f, skipping",
                        market.market_id, up_vwap + dn_vwap, lp.max_pair_cost,
                    )
                    return 0
                # Top-rung guard: trim the most expensive rungs that would produce bad pair costs
                top_up = up_rungs[-1][0]
                top_dn = dn_rungs[-1][0]
                while len(up_rungs) > 1 and len(dn_rungs) > 1 and top_up + top_dn > lp.max_pair_cost:
                    if top_up >= top_dn:
                        up_rungs.pop()
                    else:
                        dn_rungs.pop()
                    top_up = up_rungs[-1][0]
                    top_dn = dn_rungs[-1][0]
                if top_up + top_dn > lp.max_pair_cost:
                    logger.info("Top-rung guard: %s pair=%.3f > %.3f after trim, skipping",
                                market.market_id, top_up + top_dn, lp.max_pair_cost)
                    return 0

            now = time.time()
            count = 0

            # GTD expiration: orders auto-cancel at window close minus safety buffer
            expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)

            # Build batch order list for UP side
            up_order_dicts = [
                {"token_id": market.up_token_id, "price": price, "size": size,
                 "market_id": market.market_id, "side": Side.UP,
                 "expiration": expiration}
                for price, size in up_rungs
            ]
            # Build batch order list for DN side
            dn_order_dicts = [
                {"token_id": market.dn_token_id, "price": price, "size": size,
                 "market_id": market.market_id, "side": Side.DOWN,
                 "expiration": expiration}
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
                up_token_id=market.up_token_id,
                dn_token_id=market.dn_token_id,
                current_ask_up=best_ask_up,
                current_ask_dn=best_ask_dn,
                timeframe_sec=market.timeframe_sec,
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

    def _check_one_side_cap(self, filled_orders: list) -> None:
        """Cancel resting orders on heavy side when fill ratio exceeds 3:1 and qty > 5."""
        checked: set[str] = set()
        for order in filled_orders:
            mid = order.market_id
            if mid in checked:
                continue
            checked.add(mid)
            up_qty = self.tracker.filled_qty(mid, Side.UP)
            dn_qty = self.tracker.filled_qty(mid, Side.DOWN)
            for side, qty, other_qty in [(Side.UP, up_qty, dn_qty), (Side.DOWN, dn_qty, up_qty)]:
                if qty >= 5.0 and (other_qty == 0 or qty / max(other_qty, 0.1) > 3.0):
                    cancelled = self.tracker.cancel_side(mid, side)
                    for oid in cancelled:
                        self.executor.cancel_order(oid)
                    self.tracker.confirm_cancels(cancelled)
                    state = self.ladders.get(mid)
                    if state:
                        state.heavy_side_locked = side.value
                    if cancelled:
                        logger.warning("ONE-SIDE CAP: %s %s %.0f fills (other=%.0f), cancelled %d",
                                       mid, side.value, qty, other_qty, len(cancelled))

    def check_fills(self, settled_markets: set[str] | None = None) -> list[TrackedOrder]:
        """Check for fills by querying open orders. Returns newly filled orders."""
        try:
            open_orders = self.executor.get_open_orders()
        except ClobApiError:
            return []

        result = self.tracker.reconcile(open_orders, settled_markets)

        for order in result["filled"]:
            # Credit full fill minus any already-credited partial fills
            fill_qty = order.size - order.credited_to_pm
            if fill_qty > 0:
                self.positions.update_position(
                    order.market_id, order.side, fill_qty, self._fill_cost(order.price, fill_qty),
                )
                order.credited_to_pm = order.size
                logger.info("FILL: %s %s %.1f @ $%.2f on %s",
                             order.side.value, order.token_id[:16],
                             fill_qty, order.price, order.market_id)

        for order in result["partial"]:
            # Credit the newly matched quantity
            new_qty = order.filled - order.credited_to_pm
            if new_qty > 0:
                self.positions.update_position(
                    order.market_id, order.side, new_qty, self._fill_cost(order.price, new_qty),
                )
                order.credited_to_pm = order.filled
                logger.info("PARTIAL FILL: %s %s %.1f @ $%.2f on %s",
                             order.side.value, order.token_id[:16],
                             new_qty, order.price, order.market_id)

        if result["orphaned"]:
            self.executor.cancel_batch(result["orphaned"])

        if result["filled"]:
            self._check_one_side_cap(result["filled"])

        return result["filled"]

    def process_paper_fills(self, paper_fills: list[dict]) -> list[TrackedOrder]:
        """Process pre-simulated fills from PaperClobClient.tick()."""
        if not paper_fills:
            return []
        filled = []
        for fill in paper_fills:
            order_id = fill.get("id", fill.get("orderID", ""))
            order = self.tracker.orders.get(order_id)
            if order is None:
                continue
            if order.status == "filled":
                continue
            fill_qty = order.size
            self.positions.update_position(
                order.market_id, order.side, fill_qty, fill_qty * order.price,
            )
            order.status = "filled"
            order.filled = fill_qty
            logger.info("FILL: %s %s %.1f @ $%.2f on %s",
                         order.side.value, order.token_id[:16],
                         fill_qty, order.price, order.market_id)
            filled.append(order)
        if filled:
            self._check_one_side_cap(filled)
        return filled

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

            # Cache latest ask prices for dashboard
            state.current_ask_up = best_ask_up
            state.current_ask_dn = best_ask_dn

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
                up_qty = self.tracker.filled_qty(mid, Side.UP)
                dn_qty = self.tracker.filled_qty(mid, Side.DOWN)

                if up_moved and state.heavy_side_locked == "UP":
                    # UP is the locked heavy side — never reprice it
                    state.anchor_up = best_ask_up
                    logger.info("REPRICE SKIP UP: %s locked heavy side", mid)
                    up_moved_ok = False
                elif up_moved:
                    # UP is the light side (or no lock) — always allow reprice
                    up_moved_ok = True
                else:
                    up_moved_ok = False

                if up_moved_ok:
                    # Flush uncredited partial fills before cancelling the side
                    for order, delta in self.tracker.flush_uncredited(mid):
                        if order.side == Side.UP:
                            self.positions.update_position(
                                order.market_id, order.side, delta, self._fill_cost(order.price, delta),
                            )
                    cancelled = self.tracker.cancel_side(mid, Side.UP)
                    self.executor.cancel_batch(cancelled)
                    already_filled_cost = self.tracker.filled_cost(mid, Side.UP)
                    side_budget = max(0, budget_per_side - already_filled_cost)
                    if side_budget >= 1.0:
                        up_rungs = build_ladder_rungs(
                            best_ask_up, side_budget,
                            lp.rungs, lp.spacing, lp.width, lp.size_skew,
                            tick_size=tick_size,
                        )
                        reprice_expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)
                        up_order_dicts = [
                            {"token_id": market.up_token_id, "price": price, "size": size,
                             "market_id": mid, "side": Side.UP,
                             "expiration": reprice_expiration}
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

                if dn_moved and state.heavy_side_locked == "DOWN":
                    # DN is the locked heavy side — never reprice it
                    state.anchor_dn = best_ask_dn
                    logger.info("REPRICE SKIP DN: %s locked heavy side", mid)
                    dn_moved = False
                elif dn_moved:
                    # DN is the light side (or no lock) — always allow reprice
                    pass  # dn_moved stays True
                else:
                    pass  # dn_moved stays False

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
                        reprice_expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)
                        dn_order_dicts = [
                            {"token_id": market.dn_token_id, "price": price, "size": size,
                             "market_id": mid, "side": Side.DOWN,
                             "expiration": reprice_expiration}
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

                # Note: do NOT reset imbalance_accepted — reprice should not undo imbalance guard
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

            # Require minimum fills on the heavy side before judging imbalance
            heavy_count = max(
                self.tracker.filled_count(mid, Side.UP),
                self.tracker.filled_count(mid, Side.DOWN),
            )
            light_count = min(
                self.tracker.filled_count(mid, Side.UP),
                self.tracker.filled_count(mid, Side.DOWN),
            )
            min_heavy_fills = getattr(self.cfg, "imbalance_min_heavy_fills", 3)
            if heavy_count < min_heavy_fills:
                continue  # not enough fills to judge imbalance

            max_qty = max(up_qty, dn_qty)
            imbalance = abs(up_qty - dn_qty) / max_qty

            # Dynamic timeout: 30% of window timeframe, floored by config
            dynamic_timeout = state.timeframe_sec * 0.30
            timeout = max(self.cfg.imbalance_timeout_sec, dynamic_timeout)

            if imbalance > self.cfg.max_imbalance_ratio and light_count == 0:
                # Severe imbalance: cancel the heavy side's unfilled rungs
                heavy_side = Side.UP if up_qty > dn_qty else Side.DOWN
                if state.imbalance_alert_at is None:
                    state.imbalance_alert_at = now_epoch
                    cancelled = self.tracker.cancel_side(mid, heavy_side)
                    for oid in cancelled:
                        self.executor.cancel_order(oid)
                    state.heavy_side_locked = heavy_side.value
                    if cancelled:
                        logger.warning(
                            "IMBALANCE >%.0f%%: %s — cancelled %d %s rungs, locked side",
                            imbalance * 100, mid, len(cancelled), heavy_side.value,
                        )
                    acted.append(mid)
                elif now_epoch - state.imbalance_alert_at > timeout:
                    # Timeout: accept the imbalance as a directional position
                    logger.warning(
                        "IMBALANCE TIMEOUT: %s — accepting one-sided position", mid,
                    )
                    state.imbalance_accepted = True
                    state.imbalance_alert_at = None
                    acted.append(mid)

            elif imbalance > 0.30:
                # Moderate imbalance: tighten the light side closer to market
                light_side = Side.DOWN if up_qty > dn_qty else Side.UP
                light_token = state.dn_token_id if light_side == Side.DOWN else state.up_token_id
                best_ask = self.executor.get_best_ask(light_token) if light_token else None
                if best_ask is not None and best_ask > 0:
                    resting = self.tracker.get_resting_side(mid, light_side)
                    # If resting orders are more than $0.03 from market, cancel and repost tighter
                    far_orders = [o for o in resting if abs(o.price - best_ask) > 0.03]
                    if far_orders:
                        for o in far_orders:
                            self.executor.cancel_order(o.order_id)
                            self.tracker.cancel(o.order_id)
                        logger.info("LIGHT-SIDE TIGHTEN: %s — cancelled %d far %s rungs (imb=%.0f%%)",
                                    mid, len(far_orders), light_side.value, imbalance * 100)
            else:
                # Clear any previous alert
                state.imbalance_alert_at = None

        return acted

    def cancel_ladder(self, market_id: str) -> int:
        """Cancel all unfilled orders for a market. Returns count cancelled.

        Flushes any uncredited partial fills to PositionManager before cancelling
        so that partial fills are not lost when orders are cancelled.
        """
        # Flush uncredited partial fills before cancelling
        for order, delta in self.tracker.flush_uncredited(market_id):
            self.positions.update_position(
                order.market_id, order.side, delta, self._fill_cost(order.price, delta),
            )
            logger.info("FLUSH PARTIAL: %s %s %.1f @ $%.2f on %s",
                         order.side.value, order.token_id[:16],
                         delta, order.price, order.market_id)
        cancelled = self.tracker.cancel_market(market_id)
        for oid in cancelled:
            self.executor.cancel_order(oid)
        if cancelled:
            logger.info("CANCEL LADDER: %s — %d unfilled rungs", market_id, len(cancelled))
        return len(cancelled)

    def cleanup_ladder(self, market_id: str) -> None:
        """Remove all state for a settled/expired market."""
        self.ladders.pop(market_id, None)
        self._killed_ladders.discard(market_id)
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

        state = self.ladders.get(market_id)

        return {
            "up_resting": up_resting,
            "dn_resting": dn_resting,
            "up_filled": up_filled,
            "dn_filled": dn_filled,
            "up_vwap": up_vwap,
            "dn_vwap": dn_vwap,
            "pair_cost": up_vwap + dn_vwap,
            "imbalance": imbalance,
            "ask_up": state.current_ask_up if state else 0.0,
            "ask_dn": state.current_ask_dn if state else 0.0,
            "up_filled_count": self.tracker.filled_count(market_id, Side.UP),
            "dn_filled_count": self.tracker.filled_count(market_id, Side.DOWN),
            "up_total_rungs": self.tracker.total_count(market_id, Side.UP),
            "dn_total_rungs": self.tracker.total_count(market_id, Side.DOWN),
        }

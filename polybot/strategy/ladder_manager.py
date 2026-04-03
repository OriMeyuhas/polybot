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
    heavy_side_locked: str | None = None  # "UP" or "DOWN" — blocks reprice on that side
    up_token_id: str = ""
    dn_token_id: str = ""
    current_ask_up: float = 0.0
    current_ask_dn: float = 0.0
    timeframe_sec: int = 900
    force_buy_done: bool = False  # True after a force-buy has been placed
    boosted_at: float = 0.0  # timestamp when boost fired — loss cap grace period


MIN_ORDER_SIZE = 5.0  # Polymarket minimum for GTC orders


def compute_decay_factor(market: MarketWindow, now: int, floor: float = 0.58) -> float:
    """Compute time-decay factor for ladder width/rungs.

    Linear decay from 1.0 at window open to `floor` at 60% elapsed,
    then holds at `floor` for the rest of the window.
    """
    total = market.timeframe_sec
    if total <= 0:
        return floor
    remaining = market.remaining(now)
    elapsed_frac = max(0.0, 1.0 - (remaining / total))
    phase1_end = 0.6
    if elapsed_frac <= phase1_end:
        return 1.0 - (elapsed_frac / phase1_end) * (1.0 - floor)
    return floor


def build_ladder_rungs(
    best_ask: float,
    budget: float,
    rungs: int,
    spacing: float,
    width: float,
    size_skew: float,
    tick_size: float = 0.01,
    fee_rate: float = 0.0,
    max_rung_price: float = 1.0,
) -> list[tuple[float, float]]:
    """Build ladder rungs as (price, size) pairs.

    Returns rungs from cheapest (farthest from market) to most expensive (near market).
    Automatically reduces rung count if budget is too small for the configured amount.

    fee_rate: Polymarket maker fee rate. When non-zero, sizes are computed so that
              the fee-inclusive cost (price + fee) * size fits within budget.
    max_rung_price: Optional upper price clamp (default 1.0).
    """
    if best_ask <= 0 or budget <= 0:
        return []

    # Estimate max affordable rungs: ensure the cheapest rung (weight=1.0)
    # gets at least MIN_ORDER_SIZE shares.
    avg_price = max(tick_size, best_ask - width / 2)
    avg_effective_price = avg_price + compute_fee(avg_price, fee_rate)
    min_cost_per_rung = MIN_ORDER_SIZE * avg_effective_price
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

    # Compute sizes so fee-inclusive total cost = budget
    total_weighted_cost = sum(
        w * (p + compute_fee(p, fee_rate)) for w, p in zip(weights, prices)
    )
    if total_weighted_cost <= 0:
        return []

    scale = budget / total_weighted_cost
    result = []
    for price, weight in zip(prices, weights):
        size = scale * weight
        if size >= MIN_ORDER_SIZE:
            result.append((price, round(size, 1)))

    # Rescale sizes so fee-inclusive total cost matches budget (rungs may have been dropped)
    if result:
        actual_cost = sum((p + compute_fee(p, fee_rate)) * s for p, s in result)
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

    def is_killed(self, market_id: str) -> bool:
        """Return True if this market's ladder was killed by check_loss_cap."""
        return market_id in self._killed_ladders

    def post_ladder_pre_open(self, market: MarketWindow, spot_delta: float = 0.0) -> int:
        """Post ladder for pre-open markets. Delegates to post_ladder."""
        return self.post_ladder(market, spot_delta=spot_delta)

    def try_complete_pair(self, market: MarketWindow, now: float = 0, spot_delta: float = 0.0) -> dict | None:
        """Phase B: Force-buy the light side to complete a one-sided position.

        Trigger conditions (ALL must be true):
        1. Position exists for this market
        2. Window is >= force_buy_elapsed_pct elapsed
        3. Position is >75% one-sided (light_qty < 0.25 * heavy_qty)
        4. Hypothetical pair_cost < force_buy_max_pair_cost
        5. Light side needs >= MIN_ORDER_SIZE worth of shares
        6. Market is not in _killed_ladders

        Returns dict with {side, price, qty, pair_cost} or None.
        """
        mid = market.market_id

        # Guard: killed ladder
        if mid in self._killed_ladders:
            return None

        # Guard: already force-bought this window
        state = self.ladders.get(mid)
        if state is not None and state.force_buy_done:
            return None

        # Check position exists
        pos = self.positions.positions.get(mid)
        if pos is None:
            return None

        # Elapsed fraction check
        if market.timeframe_sec <= 0:
            return None
        force_buy_pct = getattr(self.cfg, "force_buy_elapsed_pct", 0.70)
        elapsed_frac = (now - market.open_epoch) / market.timeframe_sec
        if elapsed_frac < force_buy_pct:
            return None

        # Determine heavy and light sides from position
        up_qty = pos.up_qty
        dn_qty = pos.dn_qty

        if up_qty <= 0 and dn_qty <= 0:
            return None

        if up_qty >= dn_qty:
            heavy_qty = up_qty
            light_qty = dn_qty
            heavy_cost = pos.up_cost
            light_side = Side.DOWN
            light_token = market.dn_token_id
        else:
            heavy_qty = dn_qty
            light_qty = up_qty
            heavy_cost = pos.dn_cost
            light_side = Side.UP
            light_token = market.up_token_id

        # Require minimum fills on heavy side (aligned with imbalance detection)
        min_heavy = getattr(self.cfg, "imbalance_min_heavy_fills", 3)
        heavy_side_enum = Side.UP if up_qty >= dn_qty else Side.DOWN
        if self.tracker.filled_count(mid, heavy_side_enum) < min_heavy:
            return None

        # Spot gate: block force-buy when spot moves against heavy side
        gate = self.cfg.spot_gate_force_buy_threshold
        if abs(spot_delta) > gate:
            spot_against = (
                (heavy_side_enum == Side.UP and spot_delta < -gate) or
                (heavy_side_enum == Side.DOWN and spot_delta > gate)
            )
            if spot_against:
                logger.info("SPOT GATE FORCE-BUY: %s delta=%.3f%% against heavy=%s, skip",
                             mid, spot_delta * 100, heavy_side_enum.value)
                return None

        # Check one-sidedness: light must be < 25% of heavy
        if heavy_qty > 0 and light_qty >= 0.25 * heavy_qty:
            return None

        deficit = heavy_qty - light_qty

        # Estimate fill cost
        estimate = self.executor.estimate_fill_cost(light_token, deficit)
        if estimate is None:
            return None

        estimated_avg_price, estimated_total_cost = estimate

        # Min order size guard
        if deficit * estimated_avg_price < MIN_ORDER_SIZE:
            return None

        # Pair cost guard
        heavy_vwap = heavy_cost / heavy_qty if heavy_qty > 0 else 0
        force_buy_max = getattr(self.cfg, "force_buy_max_pair_cost", 0.88)
        hypothetical_pair_cost = heavy_vwap + estimated_avg_price
        if hypothetical_pair_cost >= force_buy_max:
            # Pair can't be completed profitably — kill the ladder to stop bleeding
            if mid in self.ladders:
                self.cancel_ladder(mid)
                del self.ladders[mid]
                self._killed_ladders.add(mid)
                logger.warning(
                    "FORCE-BUY REJECTED: %s — pair_cost $%.3f > $%.3f, killed ladder to cut losses",
                    mid, hypothetical_pair_cost, force_buy_max,
                )
            if state is not None:
                state.force_buy_done = True
            return None

        # Get best ask for the limit order price
        try:
            best_ask = self.executor.get_best_ask(light_token)
        except ClobApiError:
            return None
        if best_ask is None or best_ask <= 0:
            return None

        # Place order at best ask
        try:
            record = self.executor.place_limit_buy(
                light_token, best_ask, deficit, mid, light_side,
            )
        except ClobApiError:
            return None

        # Track the order
        if record.order_id:
            self.tracker.add(TrackedOrder(
                order_id=record.order_id,
                market_id=mid,
                token_id=light_token,
                side=light_side,
                price=best_ask,
                size=deficit,
                placed_at=time.time(),
            ))

        # Credit to position manager immediately
        fill_cost = self._fill_cost(best_ask, deficit)
        self.positions.update_position(mid, light_side, deficit, fill_cost)

        # Mark force-buy done so we don't repeat
        if state is not None:
            state.force_buy_done = True

        logger.info(
            "FORCE-BUY: %s — %s %.1f @ $%.3f, pair_cost=$%.3f",
            mid, light_side.value, deficit, best_ask, hypothetical_pair_cost,
        )

        return {
            "side": light_side,
            "price": estimated_avg_price,
            "qty": deficit,
            "pair_cost": hypothetical_pair_cost,
        }

    def resting_order_cost(self) -> float:
        """Fee-inclusive cost of resting (unfilled) orders across all ladders.

        Excludes filled position cost — only counts capital locked in orders
        that haven't filled yet. Used by live mode available-capital calculation.
        """
        resting = 0.0
        for mid in self.ladders:
            for order in self.tracker.get_resting(mid):
                remaining = order.size - order.filled
                resting += (order.price + compute_fee(order.price, self.fee_rate)) * remaining
        return resting

    def total_committed(self) -> float:
        """Total capital committed: fee-inclusive resting order cost + filled position cost."""
        return self.resting_order_cost() + self.positions.total_position_cost()

    def boost_light_side(self, market: MarketWindow, now: float, spot_delta: float = 0.0) -> int:
        """Phase D: Reanchor the light side's ladder closer to market.

        Trigger conditions (ALL must be true):
        1. Ladder exists for this market
        2. boosted_side is None (only boost once per window)
        3. Heavy side has >= imbalance_min_heavy_fills fully filled orders
        4. Light side has 0 filled orders
        5. Window is >= boost_elapsed_pct elapsed
        6. Ladder is not killed

        Returns number of new orders placed (0 if no action taken).
        """
        mid = market.market_id

        # Guard: killed ladder
        if mid in self._killed_ladders:
            return 0

        state = self.ladders.get(mid)
        if state is None:
            return 0

        # Only boost once per window
        if state.boosted_side is not None:
            return 0

        # Elapsed fraction check
        if market.timeframe_sec <= 0:
            return 0
        elapsed_frac = (now - market.open_epoch) / market.timeframe_sec
        boost_pct = getattr(self.cfg, "boost_elapsed_pct", 0.20)
        if elapsed_frac < boost_pct:
            return 0

        # Determine heavy and light sides by filled count
        up_count = self.tracker.filled_count(mid, Side.UP)
        dn_count = self.tracker.filled_count(mid, Side.DOWN)
        min_heavy = getattr(self.cfg, "imbalance_min_heavy_fills", 3)

        if up_count >= min_heavy and dn_count == 0:
            heavy_side = Side.UP
            light_side = Side.DOWN
            light_token = market.dn_token_id
        elif dn_count >= min_heavy and up_count == 0:
            heavy_side = Side.DOWN
            light_side = Side.UP
            light_token = market.up_token_id
        else:
            return 0  # conditions not met

        # Spot gate: skip boost if spot moves away from heavy side
        if abs(spot_delta) > self.cfg.spot_delta_reduce_threshold:
            spot_toward_heavy = (
                (heavy_side == Side.UP and spot_delta > 0) or
                (heavy_side == Side.DOWN and spot_delta < 0)
            )
            if not spot_toward_heavy:
                logger.info("SPOT GATE BOOST: %s delta=%.3f%% away from heavy=%s, skip",
                             mid, spot_delta * 100, heavy_side.value)
                return 0

        # Cancel BOTH sides' resting orders:
        # - Light side: stale rungs too far from market
        # - Heavy side: stop further accumulation while we wait for light side to fill
        cancelled_light = self.tracker.cancel_side(mid, light_side)
        self.executor.cancel_batch(cancelled_light)
        cancelled_heavy = self.tracker.cancel_side(mid, heavy_side)
        self.executor.cancel_batch(cancelled_heavy)
        if cancelled_heavy:
            logger.info("BOOST: %s — cancelled %d heavy-side (%s) rungs to cap exposure",
                        mid, len(cancelled_heavy), heavy_side.value)

        # Get best ask for the light side
        try:
            best_ask = self.executor.get_best_ask(light_token)
        except ClobApiError:
            return 0
        if best_ask is None or best_ask <= 0:
            return 0

        # Get ladder params
        lp = self.cfg.get_ladder_params(market.timeframe_sec, current_bankroll=self.positions.bankroll)
        tick_size = self.tick_cache.get_tick_size(market.condition_id, token_id=market.up_token_id) if self.tick_cache else 0.01

        # Compute budget: half of total minus already filled cost on light side
        total_budget = min(
            lp.position_size_fraction * self.positions.bankroll,
            self.positions.bankroll - self.total_committed(),
        )
        light_filled_cost = self.tracker.filled_cost(mid, light_side)
        side_budget = max(0, total_budget / 2.0 - light_filled_cost)
        if side_budget < 1.0:
            return 0

        # Build new rungs with half width (tighter)
        half_width = lp.width / 2.0
        new_rungs = build_ladder_rungs(
            best_ask, side_budget,
            lp.rungs, lp.spacing, half_width, lp.size_skew,
            tick_size=tick_size,
            fee_rate=self.fee_rate,
        )

        # Pair cost guard: trim rungs where rung_price + heavy_vwap > max_pair_cost
        heavy_filled_qty = self.tracker.filled_qty(mid, heavy_side)
        heavy_filled_cost = self.tracker.filled_cost(mid, heavy_side)
        if heavy_filled_qty > 0:
            heavy_vwap = heavy_filled_cost / heavy_filled_qty
            new_rungs = [
                (p, s) for p, s in new_rungs
                if p + heavy_vwap <= lp.max_pair_cost
            ]

        if not new_rungs:
            return 0

        # Place orders
        order_dicts = [
            {"token_id": light_token, "price": price, "size": size,
             "market_id": mid, "side": light_side}
            for price, size in new_rungs
        ]
        count = 0
        place_time = time.time()
        for record in self.executor.place_batch_limit_buys(order_dicts):
            if record.status != "error":
                self.tracker.add(TrackedOrder(
                    order_id=record.order_id,
                    market_id=mid,
                    token_id=light_token,
                    side=light_side,
                    price=record.price,
                    size=record.size,
                    placed_at=place_time,
                ))
                count += 1

        # Mark as boosted
        state.boosted_side = light_side
        state.boosted_at = time.time()
        # Update anchor for the light side
        if light_side == Side.UP:
            state.anchor_up = best_ask
        else:
            state.anchor_dn = best_ask

        logger.info(
            "BOOST: %s — reanchored %s side with %d rungs (half-width=%.3f)",
            mid, light_side.value, count, half_width,
        )
        return count

    def post_ladder(self, market: MarketWindow, spot_delta: float = 0.0) -> int:
        """Post a full ladder (both sides) for a market. Returns number of orders placed."""
        try:
            if self.risk.is_halted():
                return 0
            if market.market_id in self._killed_ladders:
                return 0
            # Don't re-create a ladder for a market that already has fills
            # (e.g. after boost cancelled the ladder state but positions remain)
            try:
                pos = self.positions.positions.get(market.market_id)
                if pos is not None and (pos.up_qty > 0 or pos.dn_qty > 0):
                    return 0
            except (TypeError, AttributeError):
                pass
            if not self.risk.can_open_position(self.positions.active_position_count()):
                return 0

            # Select timeframe-specific ladder parameters
            lp = self.cfg.get_ladder_params(market.timeframe_sec, current_bankroll=self.positions.bankroll)

            # Don't commit more capital than available bankroll
            available = self.positions.bankroll - self.total_committed()
            budget = min(
                self.positions.bankroll * lp.position_size_fraction,
                available,
            )
            # Scale down after consecutive losses
            ef = self.risk.exposure_factor()
            if ef < 1.0:
                budget *= ef
                logger.info("EXPOSURE FACTOR: %.2f — budget reduced to $%.2f", ef, budget)
            # Minimum capital guard: need enough for MIN_ORDER_SIZE on both sides
            min_required = MIN_ORDER_SIZE * 2.0
            if budget < min_required:
                logger.info("MIN CAPITAL: %s skipped — available $%.2f < min $%.2f",
                            market.market_id, budget, min_required)
                return 0

            tick_size = self.tick_cache.get_tick_size(market.condition_id, token_id=market.up_token_id) if self.tick_cache else 0.01

            best_ask_up = self.executor.get_best_ask(market.up_token_id)
            best_ask_dn = self.executor.get_best_ask(market.dn_token_id)

            # Both sides must have valid asks — don't post one-sided ladders
            if best_ask_up is None or best_ask_dn is None or best_ask_up <= 0 or best_ask_dn <= 0:
                logger.info("SKIP LADDER: %s — missing asks (UP=%s, DN=%s), will retry next cycle",
                            market.market_id, best_ask_up, best_ask_dn)
                return 0

            # Budget split: spot-aware skew based on BTC delta
            reduce_thresh = self.cfg.spot_delta_reduce_threshold
            skip_thresh = self.cfg.spot_delta_skip_threshold
            abs_delta = abs(spot_delta)

            if abs_delta >= skip_thresh:
                if spot_delta > 0:  # BTC up -> DN losing
                    budget_up = budget
                    budget_dn = 0.0
                else:
                    budget_up = 0.0
                    budget_dn = budget
                logger.info("SPOT SKIP: %s delta=%.3f%% — skipping %s side",
                            market.market_id, spot_delta * 100, "DN" if spot_delta > 0 else "UP")
            elif abs_delta >= reduce_thresh:
                losing_frac = 0.5 * (1.0 - (abs_delta - reduce_thresh) / (skip_thresh - reduce_thresh))
                winning_frac = 1.0 - losing_frac
                if spot_delta > 0:
                    budget_up = budget * winning_frac
                    budget_dn = budget * losing_frac
                else:
                    budget_up = budget * losing_frac
                    budget_dn = budget * winning_frac
                logger.info("SPOT SKEW: %s delta=%.3f%% — UP=$%.0f DN=$%.0f",
                            market.market_id, spot_delta * 100, budget_up, budget_dn)
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

            # Pair cost guard: check top-3-rung weighted average (those are what actually fill)
            # Uses fee-inclusive prices so that windows negative-EV after fees are rejected.
            if up_rungs and dn_rungs:
                top3_up = up_rungs[-3:] if len(up_rungs) >= 3 else up_rungs
                top3_dn = dn_rungs[-3:] if len(dn_rungs) >= 3 else dn_rungs
                top3_up_vwap = sum(
                    (p + compute_fee(p, self.fee_rate)) * s for p, s in top3_up
                ) / sum(s for _, s in top3_up)
                top3_dn_vwap = sum(
                    (p + compute_fee(p, self.fee_rate)) * s for p, s in top3_dn
                ) / sum(s for _, s in top3_dn)
                top3_pair = top3_up_vwap + top3_dn_vwap
                if top3_pair > lp.max_pair_cost:
                    logger.info(
                        "Pair cost guard: %s top-3 VWAP %.4f > %.4f (fee-inclusive), skipping",
                        market.market_id, top3_pair, lp.max_pair_cost,
                    )
                    return 0
                # Trim individual top rungs whose fee-inclusive price pair exceeds ceiling
                while len(up_rungs) > 1 and len(dn_rungs) > 1 and (
                    (up_rungs[-1][0] + compute_fee(up_rungs[-1][0], self.fee_rate))
                    + (dn_rungs[-1][0] + compute_fee(dn_rungs[-1][0], self.fee_rate))
                    > lp.max_pair_cost
                ):
                    if up_rungs[-1][0] >= dn_rungs[-1][0]:
                        up_rungs.pop()
                    else:
                        dn_rungs.pop()

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
                up_token_id=market.up_token_id,
                dn_token_id=market.dn_token_id,
                current_ask_up=best_ask_up,
                current_ask_dn=best_ask_dn,
                timeframe_sec=market.timeframe_sec,
            )

            # Skip if no rungs could be built on either side
            if not up_rungs and not dn_rungs:
                return 0

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

        return result["filled"]

    def process_paper_fills(self, paper_fills: list[dict]) -> list[TrackedOrder]:
        """Process pre-simulated fills from PaperClobClient.tick().
        Used in paper mode instead of check_fills to avoid reconcile misdetection."""
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
                order.market_id, order.side, fill_qty, self._fill_cost(order.price, fill_qty),
            )
            order.status = "filled"
            order.filled = fill_qty
            logger.info("FILL: %s %s %.1f @ $%.2f on %s",
                         order.side.value, order.token_id[:16],
                         fill_qty, order.price, order.market_id)
            filled.append(order)

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

            if best_ask_up is None or best_ask_dn is None:
                continue

            # Cache latest ask prices for dashboard
            state.current_ask_up = best_ask_up
            state.current_ask_dn = best_ask_dn

            up_moved = abs(best_ask_up - state.anchor_up) > self.cfg.reprice_threshold
            dn_moved = abs(best_ask_dn - state.anchor_dn) > self.cfg.reprice_threshold

            if not up_moved and not dn_moved:
                continue

            # Select timeframe-specific ladder parameters
            lp = self.cfg.get_ladder_params(market.timeframe_sec, current_bankroll=self.positions.bankroll)

            tick_size = self.tick_cache.get_tick_size(market.condition_id, token_id=market.up_token_id) if self.tick_cache else 0.01

            # Budget for reprice: only the REMAINING unfilled portion
            total_budget = self.positions.bankroll * lp.position_size_fraction
            available = self.positions.bankroll - self.total_committed()
            budget_per_side = min(total_budget / 2.0, max(0, available / 2.0))
            if budget_per_side < 1.0:
                continue

            try:
                # Compute existing filled VWAPs for pair cost check
                up_filled_qty = self.tracker.filled_qty(mid, Side.UP)
                dn_filled_qty = self.tracker.filled_qty(mid, Side.DOWN)
                up_filled_cost = self.tracker.filled_cost(mid, Side.UP)
                dn_filled_cost = self.tracker.filled_cost(mid, Side.DOWN)
                up_filled_vwap = up_filled_cost / up_filled_qty if up_filled_qty > 0 else 0
                dn_filled_vwap = dn_filled_cost / dn_filled_qty if dn_filled_qty > 0 else 0

                if up_moved:
                    if state.heavy_side_locked == "UP":
                        # UP is the locked heavy side — update anchor but don't repost
                        state.anchor_up = best_ask_up
                        logger.info("REPRICE SKIP UP: %s locked heavy side", mid)
                    else:
                        # Flush uncredited partial fills before cancelling the side
                        for order, delta in self.tracker.flush_uncredited(mid):
                            if order.side == Side.UP:
                                self.positions.update_position(
                                    order.market_id, order.side, delta, self._fill_cost(order.price, delta),
                                )
                        # Cancel unfilled UP rungs and repost
                        cancelled = self.tracker.cancel_side(mid, Side.UP)
                        self.executor.cancel_batch(cancelled)

                        # Only budget for unfilled portion of this side
                        side_budget = max(0, budget_per_side - up_filled_cost)
                        if side_budget >= 1.0:
                            up_rungs = build_ladder_rungs(
                                best_ask_up, side_budget,
                                lp.rungs, lp.spacing, lp.width, lp.size_skew,
                                tick_size=tick_size,
                            )
                            # Pair cost guard: trim rungs whose price + other side VWAP > max_pair_cost
                            if dn_filled_vwap > 0 and up_rungs:
                                up_rungs = [(p, s) for p, s in up_rungs if p + dn_filled_vwap <= lp.max_pair_cost]
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
                    if state.heavy_side_locked == "DOWN":
                        # DN is the locked heavy side — update anchor but don't repost
                        state.anchor_dn = best_ask_dn
                        logger.info("REPRICE SKIP DN: %s locked heavy side", mid)
                    else:
                        cancelled = self.tracker.cancel_side(mid, Side.DOWN)
                        self.executor.cancel_batch(cancelled)

                        side_budget = max(0, budget_per_side - dn_filled_cost)
                        if side_budget >= 1.0:
                            dn_rungs = build_ladder_rungs(
                                best_ask_dn, side_budget,
                                lp.rungs, lp.spacing, lp.width, lp.size_skew,
                                tick_size=tick_size,
                            )
                            # Pair cost guard: trim rungs whose price + other side VWAP > max_pair_cost
                            if up_filled_vwap > 0 and dn_rungs:
                                dn_rungs = [(p, s) for p, s in dn_rungs if p + up_filled_vwap <= lp.max_pair_cost]
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

                # Note: do NOT reset imbalance_accepted here — reprice should not undo imbalance guard
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
                # Severe imbalance: lock the heavy side to prevent further accumulation
                heavy_side = Side.UP if up_qty > dn_qty else Side.DOWN
                if state.heavy_side_locked != heavy_side.value:
                    state.heavy_side_locked = heavy_side.value
                    # Cancel the heavy side's resting orders immediately
                    # to stop further accumulation
                    cancelled = self.tracker.cancel_side(mid, heavy_side)
                    self.executor.cancel_batch(cancelled)
                    logger.info(
                        "IMBALANCE LOCK: %s — %s side locked, cancelled %d resting orders (UP=%.0f DN=%.0f)",
                        mid, heavy_side.value, len(cancelled), up_qty, dn_qty,
                    )

                if state.imbalance_alert_at is None:
                    state.imbalance_alert_at = now_epoch
                    logger.info(
                        "IMBALANCE %.0f%%: %s — UP=%.0f DN=%.0f, waiting for natural recovery",
                        imbalance * 100, mid, up_qty, dn_qty,
                    )
                elif now_epoch - state.imbalance_alert_at > timeout:
                    # Timeout: accept the imbalance as a directional position
                    logger.warning(
                        "IMBALANCE TIMEOUT: %s — accepting one-sided position (UP=%.0f DN=%.0f)",
                        mid, up_qty, dn_qty,
                    )
                    state.imbalance_accepted = True
                    state.imbalance_alert_at = None
                    acted.append(mid)
            else:
                # Clear any previous alert
                state.imbalance_alert_at = None

        return acted

    def check_loss_cap(self, spot_prices: dict[str, float], window_open_prices: dict[str, float] | None = None) -> list[str]:
        """Cancel remaining orders on positions whose one-sided loss exceeds threshold.

        For one-sided positions, the max loss is the total cost basis. If that exceeds
        max_loss_per_position, cancel remaining orders to prevent further accumulation.
        When window_open_prices is provided and spot moves against the one-sided position,
        the effective max loss is tightened by spot_loss_cap_multiplier.
        Returns list of market_ids where orders were cancelled.
        """
        max_loss = self.positions.bankroll * 0.05  # 5% of bankroll per position
        if max_loss < 5.0:
            max_loss = 5.0
        acted = []
        now = time.time()
        for mid, state in list(self.ladders.items()):
            # Grace period: don't kill a ladder that was just boosted — give rungs time to fill
            if state.boosted_at > 0 and now - state.boosted_at < 60.0:
                continue

            # Check position manager first (includes force-buy credits)
            pos = self.positions.positions.get(mid)
            if pos is not None and pos.up_qty > 0 and pos.dn_qty > 0:
                continue  # two-sided (possibly via force-buy), pair cost protects us

            up_qty = self.tracker.filled_qty(mid, Side.UP)
            dn_qty = self.tracker.filled_qty(mid, Side.DOWN)
            up_cost = self.tracker.filled_cost(mid, Side.UP)
            dn_cost = self.tracker.filled_cost(mid, Side.DOWN)

            # Only check one-sided positions (the risky ones)
            if up_qty > 0 and dn_qty > 0:
                continue  # two-sided, pair cost protects us
            if up_qty == 0 and dn_qty == 0:
                continue  # no fills yet

            cost_basis = up_cost + dn_cost

            # Tighten loss cap when spot confirms the losing direction
            effective_max_loss = max_loss
            if window_open_prices is not None:
                current = spot_prices.get(state.asset, 0.0)
                open_price = window_open_prices.get(mid, 0.0)
                if current > 0 and open_price > 0:
                    delta = (current - open_price) / open_price
                    spot_against = (
                        (up_qty > 0 and dn_qty == 0 and delta < -self.cfg.spot_delta_reduce_threshold) or
                        (dn_qty > 0 and up_qty == 0 and delta > self.cfg.spot_delta_reduce_threshold)
                    )
                    if spot_against:
                        effective_max_loss = max_loss * self.cfg.spot_loss_cap_multiplier

            if cost_basis > effective_max_loss:
                cancelled = self.cancel_ladder(mid)
                # Remove ladder state so reprice_if_needed won't iterate it
                del self.ladders[mid]
                # Block repost via post_ladder
                self._killed_ladders.add(mid)
                logger.warning(
                    "LOSS CAP: %s — one-sided cost $%.2f > max $%.2f, cancelled %d orders",
                    mid, cost_basis, effective_max_loss, cancelled,
                )
                acted.append(mid)
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

    def _check_one_side_cap(self, market_id: str) -> None:
        """Cancel heavy side resting orders when fill ratio > 3:1 AND heavy qty > 5.

        Called after fill detection and explicitly to prevent runaway one-sided exposure.
        """
        state = self.ladders.get(market_id)
        if state is None:
            return

        up_qty = self.tracker.filled_qty(market_id, Side.UP)
        dn_qty = self.tracker.filled_qty(market_id, Side.DOWN)

        if up_qty <= 0 and dn_qty <= 0:
            return

        max_qty = max(up_qty, dn_qty)
        min_qty = min(up_qty, dn_qty)

        # Need heavy side > 5 contracts AND ratio > 3:1
        if max_qty <= 5:
            return
        if min_qty > 0 and max_qty / min_qty <= 3.0:
            return

        heavy_side = Side.UP if up_qty > dn_qty else Side.DOWN

        # Cancel all resting orders on the heavy side
        cancelled = self.tracker.cancel_side(market_id, heavy_side)
        for oid in cancelled:
            self.executor.cancel_order(oid)
        self.tracker.confirm_cancels(cancelled)

        if cancelled:
            state.heavy_side_locked = heavy_side.value
            logger.warning(
                "ONE-SIDE CAP: %s — %s has %.0f fills (%.0f:%.0f) — cancelled %d resting rungs",
                market_id, heavy_side.value, max_qty, up_qty, dn_qty, len(cancelled),
            )

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

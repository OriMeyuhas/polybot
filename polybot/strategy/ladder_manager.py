"""Ladder manager: builds and maintains passive limit order ladders on every active market."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.fees import compute_fee
from polybot.order_executor import OrderExecutor
from polybot.order_tracker import OrderTracker, TrackedOrder
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.strategy.fair_value import certainty as fv_certainty
from polybot.tick_size_cache import round_to_tick
from polybot.types import MarketWindow, Side

logger = logging.getLogger(__name__)


MIN_REPRICE_INTERVAL = 10.0  # seconds between reprices — stay in queue for fills


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
    exit_done: bool = False  # True after exit sell placed
    chase_done: bool = False  # True after reactive chase pair placed
    directional_done: bool = False  # True after directional buy placed
    throttle_heavy_side: Side | None = None  # Set when imbalance > 0.30 but both sides have fills
    fv_cancel_history: list = field(default_factory=list)  # epoch timestamps of recent FV cancels
    is_directional: bool = False  # True if ladder was posted intentionally one-sided (FV gate, spot skip)
    # Cycle 29 — persist book-mid gate decision so reprice_if_needed() honors it.
    # Without this, reprice rebuilds bilateral ladders and nullifies the gate's
    # one-sided budget choice (cycle 28 root cause).
    gate_fired: bool = False
    gate_winner_side: Side | None = None  # side to post when gate fired
    gate_budget_cap: float = 0.0  # $ cap from book-mid gate's directional_budget_cap


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
        # Proposal #54: pending guard-fire events for bot loop to drain and record to activity log
        self._recent_aborts: list[dict] = []           # ONE-SIDED ABORT fires
        self._recent_circuit_breaker_fires: list[dict] = []  # FV CANCEL CIRCUIT BREAKER fires

    def _fill_cost(self, price: float, qty: float) -> float:
        """Fee-inclusive cost for qty shares at price."""
        return qty * (price + compute_fee(price, self.fee_rate))

    def has_ladder(self, market_id: str) -> bool:
        return market_id in self.ladders

    def is_killed(self, market_id: str) -> bool:
        """Return True if this market's ladder was killed by check_loss_cap."""
        return market_id in self._killed_ladders

    def post_ladder_pre_open(self, market: MarketWindow, spot_delta: float = 0.0, fair_up: float = 0.5, vol_annualized: float | None = None) -> int:
        """Post ladder for pre-open markets. Delegates to post_ladder."""
        return self.post_ladder(market, spot_delta=spot_delta, fair_up=fair_up, vol_annualized=vol_annualized)

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
        force_buy_pct = self.cfg.force_buy_elapsed_pct
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
        min_heavy = self.cfg.imbalance_min_heavy_fills
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
        force_buy_max = self.cfg.force_buy_max_pair_cost
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
                gate_fired=False,
                gate_reason="no_eval",
                book_mid=None,
                fv_price=None,
                fv_certainty=None,
                spread=None,
                origin="initial_post",
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

        if deficit > 50:
            logger.warning(
                "FORCE-BUY LARGE: %s deficit=%.0f shares — may only partially fill",
                mid, deficit,
            )

        return {
            "side": light_side,
            "price": estimated_avg_price,
            "qty": deficit,
            "pair_cost": hypothetical_pair_cost,
        }

    def sell_losing_side(self, market: MarketWindow, now: float, fair_up: float = 0.5) -> dict | None:
        """Exit a losing one-sided position by selling shares mid-window.

        Whale data shows 12.8% of trades are exits at avg $0.35, 55% elapsed.
        Instead of holding to settlement (lose 100% on losers), we sell at a loss
        that's smaller than the expected settlement loss.

        Returns dict with {side, price, qty, recovered} or None.
        """
        if not self.cfg.exit_enabled:
            return None

        mid = market.market_id
        if mid in self._killed_ladders:
            return None

        state = self.ladders.get(mid)
        if state is None:
            return None

        # Already exited
        if getattr(state, 'exit_done', False):
            return None

        # Check position first (needed for certainty-based exit check)
        pos = self.positions.positions.get(mid)
        if pos is None:
            return None

        up_qty = pos.up_qty
        dn_qty = pos.dn_qty

        if up_qty <= 0 and dn_qty <= 0:
            return None

        # Need significant imbalance to qualify as "losing"
        max_qty = max(up_qty, dn_qty)
        min_qty = min(up_qty, dn_qty)
        if min_qty > 0 and max_qty / min_qty < self.cfg.exit_min_loss_ratio:
            return None  # position is reasonably balanced
        if min_qty > 0:
            return None  # has some fills on both sides, not a clear loser

        if market.timeframe_sec <= 0:
            return None

        # Exit trigger: certainty-based (when fair value enabled) or elapsed-based (fallback)
        elapsed_frac = (now - market.open_epoch) / market.timeframe_sec
        if self.cfg.fair_value_enabled and fair_up != 0.5:
            # Compute certainty for the side we hold
            held_is_up = up_qty > dn_qty
            held_certainty = fair_up if held_is_up else (1.0 - fair_up)
            if held_certainty >= self.cfg.certainty_exit_threshold:
                return None  # model still thinks our side can win
            # Model says we're losing — exit regardless of elapsed
            logger.info("FV EXIT: %s — held %s certainty=%.1f%% < %.0f%% threshold",
                        mid, "UP" if held_is_up else "DN",
                        held_certainty * 100, self.cfg.certainty_exit_threshold * 100)
        else:
            # Fallback: elapsed-based trigger
            if elapsed_frac < self.cfg.exit_elapsed_pct:
                return None

        # Determine the losing side (one-sided = the side we hold)
        if up_qty > dn_qty:
            sell_side = Side.UP
            sell_token = market.up_token_id
            sell_qty = up_qty
        else:
            sell_side = Side.DOWN
            sell_token = market.dn_token_id
            sell_qty = dn_qty

        # Get current midpoint to set sell price
        sell_midpoint = self.executor.get_midpoint(sell_token)
        if sell_midpoint is None:
            return None

        # Don't sell if midpoint is too low (would recover almost nothing)
        if sell_midpoint < self.cfg.exit_min_price:
            return None

        # Sell at midpoint (passive limit sell — sit at the bid)
        sell_price = max(self.cfg.exit_min_price, sell_midpoint - 0.01)

        # Cancel remaining resting orders first
        cancelled = self.tracker.cancel_side(mid, sell_side)
        self.executor.cancel_batch(cancelled)
        # Also cancel other side since we're exiting
        other_side = Side.DOWN if sell_side == Side.UP else Side.UP
        cancelled_other = self.tracker.cancel_side(mid, other_side)
        self.executor.cancel_batch(cancelled_other)

        # Place sell order with GTD expiration
        expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)
        try:
            record = self.executor.place_limit_sell(
                sell_token, sell_price, sell_qty, mid, sell_side,
                expiration=expiration,
                gate_fired=False,
                gate_reason="no_eval",
                book_mid=None,
                fv_price=None,
                fv_certainty=None,
                spread=None,
                origin="initial_post",
            )
        except ClobApiError:
            return None

        # Track the sell order
        if record.order_id:
            self.tracker.add(TrackedOrder(
                order_id=record.order_id,
                market_id=mid,
                token_id=sell_token,
                side=sell_side,
                price=sell_price,
                size=sell_qty,
                placed_at=now,
            ))

        state.exit_done = True
        recovered = sell_price * sell_qty
        logger.info(
            "EXIT SELL: %s — selling %s %.0f @ $%.3f (midpoint=$%.3f), recovering ~$%.2f",
            mid, sell_side.value, sell_qty, sell_price, sell_midpoint, recovered,
        )

        return {
            "side": sell_side,
            "price": sell_price,
            "qty": sell_qty,
            "recovered": recovered,
        }

    def chase_pair(self, market: MarketWindow, now: float, fair_up: float = 0.5) -> int:
        """Reactive pairing: when one side fills, immediately post tight orders on the other side.

        Instead of waiting for the normal ladder to fill both sides, we actively
        chase pair completion by posting orders near midpoint on the unfilled side.

        Returns number of chase orders placed.
        """
        if not self.cfg.reactive_pairing_enabled:
            return 0

        mid = market.market_id
        if mid in self._killed_ladders:
            return 0

        state = self.ladders.get(mid)
        if state is None:
            return 0

        # Already boosted or chased — don't double up
        if getattr(state, 'chase_done', False):
            return 0

        # Need at least one fill on one side and zero on the other
        up_count = self.tracker.filled_count(mid, Side.UP)
        dn_count = self.tracker.filled_count(mid, Side.DOWN)

        if up_count == 0 and dn_count == 0:
            return 0  # no fills yet
        if up_count > 0 and dn_count > 0:
            return 0  # already have fills on both sides

        # Wait a bit after first fill to see if the other side fills naturally
        # (at least 10% of window or 30 seconds, whichever is less)
        first_fill_wait = min(market.timeframe_sec * 0.10, 30.0)
        if up_count > 0:
            first_fill_time = self._first_fill_time(mid, Side.UP)
        else:
            first_fill_time = self._first_fill_time(mid, Side.DOWN)
        if first_fill_time and now - first_fill_time < first_fill_wait:
            return 0

        # Determine chase side (the unfilled side)
        if up_count > 0 and dn_count == 0:
            chase_side = Side.DOWN
            chase_token = market.dn_token_id
        else:
            chase_side = Side.UP
            chase_token = market.up_token_id

        # Fair value guard: only chase the winning side to complete a profitable pair
        if self.cfg.fair_value_enabled and fair_up != 0.5:
            cert = fv_certainty(fair_up)
            if cert > 0.60:
                winning_side = Side.UP if fair_up > 0.5 else Side.DOWN
                if chase_side != winning_side:
                    logger.info("FV CHASE SKIP: %s — chase side %s is the LOSING side (P(UP)=%.1f%%)",
                                mid, chase_side.value, fair_up * 100)
                    return 0

        # Get current best ask for the chase side
        try:
            best_ask = self.executor.get_best_ask(chase_token)
        except ClobApiError:
            return 0
        if best_ask is None or best_ask <= 0:
            return 0

        # Use midpoint if best_ask looks like market creation artifact
        if best_ask >= 0.90:
            mid_price = self.executor.get_midpoint(chase_token)
            if mid_price and 0.01 < mid_price < 0.99:
                best_ask = mid_price + 0.01

        # Budget: fraction of remaining budget for this side
        lp = self.cfg.get_ladder_params(market.timeframe_sec, current_bankroll=self.positions.bankroll)
        total_budget = min(
            lp.position_size_fraction * self.positions.bankroll,
            self.positions.bankroll - self.total_committed(),
        )
        chase_filled_cost = self.tracker.filled_cost(mid, chase_side)
        side_budget = max(0, total_budget / 2.0 - chase_filled_cost)
        side_budget *= self.cfg.reactive_chase_budget_pct
        if side_budget < MIN_ORDER_SIZE * 0.5:
            return 0

        tick_size = self.tick_cache.get_tick_size(market.condition_id, token_id=market.up_token_id) if self.tick_cache else 0.01

        # Build tight chase ladder near midpoint
        chase_rungs = build_ladder_rungs(
            best_ask, side_budget,
            max(5, lp.rungs // 3),  # fewer rungs, tighter
            lp.spacing,
            self.cfg.reactive_chase_width,
            lp.size_skew,
            tick_size=tick_size,
            fee_rate=self.fee_rate,
        )

        # Pair cost guard on chase rungs
        filled_side = Side.UP if up_count > 0 else Side.DOWN
        filled_qty = self.tracker.filled_qty(mid, filled_side)
        filled_cost = self.tracker.filled_cost(mid, filled_side)
        if filled_qty > 0:
            filled_vwap = filled_cost / filled_qty
            chase_rungs = [
                (p, s) for p, s in chase_rungs
                if p + filled_vwap <= lp.max_pair_cost
            ]

        if not chase_rungs:
            return 0

        # Place chase orders with GTD expiration
        expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)
        _chase_gate_ctx = {
            "gate_fired": False,
            "gate_reason": "no_eval",
            "book_mid": None,
            "fv_price": None,
            "fv_certainty": None,
            "spread": None,
            "origin": "initial_post",
        }
        order_dicts = [
            {"token_id": chase_token, "price": price, "size": size,
             "market_id": mid, "side": chase_side,
             "expiration": expiration,
             **_chase_gate_ctx}
            for price, size in chase_rungs
        ]
        count = 0
        place_time = time.time()
        for record in self.executor.place_batch_limit_buys(order_dicts):
            if record.status != "error":
                self.tracker.add(TrackedOrder(
                    order_id=record.order_id,
                    market_id=mid,
                    token_id=chase_token,
                    side=chase_side,
                    price=record.price,
                    size=record.size,
                    placed_at=place_time,
                ))
                count += 1

        state.chase_done = True
        logger.info(
            "CHASE PAIR: %s — posted %d tight rungs on %s side (width=%.2f, budget=$%.2f)",
            mid, count, chase_side.value, self.cfg.reactive_chase_width, side_budget,
        )
        return count

    def directional_buy(self, market: MarketWindow, now: float, fair_up: float = 0.5) -> dict | None:
        """Late-window directional buy of the near-certain winning side.

        When certainty is high (>85%) late in the window, buy the winning side
        at a discount — it will settle at $1.00. This is the polytrader-style edge
        applied within our market-making framework.

        Returns dict with {side, price, qty, ev_per_share} or None.
        """
        if not self.cfg.fair_value_enabled:
            return None

        mid = market.market_id
        if mid in self._killed_ladders:
            return None

        state = self.ladders.get(mid)
        if state is None:
            return None

        # Already did a directional buy
        if getattr(state, 'directional_done', False):
            return None

        # Elapsed check: must be in directional phase
        if market.timeframe_sec <= 0:
            return None
        elapsed_frac = (now - market.open_epoch) / market.timeframe_sec
        if elapsed_frac < self.cfg.directional_phase_pct:
            return None

        # Certainty check
        cert = fv_certainty(fair_up)
        if cert < self.cfg.certainty_directional_threshold:
            return None

        # Determine winning side
        if fair_up > 0.5:
            buy_side = Side.UP
            buy_token = market.up_token_id
            p_fair = fair_up
        else:
            buy_side = Side.DOWN
            buy_token = market.dn_token_id
            p_fair = 1.0 - fair_up

        # Get best ask for winning side
        try:
            best_ask = self.executor.get_best_ask(buy_token)
        except ClobApiError:
            return None
        if best_ask is None or best_ask <= 0:
            return None

        # Price guard: don't overpay
        if best_ask > self.cfg.directional_max_ask:
            return None

        # EV check: p_fair - ask must exceed fee buffer (2c)
        fee_buffer = 0.02
        ev_per_share = p_fair - best_ask - fee_buffer
        if ev_per_share <= 0:
            return None

        # Budget: use remaining available capital
        lp = self.cfg.get_ladder_params(market.timeframe_sec, current_bankroll=self.positions.bankroll)
        available = self.positions.bankroll - self.total_committed()
        budget = min(lp.position_size_fraction * self.positions.bankroll * 0.5, available)
        if budget < MIN_ORDER_SIZE * best_ask:
            return None

        qty = budget / best_ask
        if qty < MIN_ORDER_SIZE:
            return None

        # Place order with GTD expiration
        expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)
        try:
            record = self.executor.place_limit_buy(
                buy_token, best_ask, qty, mid, buy_side,
                expiration=expiration,
                gate_fired=False,
                gate_reason="no_eval",
                book_mid=None,
                fv_price=fair_up,
                fv_certainty=cert,
                spread=None,
                origin="initial_post",
            )
        except ClobApiError:
            return None

        if record.order_id:
            self.tracker.add(TrackedOrder(
                order_id=record.order_id,
                market_id=mid,
                token_id=buy_token,
                side=buy_side,
                price=best_ask,
                size=qty,
                placed_at=now,
            ))

        state.directional_done = True
        logger.info(
            "DIRECTIONAL BUY: %s — %s %.0f @ $%.3f (P=%.1f%%, EV=$%.3f)",
            mid, buy_side.value, qty, best_ask, p_fair * 100, ev_per_share,
        )

        return {
            "side": buy_side,
            "price": best_ask,
            "qty": qty,
            "ev_per_share": ev_per_share,
        }

    def _first_fill_time(self, market_id: str, side: Side) -> float | None:
        """Return timestamp of the first fill on this side, or None."""
        for order in self.tracker.orders.values():
            if order.market_id == market_id and order.side == side and order.status == "filled":
                return order.placed_at
        return None

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
        boost_pct = self.cfg.boost_elapsed_pct
        if elapsed_frac < boost_pct:
            return 0

        # Determine heavy and light sides by filled count
        up_count = self.tracker.filled_count(mid, Side.UP)
        dn_count = self.tracker.filled_count(mid, Side.DOWN)
        min_heavy = self.cfg.imbalance_min_heavy_fills

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
                if not hasattr(self, '_spot_gate_logged'):
                    self._spot_gate_logged = set()
                if mid not in self._spot_gate_logged:
                    logger.info("SPOT GATE BOOST: %s delta=%.3f%% away from heavy=%s, skip",
                                 mid, spot_delta * 100, heavy_side.value)
                    self._spot_gate_logged.add(mid)
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

        # Place orders with GTD expiration
        expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)
        _boost_gate_ctx = {
            "gate_fired": False,
            "gate_reason": "no_eval",
            "book_mid": None,
            "fv_price": None,
            "fv_certainty": None,
            "spread": None,
            "origin": "initial_post",
        }
        order_dicts = [
            {"token_id": light_token, "price": price, "size": size,
             "market_id": mid, "side": light_side,
             "expiration": expiration,
             **_boost_gate_ctx}
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

    def post_ladder(self, market: MarketWindow, spot_delta: float = 0.0, fair_up: float = 0.5, vol_annualized: float | None = None) -> int:
        """Post a full ladder (both sides) for a market. Returns number of orders placed."""
        try:
            if self.risk.is_halted():
                return 0
            if market.market_id in self._killed_ladders:
                return 0
            # Late-window guard: if <20% of window remaining, only enter if FV
            # is confident enough to pick the winning side. Blind two-sided
            # ladders this late won't pair — guaranteed one-sided loss.
            _now = int(time.time())
            if market.timeframe_sec > 0 and market.close_epoch > _now:
                remaining_frac = market.remaining(_now) / market.timeframe_sec
                if remaining_frac < 0.20:
                    cert = fv_certainty(fair_up) if self.cfg.fair_value_enabled else 0.0
                    if cert < 0.60:
                        logger.info("SKIP LADDER: %s — %.0f%% remaining, certainty only %.0f%% (need 60%%), too risky",
                                    market.market_id, remaining_frac * 100, cert * 100)
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

            # Fix market-creation artifact: early-window books show best_ask=$0.99
            # (initial seed liquidity). Use midpoint as anchor instead — real market
            # makers populate both sides with tight spreads within seconds.
            if best_ask_up >= 0.90:
                mid_up = self.executor.get_midpoint(market.up_token_id)
                if mid_up and 0.01 < mid_up < 0.99:
                    best_ask_up = mid_up + tick_size
            if best_ask_dn >= 0.90:
                mid_dn = self.executor.get_midpoint(market.dn_token_id)
                if mid_dn and 0.01 < mid_dn < 0.99:
                    best_ask_dn = mid_dn + tick_size


            # Vol-aware width: scale ladder width by realized vol
            effective_width = lp.width
            if self.cfg.fair_value_enabled and vol_annualized is not None and vol_annualized > 0:
                vol_ratio = vol_annualized / self.cfg.vol_fallback_annual
                width_factor = max(0.6, min(1.5, vol_ratio))
                effective_width = lp.width * width_factor

            # Book-mid entry gate (Proposal #1, holdout-validated 2026-04-17).
            # Independent signal from the Binance-FV gate below. Reads CLOB book mid
            # at window open; when both sides have a tight/liquid book AND normalized
            # mid-based certainty (2*|book_mid_up - 0.5|) >= threshold, skips the
            # losing side and caps directional budget at directional_budget_cap.
            # Falls through silently (gate not fired) on any None / wide-spread / degenerate case.
            book_mid_gate_fired = False
            # Defaults for log instrumentation — overwritten inside the gate block when enabled
            _log_gate_reason = "no_eval"
            _log_book_mid: float | None = None
            _log_fv_certainty_gate: float | None = None
            _log_spread: float | None = None
            if self.cfg.book_mid_gate_enabled:
                _up_mid = self.executor.get_midpoint(market.up_token_id)
                _dn_mid = self.executor.get_midpoint(market.dn_token_id)
                _up_bid = self.executor.get_best_bid(market.up_token_id)
                _up_ask_bmg = self.executor.get_best_ask(market.up_token_id)
                _dn_bid = self.executor.get_best_bid(market.dn_token_id)
                _dn_ask_bmg = self.executor.get_best_ask(market.dn_token_id)
                _max_spread = self.cfg.book_mid_gate_max_spread
                # Cycle 19 instrumentation: classify non-fires so we can tell
                # whether the gate is silent because of missing data, wide
                # spreads, or insufficient certainty. DEBUG-level to avoid
                # log spam (fires every window).
                _has_all_data = (
                    _up_mid is not None and _dn_mid is not None
                    and _up_bid is not None and _up_ask_bmg is not None
                    and _dn_bid is not None and _dn_ask_bmg is not None
                    and (_up_mid + _dn_mid) > 0.0
                )
                _spread_up = (
                    (_up_ask_bmg - _up_bid)
                    if (_up_ask_bmg is not None and _up_bid is not None) else None
                )
                _spread_dn = (
                    (_dn_ask_bmg - _dn_bid)
                    if (_dn_ask_bmg is not None and _dn_bid is not None) else None
                )
                # Cycle 20/21: defensive guard against crossed books (bid > ask)
                # produced by stale/partial WS snapshots at window-open. Check
                # this FIRST so a crossed book never falls through into the
                # wide-spread or certainty branches.
                _is_crossed = (
                    (_spread_up is not None and _spread_up < 0)
                    or (_spread_dn is not None and _spread_dn < 0)
                )
                if _has_all_data and _is_crossed:
                    _spread_up_str = (
                        "%.4f" % _spread_up if _spread_up is not None else "None"
                    )
                    _spread_dn_str = (
                        "%.4f" % _spread_dn if _spread_dn is not None else "None"
                    )
                    logger.debug(
                        "BOOK MID GATE SKIP: %s reason=crossed_book "
                        "spread_up=%s spread_dn=%s cert=None",
                        market.market_id, _spread_up_str, _spread_dn_str,
                    )
                    _log_gate_reason = "crossed_book"
                    _log_book_mid = None
                    _log_fv_certainty_gate = None
                    _log_spread = _spread_up
                elif (
                    _has_all_data
                    and _spread_up is not None and _spread_up <= _max_spread
                    and _spread_dn is not None and _spread_dn <= _max_spread
                ):
                    _book_mid_up = _up_mid / (_up_mid + _dn_mid)
                    _cert_book = 2.0 * abs(_book_mid_up - 0.5)
                    if _cert_book >= self.cfg.book_mid_gate_certainty_threshold:
                        _dir_cap_bmg = self.cfg.directional_budget_cap
                        _capped_bmg = min(budget, _dir_cap_bmg)
                        if _book_mid_up > 0.5:
                            budget_up = _capped_bmg
                            budget_dn = 0.0
                            _side_label = "UP"
                        else:
                            budget_up = 0.0
                            budget_dn = _capped_bmg
                            _side_label = "DN"
                        book_mid_gate_fired = True
                        logger.info(
                            "BOOK MID GATE: %s cert=%.0f%% %s — skipping loser, "
                            "budget=$%.2f (cap=$%.2f) book_mid_up=%.3f",
                            market.market_id, _cert_book * 100, _side_label,
                            _capped_bmg, _dir_cap_bmg, _book_mid_up,
                        )
                        _log_gate_reason = "fired"
                        _log_book_mid = _book_mid_up
                        _log_fv_certainty_gate = _cert_book
                        _log_spread = _spread_up
                    else:
                        # Certainty too low — data is good, spreads are tight,
                        # but mid-based certainty didn't clear threshold.
                        _spread_up_str = "%.4f" % _spread_up
                        _spread_dn_str = "%.4f" % _spread_dn
                        _cert_str = "%.4f" % _cert_book
                        logger.debug(
                            "BOOK MID GATE SKIP: %s reason=certainty_too_low "
                            "spread_up=%s spread_dn=%s cert=%s",
                            market.market_id, _spread_up_str, _spread_dn_str, _cert_str,
                        )
                        _log_gate_reason = "fv_certainty_below_thresh"
                        _log_book_mid = _book_mid_up
                        _log_fv_certainty_gate = _cert_book
                        _log_spread = _spread_up
                elif _has_all_data:
                    # Spread too wide on one or both sides.
                    _spread_up_str = "%.4f" % _spread_up
                    _spread_dn_str = "%.4f" % _spread_dn
                    logger.debug(
                        "BOOK MID GATE SKIP: %s reason=spread_too_wide "
                        "spread_up=%s spread_dn=%s cert=None",
                        market.market_id, _spread_up_str, _spread_dn_str,
                    )
                    _log_gate_reason = "no_eval"
                    _log_book_mid = None
                    _log_fv_certainty_gate = None
                    _log_spread = _spread_up
                else:
                    # Missing bid/ask/mid data (or degenerate mids).
                    _spread_up_str = (
                        "%.4f" % _spread_up if _spread_up is not None else "None"
                    )
                    _spread_dn_str = (
                        "%.4f" % _spread_dn if _spread_dn is not None else "None"
                    )
                    logger.debug(
                        "BOOK MID GATE SKIP: %s reason=missing_bid_ask "
                        "spread_up=%s spread_dn=%s cert=None",
                        market.market_id, _spread_up_str, _spread_dn_str,
                    )
                    _log_gate_reason = "no_eval"
                    _log_book_mid = None
                    _log_fv_certainty_gate = None
                    _log_spread = None

                # Cycle 24 H0: when the book-mid gate is enabled AND did not fire,
                # skip the paired-ladder fallback entirely. Dome evidence (n=583,
                # t=0.55) shows gate-miss subset at -$4.04/mkt with 95% CI
                # [-5.42, -2.67]; live corroborates at -$12.23/mkt. The symmetric
                # paired ladder on gate-miss markets is negative-EV; refusing to
                # post captures the gate-fire subset's profitability (+$4.36/mkt,
                # 97.6% WR) while cutting the bleed. Flag-gated so optionality
                # is preserved — set SKIP_ON_GATE_MISS=false to restore pre-H0
                # behavior without a code change.
                if self.cfg.skip_on_gate_miss and not book_mid_gate_fired:
                    logger.info(
                        "PAIRED SKIP: gate_missed + skip_on_gate_miss=true %s",
                        market.market_id,
                    )
                    return 0

            # FV gate: if certainty > 80% at posting time, skip the losing side entirely.
            # Threshold raised from 0.60 to 0.80 — 60% gate fired too often (76-84% loss
            # rate on 452 zero-fill one-sided windows). Only block posting when very confident.
            # FV cancel (0.60) still cleans up AFTER posting. Late-window guard (0.60) unchanged.
            cert = fv_certainty(fair_up) if self.cfg.fair_value_enabled else 0.0
            # Hard cap on directional budget (Proposal #53): when FV gate or spot skip forces
            # 100% of budget onto one side, clamp to directional_budget_cap. This limits
            # worst-case adverse selection loss per window to ≤$20 (from -$27/-$30 outliers).
            # Orthogonal to is_directional flag — cap operates unconditionally on one-sided posts.
            dir_cap = self.cfg.directional_budget_cap
            if book_mid_gate_fired:
                # Book-mid gate already decided budget_up / budget_dn. Skip Binance-FV and
                # spot-delta branches — they would overwrite the gate's decision.
                pass
            elif self.cfg.fv_gate_enabled and cert >= 0.80:
                capped_budget = min(budget, dir_cap)
                if fair_up > 0.5:
                    # UP is winning — don't post DN
                    budget_up = capped_budget
                    budget_dn = 0.0
                    logger.info(
                        "FV GATE: %s certainty %.0f%% UP — DN skipped, UP budget=$%.2f (cap=$%.2f)",
                        market.market_id, cert * 100, capped_budget, dir_cap,
                    )
                else:
                    # DN is winning — don't post UP
                    budget_up = 0.0
                    budget_dn = capped_budget
                    logger.info(
                        "FV GATE: %s certainty %.0f%% DN — UP skipped, DN budget=$%.2f (cap=$%.2f)",
                        market.market_id, cert * 100, capped_budget, dir_cap,
                    )
            else:
                if not self.cfg.fv_gate_enabled and cert >= 0.80:
                    # Gate disabled — log what would have fired for observability / later analysis
                    p_up = fair_up
                    logger.info(
                        "FV GATE DISABLED: would have fired cert=%.2f p_up=%.2f on %s — posting bilateral",
                        cert, p_up, market.market_id,
                    )
                # Spot-delta based skew (mild, defensive only)
                reduce_thresh = self.cfg.spot_delta_reduce_threshold
                skip_thresh = self.cfg.spot_delta_skip_threshold
                abs_delta = abs(spot_delta)

                if abs_delta >= skip_thresh:
                    capped_budget = min(budget, dir_cap)
                    if spot_delta > 0:
                        budget_up = capped_budget
                        budget_dn = 0.0
                    else:
                        budget_up = 0.0
                        budget_dn = capped_budget
                    logger.info(
                        "SPOT SKIP: %s delta=%.3f%% — one-side budget=$%.2f (cap=$%.2f)",
                        market.market_id, spot_delta * 100, capped_budget, dir_cap,
                    )
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

            # Paired imbalance throttle: halve the heavy-side budget when throttle is active.
            # throttle_heavy_side is set by check_imbalance() when imbalance > 30% with fills
            # on both sides. This slows accumulation on the runaway side without killing it.
            existing_state = self.ladders.get(market.market_id)
            if existing_state is not None and existing_state.throttle_heavy_side is not None:
                throttle_side = existing_state.throttle_heavy_side
                if throttle_side == Side.UP:
                    budget_up = budget_up * 0.5
                    logger.info(
                        "THROTTLE APPLIED: %s — UP budget halved to $%.1f (heavy=UP)",
                        market.market_id, budget_up,
                    )
                elif throttle_side == Side.DOWN:
                    budget_dn = budget_dn * 0.5
                    logger.info(
                        "THROTTLE APPLIED: %s — DN budget halved to $%.1f (heavy=DN)",
                        market.market_id, budget_dn,
                    )

            up_rungs = build_ladder_rungs(
                best_ask_up, budget_up,
                lp.rungs, lp.spacing, effective_width, lp.size_skew,
                tick_size=tick_size,
            )
            dn_rungs = build_ladder_rungs(
                best_ask_dn, budget_dn,
                lp.rungs, lp.spacing, effective_width, lp.size_skew,
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
                    if not hasattr(self, '_pc_guard_logged'):
                        self._pc_guard_logged = {}
                    now_t = time.time()
                    last_log = self._pc_guard_logged.get(market.market_id, 0)
                    if now_t - last_log > 60:
                        logger.info(
                            "Pair cost guard: %s top-3 VWAP %.4f > %.4f (fee-inclusive), skipping",
                            market.market_id, top3_pair, lp.max_pair_cost,
                        )
                        self._pc_guard_logged[market.market_id] = now_t
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

            # GTD expiration: orders auto-cancel at window close minus safety buffer
            expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)

            # Gate instrumentation context for order log (pure observability, no behavior change)
            _gate_ctx = {
                "gate_fired": book_mid_gate_fired,
                "gate_reason": _log_gate_reason,
                "book_mid": _log_book_mid,
                "fv_price": fair_up,
                "fv_certainty": cert,
                "spread": _log_spread,
                "origin": "initial_post",
            }

            # Build batch order list for UP side
            up_order_dicts = [
                {"token_id": market.up_token_id, "price": price, "size": size,
                 "market_id": market.market_id, "side": Side.UP,
                 "expiration": expiration,
                 **_gate_ctx}
                for price, size in up_rungs
            ]
            # Build batch order list for DN side
            dn_order_dicts = [
                {"token_id": market.dn_token_id, "price": price, "size": size,
                 "market_id": market.market_id, "side": Side.DOWN,
                 "expiration": expiration,
                 **_gate_ctx}
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

            # Mark as directional if budget was fully allocated to one side
            # (FV gate, spot skip, or budget_dn/up == 0 for any reason).
            # This prevents #50 one-sided abort from killing intentional directional ladders.
            _is_directional = (budget_up <= 0.0 or budget_dn <= 0.0)

            # Cycle 29 — persist gate decision so reprice honors one-sided intent.
            if book_mid_gate_fired:
                _gate_winner = Side.UP if budget_dn <= 0.0 else Side.DOWN
                _gate_cap = _capped_bmg
            else:
                _gate_winner = None
                _gate_cap = 0.0

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
                is_directional=_is_directional,
                gate_fired=book_mid_gate_fired,
                gate_winner_side=_gate_winner,
                gate_budget_cap=_gate_cap,
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
                # Synchronous one-sided abort immediately after crediting each live fill
                self.check_one_sided_abort(order.market_id)

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

        # One-side cap check removed: _check_one_side_cap fired destructively in Cycles 9/10

        return result["filled"]

    def process_paper_fills(self, paper_fills: list[dict]) -> list[TrackedOrder]:
        """Process pre-simulated fills from PaperClobClient.tick().
        Used in paper mode instead of check_fills to avoid reconcile misdetection.
        Handles both BUY fills (add to position) and SELL fills (reduce position)."""
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
            fill_side_raw = fill.get("side", "BUY").upper()

            if fill_side_raw == "SELL":
                # SELL fill: reduce position and recover capital
                proceeds = order.price * fill_qty
                self.positions.reduce_position(
                    order.market_id, order.side, fill_qty, proceeds,
                )
                # Credit recovered capital to bankroll
                self.positions.bankroll += proceeds
                logger.info("SELL FILL: %s %s %.1f @ $%.2f on %s (recovered $%.2f)",
                             order.side.value, order.token_id[:16],
                             fill_qty, order.price, order.market_id, proceeds)
            else:
                # BUY fill: add to position
                self.positions.update_position(
                    order.market_id, order.side, fill_qty, self._fill_cost(order.price, fill_qty),
                )
                logger.info("FILL: %s %s %.1f @ $%.2f on %s",
                             order.side.value, order.token_id[:16],
                             fill_qty, order.price, order.market_id)

            order.status = "filled"
            order.filled = fill_qty
            order.credited_to_pm = fill_qty
            filled.append(order)

            # Synchronous one-sided abort: check immediately after crediting each fill
            # so adverse bursts are caught within the same tick. Only fires on BUY fills
            # (SELL fills reduce the position, can't build a one-sided BUY imbalance).
            if fill_side_raw == "BUY":
                self.check_one_sided_abort(order.market_id)

        # One-side cap check removed: _check_one_side_cap fired destructively in Cycles 9/10

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
            half_budget = min(total_budget / 2.0, max(0, available / 2.0))
            if half_budget < 1.0:
                continue

            try:
                # Compute existing filled VWAPs for pair cost check
                up_filled_qty = self.tracker.filled_qty(mid, Side.UP)
                dn_filled_qty = self.tracker.filled_qty(mid, Side.DOWN)
                up_filled_cost = self.tracker.filled_cost(mid, Side.UP)
                dn_filled_cost = self.tracker.filled_cost(mid, Side.DOWN)
                up_filled_vwap = up_filled_cost / up_filled_qty if up_filled_qty > 0 else 0
                dn_filled_vwap = dn_filled_cost / dn_filled_qty if dn_filled_qty > 0 else 0

                # Cycle 29 — gate persistence: if the book-mid gate fired when this
                # ladder was initially posted, reprice MUST honor the one-sided decision.
                # Otherwise reprice rebuilds a bilateral ladder and negates the gate's
                # loser-side suppression (root cause of cycle 28 losses).
                if state.gate_fired and state.gate_winner_side is not None:
                    if state.gate_winner_side == Side.UP:
                        budget_up_side = min(half_budget, state.gate_budget_cap)
                        budget_dn_side = 0.0
                    else:
                        budget_up_side = 0.0
                        budget_dn_side = min(half_budget, state.gate_budget_cap)
                    logger.debug(
                        "REPRICE gate-persist: %s winner=%s cap=$%.2f",
                        mid, state.gate_winner_side.value, state.gate_budget_cap,
                    )
                # Inventory-aware: skew budget toward the lighter side
                elif self.cfg.inventory_skew_enabled and up_filled_qty != dn_filled_qty:
                    skew_max = self.cfg.inventory_skew_max
                    if up_filled_qty > dn_filled_qty:
                        budget_up_side = half_budget * (1.0 - skew_max) / 0.5
                        budget_dn_side = half_budget * skew_max / 0.5
                    else:
                        budget_up_side = half_budget * skew_max / 0.5
                        budget_dn_side = half_budget * (1.0 - skew_max) / 0.5
                else:
                    budget_up_side = half_budget
                    budget_dn_side = half_budget

                # Gate instrumentation for reprice: emit persisted state, not re-evaluated gate.
                # (gate was evaluated once at post_ladder time; reprice logs what was decided then)
                _reprice_gate_ctx = {
                    "gate_fired": state.gate_fired,
                    "gate_reason": "fired" if state.gate_fired else "no_eval",
                    "book_mid": None,      # gate was evaluated at initial-post; no re-eval at reprice
                    "fv_price": None,
                    "fv_certainty": None,
                    "spread": None,
                    "origin": "reprice",
                }

                if up_moved:
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
                    side_budget = max(0, budget_up_side - up_filled_cost)
                    if side_budget >= 1.0:
                        up_rungs = build_ladder_rungs(
                            best_ask_up, side_budget,
                            lp.rungs, lp.spacing, lp.width, lp.size_skew,
                            tick_size=tick_size,
                        )
                        # Pair cost guard: trim rungs whose price + other side VWAP > max_pair_cost
                        if dn_filled_vwap > 0 and up_rungs:
                            up_rungs = [(p, s) for p, s in up_rungs if p + dn_filled_vwap <= lp.max_pair_cost]
                        reprice_expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)
                        up_order_dicts = [
                            {"token_id": market.up_token_id, "price": price, "size": size,
                             "market_id": mid, "side": Side.UP,
                             "expiration": reprice_expiration,
                             **_reprice_gate_ctx}
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
                    # Flush uncredited partial fills before cancelling the side
                    for order, delta in self.tracker.flush_uncredited(mid):
                        if order.side == Side.DOWN:
                            self.positions.update_position(
                                order.market_id, order.side, delta, self._fill_cost(order.price, delta),
                            )
                    cancelled = self.tracker.cancel_side(mid, Side.DOWN)
                    self.executor.cancel_batch(cancelled)

                    side_budget = max(0, budget_dn_side - dn_filled_cost)
                    if side_budget >= 1.0:
                        dn_rungs = build_ladder_rungs(
                            best_ask_dn, side_budget,
                            lp.rungs, lp.spacing, lp.width, lp.size_skew,
                            tick_size=tick_size,
                        )
                        # Pair cost guard: trim rungs whose price + other side VWAP > max_pair_cost
                        if up_filled_vwap > 0 and dn_rungs:
                            dn_rungs = [(p, s) for p, s in dn_rungs if p + up_filled_vwap <= lp.max_pair_cost]
                        reprice_expiration = int(market.close_epoch - self.cfg.no_trade_final_sec)
                        dn_order_dicts = [
                            {"token_id": market.dn_token_id, "price": price, "size": size,
                             "market_id": mid, "side": Side.DOWN,
                             "expiration": reprice_expiration,
                             **_reprice_gate_ctx}
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

    def cancel_losing_side_orders(self, market: MarketWindow, fair_up: float = 0.5) -> int:
        """Cancel resting orders on the side that fair value says is losing.

        When certainty > 70%, the market has moved enough that one side is
        likely to lose. Cancel that side's resting orders to prevent further
        adverse fills. This is PREVENTIVE (stop new fills) not reactive (sell held).

        Returns number of cancelled orders.
        """
        if not self.cfg.fair_value_enabled:
            return 0
        if fair_up == 0.5:
            return 0

        mid = market.market_id
        state = self.ladders.get(mid)
        if state is None:
            return 0
        if mid in self._killed_ladders:
            return 0

        cert = fv_certainty(fair_up)
        if cert < 0.90:
            return 0  # only cancel at very high certainty (calibration: 96.7% at cert>=0.90)

        # Only cancel in the last 150s of the window — FV is unreliable before that.
        # Researcher analysis: losing-side sweep starts at t=295s median, but FV accuracy
        # is only 56% mid-window. At t>=750s with cert>=0.90, accuracy is 96.7%.
        now_ts = time.time()
        elapsed = now_ts - market.open_epoch
        window_dur = market.close_epoch - market.open_epoch
        if window_dur > 0 and elapsed / window_dur < 0.83:
            return 0  # too early — FV not reliable yet

        # Determine losing side
        if fair_up > 0.5:
            losing_side = Side.DOWN  # UP is winning, DN is losing
        else:
            losing_side = Side.UP

        # Guard: if we already locked the OTHER side, don't flip — that would
        # cancel both sides and guarantee one-sided exposure regardless of outcome.
        if state.heavy_side_locked is not None and state.heavy_side_locked != losing_side.value:
            return 0

        # FV cancel circuit breaker: if FV has cancelled 3+ times in 60s, kill the ladder.
        # This prevents the FV-cancel/reprice ping-pong loop that causes $15+ losses.
        now_ts = time.time()
        state.fv_cancel_history = [t for t in state.fv_cancel_history if now_ts - t <= 60.0]
        if len(state.fv_cancel_history) >= 3:
            logger.warning(
                "FV CANCEL CIRCUIT BREAKER: market=%s fired %d times in 60s, killing ladder",
                mid, len(state.fv_cancel_history),
            )
            self._recent_circuit_breaker_fires.append({
                "market_id": mid,
                "asset": state.asset,
                "cancel_count": len(state.fv_cancel_history),
            })
            all_cancelled = self.tracker.cancel_market(mid)
            self.executor.cancel_batch(all_cancelled)
            self._killed_ladders.add(mid)
            return 0

        # Cancel losing side resting orders
        cancelled = self.tracker.cancel_side(mid, losing_side)
        if not cancelled:
            return 0

        self.executor.cancel_batch(cancelled)

        # Record this cancel in history for circuit breaker tracking
        state.fv_cancel_history.append(now_ts)

        # Lock the losing side so reprice doesn't repost
        state.heavy_side_locked = losing_side.value

        logger.info(
            "FV CANCEL LOSING: %s — certainty %.0f%%, cancelled %d %s resting orders",
            mid, cert * 100, len(cancelled), losing_side.value,
        )
        return len(cancelled)

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
            min_heavy_fills = self.cfg.imbalance_min_heavy_fills
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

            # Paired imbalance throttle: both sides have fills but ratio is skewed.
            # Unlike the lock (which fires only when light_count == 0), this guard fires
            # when light_count > 0 but imbalance is still > 30%.
            # Example: 172 UP vs 77 DN — imbalance = 0.55 — the old guard missed this.
            # When active, post_ladder halves the heavy-side budget so accumulation slows.
            if light_count > 0:
                heavy_side = Side.UP if up_qty > dn_qty else Side.DOWN
                if imbalance > 0.30:
                    if state.throttle_heavy_side != heavy_side:
                        state.throttle_heavy_side = heavy_side
                        logger.info(
                            "THROTTLE SET: %s — heavy=%s imbalance=%.0f%% (UP=%.0f DN=%.0f), "
                            "halving heavy-side budget on next post",
                            mid, heavy_side.value, imbalance * 100, up_qty, dn_qty,
                        )
                elif imbalance < 0.25 and state.throttle_heavy_side is not None:
                    # Hysteresis: clear throttle only when imbalance drops well below 0.30
                    logger.info(
                        "THROTTLE CLEARED: %s — imbalance=%.0f%% recovered (UP=%.0f DN=%.0f)",
                        mid, imbalance * 100, up_qty, dn_qty,
                    )
                    state.throttle_heavy_side = None

            # Early one-sided kill: if 100% one-sided and >25% of window elapsed, kill immediately
            # Data shows 95.5% of one-sided fills are adverse selection — cut losses fast
            min_qty_check = min(up_qty, dn_qty)
            if min_qty_check == 0 and max_qty > 0:
                elapsed_since_post = now_epoch - state.posted_at
                if elapsed_since_post > state.timeframe_sec * 0.25:
                    heavy_cost = self.tracker.filled_cost(mid, Side.UP if up_qty > dn_qty else Side.DOWN)
                    if heavy_cost > 3.0:  # at least $3 at risk
                        self.cancel_ladder(mid)
                        if mid in self.ladders:
                            del self.ladders[mid]
                        self._killed_ladders.add(mid)
                        logger.warning(
                            "ONE-SIDED ABORT: %s — 100%% one-sided at %.0f%% elapsed (UP=%.0f DN=%.0f cost=$%.2f), killed",
                            mid, (elapsed_since_post / state.timeframe_sec) * 100, up_qty, dn_qty, heavy_cost,
                        )
                        acted.append(mid)
                        continue

            # Extreme imbalance kill: ratio > 5 with significant cost → kill immediately
            min_qty = min(up_qty, dn_qty)
            if min_qty > 0:
                ratio = max_qty / min_qty
                heavy_cost = self.tracker.filled_cost(mid, Side.UP if up_qty > dn_qty else Side.DOWN)
                if ratio > 5.0 and heavy_cost > self.positions.bankroll * 0.02:
                    self.cancel_ladder(mid)
                    del self.ladders[mid]
                    self._killed_ladders.add(mid)
                    logger.warning(
                        "EXTREME IMBALANCE KILL: %s — ratio %.1f:1 (UP=%.0f DN=%.0f) cost=$%.2f, killed",
                        mid, ratio, up_qty, dn_qty, heavy_cost,
                    )
                    acted.append(mid)

        return acted

    def check_one_sided_abort(self, market_id: str) -> bool:
        """Kill-and-walk-away guard for synchronous fill-time calls.

        Called immediately after each fill is credited so adverse bursts are caught
        within the same tick rather than waiting for the main polling loop.

        Triggers on EITHER condition:
        - 100% one-sided AND total cost > 1% of bankroll (early burst detection)
        - Ratio > 3:1 AND total cost > $10.0 (accumulating imbalance)

        Does NOT re-enable lock / boost / chase — kill-and-walk-away only.
        Returns True if the ladder was killed, False otherwise.
        """
        if market_id in self._killed_ladders:
            return False
        if market_id not in self.ladders:
            return False

        state = self.ladders.get(market_id)
        up_qty = self.tracker.filled_qty(market_id, Side.UP)
        dn_qty = self.tracker.filled_qty(market_id, Side.DOWN)
        if up_qty + dn_qty < 1.0:
            return False

        # Grace period: bilateral ladders need time for both sides to fill.
        # Don't abort in the first 30 seconds — give the other side a chance.
        # After 30s, if still 100% one-sided, then it's a real adverse burst.
        now = time.time()
        if state is not None and state.posted_at > 0:
            elapsed_since_post = now - state.posted_at
            if elapsed_since_post < 30.0:
                return False

        # Skip check for intentionally directional ladders (FV gate / spot skip posted
        # a one-sided budget). These are EXPECTED to be single-side and the guard would
        # otherwise kill them on the first fill. Clear the flag once BOTH sides have
        # meaningful fills (reprice added the other side) so the guard reactivates.
        if state is not None and state.is_directional:
            if up_qty > 0 and dn_qty > 0:
                state.is_directional = False  # both sides now — re-enable guard
            else:
                return False

        up_cost = self.tracker.filled_cost(market_id, Side.UP)
        dn_cost = self.tracker.filled_cost(market_id, Side.DOWN)
        total_cost = up_cost + dn_cost

        bankroll = self.positions.bankroll
        cost_threshold_pct = bankroll * 0.01  # 1% of bankroll

        triggered = False
        # Condition 1: 100% one-sided with any meaningful cost
        if (up_qty == 0 or dn_qty == 0) and total_cost > cost_threshold_pct:
            triggered = True

        # Condition 2: ratio > 3:1 with absolute minimum $10 at risk
        if not triggered:
            min_qty = min(up_qty, dn_qty)
            max_qty = max(up_qty, dn_qty)
            if min_qty > 0 and max_qty / min_qty > 3.0 and total_cost > 10.0:
                triggered = True

        if triggered:
            logger.warning(
                "ONE-SIDED ABORT: market=%s up=%.1f dn=%.1f cost=$%.2f — killing ladder",
                market_id, up_qty, dn_qty, total_cost,
            )
            asset = state.asset if state is not None else "UNKNOWN"
            self._recent_aborts.append({
                "market_id": market_id,
                "asset": asset,
                "up_qty": up_qty,
                "dn_qty": dn_qty,
                "cost": total_cost,
            })
            self.cancel_ladder(market_id)
            if market_id in self.ladders:
                del self.ladders[market_id]
            self._killed_ladders.add(market_id)
            return True

        return False

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
                ratio = max(pos.up_qty, pos.dn_qty) / min(pos.up_qty, pos.dn_qty)
                if ratio < 3.0:
                    continue  # truly two-sided, pair cost protects us
                # else: highly imbalanced (e.g. 319:5), treat as one-sided and check loss cap

            up_qty = self.tracker.filled_qty(mid, Side.UP)
            dn_qty = self.tracker.filled_qty(mid, Side.DOWN)
            up_cost = self.tracker.filled_cost(mid, Side.UP)
            dn_cost = self.tracker.filled_cost(mid, Side.DOWN)

            # Only check one-sided positions (the risky ones)
            if up_qty > 0 and dn_qty > 0:
                ratio = max(up_qty, dn_qty) / min(up_qty, dn_qty)
                if ratio < 3.0:
                    continue  # truly two-sided, pair cost protects us
                # else: highly imbalanced, treat as one-sided and check loss cap
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
        """Cancel heavy side resting orders when fills become imbalanced.

        Triggers at 2:1 ratio to prevent runaway one-sided exposure.
        Called after fill detection on every tick.
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

        # Grace period: don't cap within first 5% of window — give BOTH sides
        # time to get their first fill before judging imbalance.
        # 15m = 45s grace, 1h = 180s grace
        grace_sec = state.timeframe_sec * 0.05
        now = time.time()
        if now - state.posted_at < grace_sec:
            return

        # Trigger: ratio > 3:1 after grace period
        if max_qty < MIN_ORDER_SIZE:
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

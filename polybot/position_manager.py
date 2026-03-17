"""Position manager: tracks open positions, computes sizing, manages bankroll."""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.types import Opportunity, Position, Side, StrategyType

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, cfg: BotConfig, bankroll: float):
        self.cfg = cfg
        self.bankroll = bankroll
        self.positions: dict[str, Position] = {}

    def compute_order_size(
        self,
        opp: Opportunity,
        book_depth: float,
    ) -> tuple[Side, float] | None:
        if opp.strategy != StrategyType.DIRECTIONAL or opp.side is None:
            return None

        max_capital = self.bankroll * self.cfg.position_size_fraction
        qty = max_capital / opp.price
        qty = min(qty, book_depth * self.cfg.max_book_depth_take_pct)

        if qty <= 0:
            return None

        return (opp.side, qty)

    def compute_spread_size(
        self,
        opp: Opportunity,
    ) -> tuple[float, float] | None:
        if opp.strategy != StrategyType.SPREAD:
            return None
        if opp.up_price is None or opp.dn_price is None:
            return None

        max_capital = self.bankroll * self.cfg.position_size_fraction
        budget_per_side = max_capital / 2.0

        qty_up = budget_per_side / opp.up_price
        qty_dn = budget_per_side / opp.dn_price
        qty = min(qty_up, qty_dn)

        pos = self.positions.get(opp.market_id, Position(market_id=opp.market_id))
        new_up_cost = pos.up_cost + qty * opp.up_price
        new_dn_cost = pos.dn_cost + qty * opp.dn_price
        new_min_qty = min(pos.up_qty + qty, pos.dn_qty + qty)

        if new_min_qty <= 0:
            return None

        pair_cost = (new_up_cost + new_dn_cost) / new_min_qty
        if pair_cost > self.cfg.max_pair_cost:
            logger.debug(
                "Spread rejected for %s: pair_cost=%.4f > %.4f",
                opp.market_id, pair_cost, self.cfg.max_pair_cost,
            )
            return None

        return (qty, qty)

    def update_position(self, market_id: str, side: Side, qty: float, cost: float):
        if market_id not in self.positions:
            self.positions[market_id] = Position(market_id=market_id)
        pos = self.positions[market_id]
        if side == Side.UP:
            pos.up_qty += qty
            pos.up_cost += cost
        else:
            pos.dn_qty += qty
            pos.dn_cost += cost

    def remove_position(self, market_id: str):
        self.positions.pop(market_id, None)

    def active_position_count(self) -> int:
        return len(self.positions)

    def update_bankroll(self, new_bankroll: float):
        self.bankroll = new_bankroll

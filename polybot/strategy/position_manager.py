"""Position manager: tracks open positions, computes sizing, manages bankroll."""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.types import Position, Side

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, cfg: BotConfig, bankroll: float):
        self.cfg = cfg
        self.bankroll = bankroll
        self.positions: dict[str, Position] = {}
        self._pending_settlement: set[str] = set()
        self._failed_settlement: set[str] = set()

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
        """Count positions excluding those pending or failed settlement."""
        return len(
            self.positions.keys()
            - self._pending_settlement
            - self._failed_settlement
        )

    def update_bankroll(self, new_bankroll: float):
        self.bankroll = new_bankroll

    def mark_pending_settlement(self, market_id: str) -> None:
        self._pending_settlement.add(market_id)

    def get_pending_settlements(self) -> list[str]:
        return list(self._pending_settlement)

    def mark_failed_settlement(self, market_id: str) -> None:
        self._pending_settlement.discard(market_id)
        self._failed_settlement.add(market_id)

    def get_failed_settlements(self) -> list[str]:
        return list(self._failed_settlement)

    def total_position_cost(self) -> float:
        """Total capital locked in filled positions (up_cost + dn_cost across all)."""
        return sum(p.up_cost + p.dn_cost for p in self.positions.values())

    def complete_settlement(self, market_id: str) -> None:
        self._pending_settlement.discard(market_id)
        self._failed_settlement.discard(market_id)

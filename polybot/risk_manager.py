"""Risk manager: drawdown circuit breaker, position limits, timing gates."""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.types import MarketWindow

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: BotConfig, starting_bankroll: float):
        self.cfg = cfg
        self.starting_bankroll = starting_bankroll
        self.daily_pnl: float = 0.0

    def can_open_position(self, current_count: int) -> bool:
        return current_count < self.cfg.max_concurrent_positions

    def is_halted(self) -> bool:
        max_loss = self.starting_bankroll * self.cfg.max_daily_drawdown_pct
        return self.daily_pnl <= -max_loss

    def update_pnl(self, amount: float):
        self.daily_pnl += amount
        if self.is_halted():
            logger.warning(
                "CIRCUIT BREAKER: daily PnL %.2f exceeds -%.1f%% of %.2f",
                self.daily_pnl,
                self.cfg.max_daily_drawdown_pct * 100,
                self.starting_bankroll,
            )

    def can_trade_in_window(self, market: MarketWindow, now_epoch: int) -> bool:
        if not market.is_active(now_epoch):
            return False
        return market.remaining(now_epoch) >= self.cfg.no_trade_final_sec

    def reset_daily(self):
        self.daily_pnl = 0.0
        logger.info("Daily PnL reset")

"""Risk manager: drawdown circuit breaker, position limits, timing gates, loss streaks."""

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
        self.consecutive_losses: int = 0
        self._max_concurrent_override: int | None = None

    def can_open_position(self, current_count: int, max_positions: int | None = None) -> bool:
        limit = max_positions or self._max_concurrent_override or self.cfg.max_concurrent_positions
        return current_count < limit

    def is_halted(self) -> bool:
        # Daily drawdown halt
        max_loss = self.starting_bankroll * self.cfg.max_daily_drawdown_pct
        if self.daily_pnl <= -max_loss:
            return True
        # Consecutive loss halt
        if self.consecutive_losses >= self.cfg.consecutive_loss_halt:
            return True
        return False

    def exposure_factor(self) -> float:
        """Scaling factor for position size based on consecutive losses.

        Returns 1.0 normally, 0.5 after 3+ consecutive losses.
        At consecutive_loss_halt (default 5), is_halted() kicks in instead.
        """
        if self.consecutive_losses >= 3:
            return 0.5
        return 1.0

    def update_pnl(self, amount: float):
        self.daily_pnl += amount
        if amount < 0:
            self.consecutive_losses += 1
        elif amount > 0:
            self.consecutive_losses = 0
        # amount == 0: no change to consecutive_losses

        if self.is_halted():
            if self.consecutive_losses >= self.cfg.consecutive_loss_halt:
                logger.warning(
                    "CONSECUTIVE LOSS HALT: %d losses in a row >= %d limit",
                    self.consecutive_losses,
                    self.cfg.consecutive_loss_halt,
                )
            else:
                logger.warning(
                    "CIRCUIT BREAKER: daily PnL %.2f exceeds -%.1f%% of %.2f",
                    self.daily_pnl,
                    self.cfg.max_daily_drawdown_pct * 100,
                    self.starting_bankroll,
                )

    def check_capital_at_risk(self, committed: float, bankroll: float) -> bool:
        """Return True if it's safe to open new positions.

        Args:
            committed: total capital committed (resting orders + filled positions)
            bankroll: current bankroll
        """
        if bankroll <= 0:
            return False
        return committed / bankroll <= self.cfg.max_capital_at_risk_pct

    def can_trade_in_window(self, market: MarketWindow, now_epoch: int) -> bool:
        if not market.is_active(now_epoch):
            return False
        return market.remaining(now_epoch) >= self.cfg.no_trade_final_sec

    def reset_daily(self):
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        logger.info("Daily PnL and consecutive losses reset")

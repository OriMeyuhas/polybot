"""No-op RiskManager stub — always allows trading.

Drop-in replacement for polybot.risk_manager.RiskManager.
Swap for the real implementation when risk management is added.
"""


class RiskStub:
    """Satisfies the RiskManager interface with no-op implementations."""

    def is_halted(self) -> bool:
        return False

    def can_open_position(self, current_count: int) -> bool:
        return True

    def can_trade_in_window(self, market, now_epoch: int) -> bool:
        return True

    def update_pnl(self, amount: float) -> None:
        pass

    def reset_daily(self) -> None:
        pass

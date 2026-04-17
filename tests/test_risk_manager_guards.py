"""Tests for RiskManager consecutive loss and capital-at-risk guards."""

import pytest
from polybot.config import BotConfig
from polybot.risk_manager import RiskManager


class TestConsecutiveLosses:
    def test_consecutive_losses_start_at_zero(self):
        cfg = BotConfig(dry_run=True)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        assert risk.consecutive_losses == 0

    def test_loss_increments_counter(self):
        cfg = BotConfig(dry_run=True)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        risk.update_pnl(-1.0)
        assert risk.consecutive_losses == 1

    def test_win_resets_counter(self):
        cfg = BotConfig(dry_run=True)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        risk.update_pnl(-1.0)
        risk.update_pnl(-1.0)
        assert risk.consecutive_losses == 2
        risk.update_pnl(1.0)
        assert risk.consecutive_losses == 0

    def test_zero_pnl_does_not_change_counter(self):
        cfg = BotConfig(dry_run=True)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        risk.update_pnl(-1.0)
        risk.update_pnl(0.0)
        assert risk.consecutive_losses == 1

    def test_exposure_factor_normal(self):
        """0-2 consecutive losses: exposure_factor = 1.0"""
        cfg = BotConfig(dry_run=True)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        assert risk.exposure_factor() == 1.0

    def test_exposure_factor_halved_at_3_losses(self):
        """3+ consecutive losses: exposure_factor = 0.5"""
        cfg = BotConfig(dry_run=True)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        risk.update_pnl(-1.0)
        risk.update_pnl(-1.0)
        risk.update_pnl(-1.0)
        assert risk.exposure_factor() == 0.5

    def test_exposure_factor_halved_at_4_losses(self):
        """4 consecutive losses: still 0.5"""
        cfg = BotConfig(dry_run=True)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        for _ in range(4):
            risk.update_pnl(-1.0)
        assert risk.exposure_factor() == 0.5

    def test_cancel_only_at_5_losses(self):
        """5+ consecutive losses: is_halted() returns True"""
        cfg = BotConfig(dry_run=True, consecutive_loss_halt=5)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        for _ in range(5):
            risk.update_pnl(-0.01)  # small losses, not enough for drawdown halt
        assert risk.is_halted() is True

    def test_drawdown_halt_still_works(self):
        """Daily drawdown halt takes priority even before 5 consecutive losses."""
        cfg = BotConfig(dry_run=True, max_daily_drawdown_pct=0.05)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        risk.update_pnl(-60.0)  # > 5% of 1000
        assert risk.is_halted() is True
        assert risk.consecutive_losses == 1

    def test_win_after_halt_resets_consecutive(self):
        """A win resets consecutive_losses even if daily drawdown is still active."""
        cfg = BotConfig(dry_run=True, consecutive_loss_halt=5)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        for _ in range(5):
            risk.update_pnl(-0.01)
        assert risk.is_halted() is True
        risk.update_pnl(1.0)
        assert risk.consecutive_losses == 0

    def test_reset_daily_clears_consecutive_losses(self):
        cfg = BotConfig(dry_run=True)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        risk.update_pnl(-1.0)
        risk.update_pnl(-1.0)
        risk.reset_daily()
        assert risk.consecutive_losses == 0


class TestCapitalAtRisk:
    def test_can_open_when_under_limit(self):
        cfg = BotConfig(dry_run=True, max_capital_at_risk_pct=0.40)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        # 30% committed, under 40%
        assert risk.check_capital_at_risk(300.0, 1000.0) is True

    def test_blocked_when_over_limit(self):
        cfg = BotConfig(dry_run=True, max_capital_at_risk_pct=0.40)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        # 50% committed, over 40%
        assert risk.check_capital_at_risk(500.0, 1000.0) is False

    def test_boundary_exactly_at_limit(self):
        cfg = BotConfig(dry_run=True, max_capital_at_risk_pct=0.40)
        risk = RiskManager(cfg, starting_bankroll=1000.0)
        # Exactly at 40% — allow it (not strictly greater)
        assert risk.check_capital_at_risk(400.0, 1000.0) is True

    def test_zero_bankroll_blocks(self):
        cfg = BotConfig(dry_run=True, max_capital_at_risk_pct=0.40)
        risk = RiskManager(cfg, starting_bankroll=0.0)
        assert risk.check_capital_at_risk(1.0, 0.0) is False

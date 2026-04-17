"""Tests for Phase 1F: Config validation for live mode."""

from polybot.config import BotConfig, validate_live_config


def test_defaults_pass_validation():
    """Default config should produce no errors."""
    cfg = BotConfig()
    errors = validate_live_config(cfg)
    assert errors == []


def test_rejects_high_position_fraction():
    """position_size_fraction=1.0 should be rejected."""
    cfg = BotConfig(position_size_fraction=1.0)
    errors = validate_live_config(cfg)
    assert any("position_size_fraction" in e for e in errors)


def test_rejects_high_drawdown():
    """max_daily_drawdown_pct=0.50 should be rejected."""
    cfg = BotConfig(max_daily_drawdown_pct=0.50)
    errors = validate_live_config(cfg)
    assert any("max_daily_drawdown_pct" in e for e in errors)


def test_rejects_too_many_positions():
    """max_concurrent_positions=25 should be rejected."""
    cfg = BotConfig(max_concurrent_positions=25)
    errors = validate_live_config(cfg)
    assert any("max_concurrent_positions" in e for e in errors)


def test_rejects_tiny_bankroll():
    """bankroll=$5 should be rejected."""
    cfg = BotConfig(bankroll=5.0)
    errors = validate_live_config(cfg)
    assert any("bankroll" in e for e in errors)


def test_rejects_huge_batch_size():
    """batch_order_size=100 should be rejected."""
    cfg = BotConfig(batch_order_size=100)
    errors = validate_live_config(cfg)
    assert any("batch_order_size" in e for e in errors)


def test_allows_valid_custom_config():
    """A custom but within-bounds config should pass."""
    cfg = BotConfig(
        position_size_fraction=0.10,
        max_daily_drawdown_pct=0.10,
        max_concurrent_positions=15,
        bankroll=500.0,
        batch_order_size=30,
    )
    errors = validate_live_config(cfg)
    assert errors == []

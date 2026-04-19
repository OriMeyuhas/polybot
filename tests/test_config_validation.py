"""Tests for Phase 1F: Config validation for live mode."""

from polybot.config import BotConfig, validate_live_config


def test_defaults_pass_validation():
    """Default config flags V2 placeholder addresses (expected — addresses are TODO stubs).

    The zero-placeholder pusd_address and collateral_onramp_address are intentionally
    flagged by validate_live_config to prevent live launch before they are set.
    All OTHER bounds checks must still pass.
    """
    cfg = BotConfig()
    errors = validate_live_config(cfg)
    # Only the V2 address placeholders should be flagged — nothing else
    non_addr_errors = [e for e in errors if "pusd_address" not in e and "collateral_onramp_address" not in e]
    assert non_addr_errors == [], f"Unexpected non-address errors: {non_addr_errors}"
    # The two address errors MUST be present (regression guard)
    assert any("pusd_address" in e for e in errors)
    assert any("collateral_onramp_address" in e for e in errors)


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
    """A fully-specified within-bounds config (including V2 addresses) should pass."""
    cfg = BotConfig(
        position_size_fraction=0.10,
        max_daily_drawdown_pct=0.10,
        max_concurrent_positions=15,
        bankroll=500.0,
        batch_order_size=30,
        # V2 collateral addresses — non-zero so address validation passes
        pusd_address="0x" + "a" * 40,
        usdc_address="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        collateral_onramp_address="0x" + "c" * 40,
    )
    errors = validate_live_config(cfg)
    assert errors == []

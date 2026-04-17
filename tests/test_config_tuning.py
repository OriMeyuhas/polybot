"""Tests for config and risk tuning changes (2026-03-30)."""
from polybot.config import BotConfig, load_bot_config


def test_max_imbalance_ratio_default():
    """BotConfig default max_imbalance_ratio should be 0.35."""
    cfg = BotConfig()
    assert cfg.max_imbalance_ratio == 0.60


def test_load_bot_config_imbalance_fallback(monkeypatch):
    """load_bot_config fallback for MAX_IMBALANCE_RATIO should be 0.35."""
    # Clear any env var so fallback is used
    monkeypatch.delenv("MAX_IMBALANCE_RATIO", raising=False)
    cfg = load_bot_config()
    assert cfg.max_imbalance_ratio == 0.60

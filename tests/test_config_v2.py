"""Guardrails for V2 config changes: chain rename, pUSD fields, V2 host."""

import os
from unittest.mock import patch

from polybot.config import BotConfig, load_bot_config


def test_botconfig_has_chain_field():
    """`chain` replaces `chain_id` in BotConfig."""
    cfg = BotConfig()
    assert hasattr(cfg, "chain")
    assert cfg.chain == 137


def test_botconfig_default_host_is_v2():
    cfg = BotConfig()
    assert cfg.polymarket_host == "https://clob-v2.polymarket.com"


def test_botconfig_has_pusd_fields():
    cfg = BotConfig()
    assert hasattr(cfg, "pusd_address")
    assert hasattr(cfg, "usdc_address")
    assert hasattr(cfg, "collateral_onramp_address")
    assert hasattr(cfg, "wrap_on_startup")
    assert cfg.wrap_on_startup is False


def test_load_bot_config_reads_chain_env():
    with patch.dict(os.environ, {"CHAIN": "137"}, clear=False):
        cfg = load_bot_config()
        assert cfg.chain == 137


def test_load_bot_config_falls_back_to_legacy_chain_id(caplog):
    """If CHAIN is missing but CHAIN_ID is set, use it with a deprecation warning."""
    with patch.dict(os.environ, {"CHAIN_ID": "137"}, clear=False):
        # Ensure CHAIN itself is not set
        os.environ.pop("CHAIN", None)
        cfg = load_bot_config()
        assert cfg.chain == 137
        # Warning message present somewhere in captured logs
        assert any("CHAIN_ID" in rec.message for rec in caplog.records)

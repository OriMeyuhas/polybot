"""Startup balance-gate behavior for the live path."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from polybot.config import BotConfig
from polybot.errors import LiveStartupError


def _live_cfg(**overrides):
    defaults = dict(
        dry_run=False,
        private_key="0x" + "1" * 64,
        api_key="k", api_secret="s", api_passphrase="p",
        pusd_address="0x" + "a" * 40,
        usdc_address="0x" + "b" * 40,
        collateral_onramp_address="0x" + "c" * 40,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _mock_sdk():
    """Returns a context manager that mocks the live SDK import."""
    return patch("py_clob_client_v2.client.ClobClient")


def test_no_pusd_no_usdc_raises_live_startup_error():
    from polybot.oms.clob_client import create_clob_client

    cfg = _live_cfg()
    wrapper = MagicMock()
    wrapper.pusd_balance.return_value = Decimal("0")
    wrapper.usdc_balance.return_value = Decimal("0")

    with _mock_sdk(), \
         patch("polybot.oms.collateral.PUsdWrapper", return_value=wrapper), \
         patch("web3.Web3"):
        with pytest.raises(LiveStartupError, match="No collateral"):
            create_clob_client(cfg, book_manager=None)


def test_has_pusd_no_wrap_needed():
    from polybot.oms.clob_client import create_clob_client

    cfg = _live_cfg()
    wrapper = MagicMock()
    wrapper.pusd_balance.return_value = Decimal("100")
    wrapper.usdc_balance.return_value = Decimal("0")

    with _mock_sdk(), \
         patch("polybot.oms.collateral.PUsdWrapper", return_value=wrapper), \
         patch("web3.Web3"):
        create_clob_client(cfg, book_manager=None)

    wrapper.wrap.assert_not_called()


def test_has_usdc_wrap_on_startup_false_raises():
    from polybot.oms.clob_client import create_clob_client

    cfg = _live_cfg(wrap_on_startup=False)
    wrapper = MagicMock()
    wrapper.pusd_balance.return_value = Decimal("0")
    wrapper.usdc_balance.return_value = Decimal("50")

    with _mock_sdk(), \
         patch("polybot.oms.collateral.PUsdWrapper", return_value=wrapper), \
         patch("web3.Web3"):
        with pytest.raises(LiveStartupError, match="USDC present"):
            create_clob_client(cfg, book_manager=None)

    wrapper.wrap.assert_not_called()


def test_has_usdc_wrap_on_startup_true_calls_wrap():
    from polybot.oms.clob_client import create_clob_client

    cfg = _live_cfg(wrap_on_startup=True)
    wrapper = MagicMock()
    wrapper.pusd_balance.side_effect = [Decimal("0"), Decimal("50")]  # before and after wrap
    wrapper.usdc_balance.return_value = Decimal("50")
    wrapper.wrap.return_value = "0x" + "f" * 64

    with _mock_sdk(), \
         patch("polybot.oms.collateral.PUsdWrapper", return_value=wrapper), \
         patch("web3.Web3"):
        create_clob_client(cfg, book_manager=None)

    wrapper.wrap.assert_called_once_with(Decimal("50"))


def test_zero_placeholder_addresses_rejected_in_live_validation():
    from polybot.config import validate_live_config

    cfg = BotConfig(
        dry_run=False,
        pusd_address="0x0000000000000000000000000000000000000000",
        usdc_address="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        collateral_onramp_address="0x0000000000000000000000000000000000000000",
    )
    errors = validate_live_config(cfg)
    assert any("pusd_address" in e for e in errors)
    assert any("collateral_onramp_address" in e for e in errors)

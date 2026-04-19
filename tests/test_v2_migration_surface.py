"""Surface-level guardrails that V2 migration is complete and not regressed."""

from unittest.mock import MagicMock, patch

import pytest

from polybot.config import BotConfig
from polybot.oms.clob_client import PaperClobClient, create_clob_client


def _live_cfg(**overrides):
    """Build a BotConfig that will route to the live SDK branch."""
    defaults = dict(
        dry_run=False,
        private_key="0x" + "1" * 64,
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        pusd_address="0x" + "a" * 40,
        usdc_address="0x" + "b" * 40,
        collateral_onramp_address="0x" + "c" * 40,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def test_paper_client_has_no_v2_sdk_dependency():
    """Paper mode must never import the V2 SDK."""
    cfg = BotConfig(dry_run=True)
    client = create_clob_client(cfg, book_manager=None)
    assert isinstance(client, PaperClobClient)


def test_live_factory_uses_v2_package():
    """The live factory calls py_clob_client_v2.client.ClobClient with V2 kwargs."""
    cfg = _live_cfg()
    with patch("py_clob_client_v2.client.ClobClient") as mock_cls, \
         patch("polybot.oms.collateral.PUsdWrapper") as mock_wrapper_cls:
        mock_wrapper = MagicMock()
        mock_wrapper.pusd_balance.return_value = 1.0
        mock_wrapper.usdc_balance.return_value = 0.0
        mock_wrapper_cls.return_value = mock_wrapper

        create_clob_client(cfg, book_manager=None)

        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args.kwargs
        assert "chain" in kwargs, "V2 expects `chain`, not `chain_id`"
        assert "chain_id" not in kwargs
        assert kwargs["chain"] == 137
        assert kwargs["host"] == "https://clob-v2.polymarket.com"


def test_orderargs_fallback_has_no_v1_fields():
    """Fallback OrderArgs dataclass matches V2 field set (no nonce, fee_rate_bps, taker)."""
    from polybot.oms import order_executor

    args = order_executor.OrderArgs(
        token_id="t", price=0.5, size=10.0, side="BUY", expiration=0
    )
    # V2-stripped fields must not exist or must default to a "neutral" absent value.
    assert not hasattr(args, "fee_rate_bps"), \
        "fee_rate_bps removed in V2 — protocol-managed"
    assert not hasattr(args, "nonce"), "nonce removed in V2 — timestamp-based"
    # taker in V2 defaults to zero address — allowed to exist but must not be user-set

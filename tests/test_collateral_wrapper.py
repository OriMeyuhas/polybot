"""Unit tests for PUsdWrapper with fully mocked web3."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest


def _mock_web3_with_contracts(usdc_balance=0, pusd_balance=0, allowance=0):
    """Build a mock web3 whose .eth.contract() returns stubs with balance/allowance methods."""
    w3 = MagicMock()
    usdc = MagicMock()
    usdc.functions.balanceOf.return_value.call.return_value = usdc_balance
    usdc.functions.allowance.return_value.call.return_value = allowance
    usdc.functions.approve.return_value.build_transaction.return_value = {"nonce": 0, "gas": 50000}

    pusd = MagicMock()
    pusd.functions.balanceOf.return_value.call.return_value = pusd_balance

    onramp = MagicMock()
    onramp.functions.wrap.return_value.build_transaction.return_value = {"nonce": 1, "gas": 100000}

    # Different ABIs → different contract objects. Use address to dispatch.
    def contract(address, abi):
        if address.endswith("usdc"):
            return usdc
        if address.endswith("pusd"):
            return pusd
        return onramp

    w3.eth.contract.side_effect = contract
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.gas_price = 30_000_000_000
    w3.eth.send_raw_transaction.return_value = b"\x01" * 32
    w3.eth.wait_for_transaction_receipt.return_value = MagicMock(status=1)
    w3.eth.account.sign_transaction.return_value = MagicMock(rawTransaction=b"signed")
    return w3


def test_usdc_balance_returns_human_decimal():
    from polybot.oms.collateral import PUsdWrapper

    w3 = _mock_web3_with_contracts(usdc_balance=12_000_000)  # 12 USDC (6 decimals)
    wrapper = PUsdWrapper(
        w3=w3, private_key="0x" + "1" * 64,
        onramp_address="0xonramp", usdc_address="0xusdc", pusd_address="0xpusd",
    )
    assert wrapper.usdc_balance() == Decimal("12")


def test_pusd_balance_returns_human_decimal():
    from polybot.oms.collateral import PUsdWrapper

    w3 = _mock_web3_with_contracts(pusd_balance=5_500_000)  # 5.5 pUSD
    wrapper = PUsdWrapper(
        w3=w3, private_key="0x" + "1" * 64,
        onramp_address="0xonramp", usdc_address="0xusdc", pusd_address="0xpusd",
    )
    assert wrapper.pusd_balance() == Decimal("5.5")


def test_wrap_approves_then_calls_wrap():
    from polybot.oms.collateral import PUsdWrapper

    w3 = _mock_web3_with_contracts(usdc_balance=10_000_000, allowance=0)
    wrapper = PUsdWrapper(
        w3=w3, private_key="0x" + "1" * 64,
        onramp_address="0xonramp", usdc_address="0xusdc", pusd_address="0xpusd",
    )
    tx_hash = wrapper.wrap(Decimal("10"))
    assert isinstance(tx_hash, str)
    assert tx_hash.startswith("0x")
    # Two transactions sent: approve, then wrap
    assert w3.eth.send_raw_transaction.call_count == 2


def test_wrap_skips_approve_when_allowance_sufficient():
    from polybot.oms.collateral import PUsdWrapper

    w3 = _mock_web3_with_contracts(usdc_balance=10_000_000, allowance=100_000_000)
    wrapper = PUsdWrapper(
        w3=w3, private_key="0x" + "1" * 64,
        onramp_address="0xonramp", usdc_address="0xusdc", pusd_address="0xpusd",
    )
    wrapper.wrap(Decimal("10"))
    # One transaction: wrap only
    assert w3.eth.send_raw_transaction.call_count == 1


def test_wrap_raises_when_receipt_reverts():
    from polybot.oms.collateral import PUsdWrapper, WrapFailed

    w3 = _mock_web3_with_contracts(usdc_balance=10_000_000, allowance=100_000_000)
    w3.eth.wait_for_transaction_receipt.return_value = MagicMock(status=0)
    wrapper = PUsdWrapper(
        w3=w3, private_key="0x" + "1" * 64,
        onramp_address="0xonramp", usdc_address="0xusdc", pusd_address="0xpusd",
    )
    with pytest.raises(WrapFailed):
        wrapper.wrap(Decimal("10"))

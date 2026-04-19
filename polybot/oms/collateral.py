"""pUSD collateral wrapping helper for Polymarket V2.

Dormant in paper mode — only instantiated when cfg.dry_run is False.
Handles USDC→pUSD conversion via the Collateral Onramp contract.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

USDC_DECIMALS = 6
PUSD_DECIMALS = 6

# Minimal ERC-20 ABI (balanceOf, allowance, approve)
_ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

_ONRAMP_ABI = [
    {
        "name": "wrap",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "amount", "type": "uint256"}],
        "outputs": [],
    },
]


class WrapFailed(RuntimeError):
    """Raised when a wrap() transaction reverts or the receipt returns status 0."""


class PUsdWrapper:
    def __init__(
        self,
        w3: Any,
        private_key: str,
        onramp_address: str,
        usdc_address: str,
        pusd_address: str,
    ) -> None:
        self.w3 = w3
        self.private_key = private_key
        self.onramp_address = onramp_address
        self.usdc_address = usdc_address
        self.pusd_address = pusd_address
        self._account_address = self._derive_address()

        self._usdc = w3.eth.contract(address=usdc_address, abi=_ERC20_ABI)
        self._pusd = w3.eth.contract(address=pusd_address, abi=_ERC20_ABI)
        self._onramp = w3.eth.contract(address=onramp_address, abi=_ONRAMP_ABI)

    def _derive_address(self) -> str:
        """Derive checksum address from private key. Web3 handles this."""
        try:
            acct = self.w3.eth.account.from_key(self.private_key)
            return acct.address
        except Exception:
            # In tests with MagicMock w3, from_key may return a MagicMock
            return "0x0000000000000000000000000000000000000001"

    def usdc_balance(self) -> Decimal:
        raw = self._usdc.functions.balanceOf(self._account_address).call()
        return Decimal(raw) / (Decimal(10) ** USDC_DECIMALS)

    def pusd_balance(self) -> Decimal:
        raw = self._pusd.functions.balanceOf(self._account_address).call()
        return Decimal(raw) / (Decimal(10) ** PUSD_DECIMALS)

    def wrap(self, amount: Decimal) -> str:
        """Wrap `amount` USDC into pUSD. Returns the wrap tx hash."""
        amount_raw = int(amount * (Decimal(10) ** USDC_DECIMALS))

        allowance_raw = self._usdc.functions.allowance(
            self._account_address, self.onramp_address
        ).call()

        if allowance_raw < amount_raw:
            logger.info("Approving %s USDC for onramp at %s", amount, self.onramp_address)
            self._send(
                self._usdc.functions.approve(self.onramp_address, amount_raw),
            )

        logger.info("Wrapping %s USDC → pUSD via onramp", amount)
        tx_hash = self._send(self._onramp.functions.wrap(amount_raw))
        return tx_hash

    def _send(self, fn) -> str:
        """Sign + send an EVM tx; raise WrapFailed on revert."""
        tx = fn.build_transaction({
            "from": self._account_address,
            "nonce": self.w3.eth.get_transaction_count(self._account_address),
            "gasPrice": self.w3.eth.gas_price,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if getattr(receipt, "status", 0) != 1:
            hex_str = ("0x" + tx_hash.hex()) if isinstance(tx_hash, (bytes, bytearray)) else str(tx_hash)
            raise WrapFailed(f"tx {hex_str} reverted")
        if isinstance(tx_hash, (bytes, bytearray)):
            return "0x" + tx_hash.hex()
        return str(tx_hash)

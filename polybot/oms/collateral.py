"""Collateral management — pUSD/USDC balance wrapper.

Stub module: PUsdWrapper is a placeholder until Task 7 implements the real
pUSD balance gate. Importing this module must always succeed so that
test mocks and the live factory can reference it safely.
"""

import logging

logger = logging.getLogger(__name__)


class PUsdWrapper:
    """Placeholder for Task 7 pUSD balance gate implementation.

    Task 7 will replace this with a real wrapper that queries the pUSD
    contract balance and triggers USDC → pUSD conversion if needed.
    """

    def __init__(self, cfg):
        self._cfg = cfg

    def pusd_balance(self) -> float:
        """Return pUSD balance. Placeholder returns 0."""
        return 0.0

    def usdc_balance(self) -> float:
        """Return USDC balance. Placeholder returns 0."""
        return 0.0

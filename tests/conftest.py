"""Global test fixtures.

Key fixture: `no_collateral_gate` — patches `_ensure_collateral` to a no-op
for all tests except those in `test_live_startup_pusd_gate.py`, which test
the gate itself and must exercise the real implementation.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _no_collateral_gate_outside_gate_tests(request):
    """Patch _ensure_collateral to a no-op in all tests EXCEPT the gate test module.

    Tests in test_live_startup_pusd_gate.py patch PUsdWrapper themselves and
    must exercise the real _ensure_collateral logic; everything else should not
    make network calls to polygon-rpc.com.
    """
    # Check if this test is from the gate test module
    if request.fspath.basename == "test_live_startup_pusd_gate.py":
        # Gate tests exercise _ensure_collateral directly — do not patch it
        yield
        return

    with patch("polybot.oms.clob_client._ensure_collateral", return_value=None):
        yield

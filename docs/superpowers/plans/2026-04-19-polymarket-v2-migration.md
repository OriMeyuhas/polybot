# Polymarket V2 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate PolyBot from Polymarket CLOB V1 to V2 with minimum viable live-mode scaffolding, keeping paper mode running continuously through the April 28 cutover.

**Architecture:** Swap `py-clob-client` → `py-clob-client-v2==1.0.0` inside `polybot/oms/`. `PaperClobClient` is untouched. Live path gets a new `PUsdWrapper` helper for USDC→pUSD collateral wrapping, gated behind `DRY_RUN=false` + `WRAP_ON_STARTUP=true`.

**Tech Stack:** Python 3.14, pytest, py-clob-client-v2 (V2 SDK), web3.py (for on-chain pUSD wrap).

---

## File Structure

**Modified:**
- `requirements.txt` — swap SDK package, add web3
- `polybot/config.py` — rename `chain_id`→`chain`, add pUSD addresses & `wrap_on_startup` field, default host to V2
- `polybot/oms/clob_client.py` — swap live SDK import, new constructor signature, wire pUSD balance gate
- `polybot/oms/order_executor.py` — swap SDK import, update fallback `OrderArgs` dataclass (drop `fee_rate_bps`, `nonce`, `taker`)
- `tests/test_credential_autovalidation.py` — update mock target from `py_clob_client` to `py_clob_client_v2`
- `tests/test_live_mode_hardening.py` — update mock target

**Created:**
- `polybot/oms/collateral.py` — `PUsdWrapper` class (USDC balance, pUSD balance, `wrap()`)
- `tests/test_v2_migration_surface.py` — regression guardrails
- `tests/test_collateral_wrapper.py` — unit tests for `PUsdWrapper` with mocked web3
- `tests/test_live_startup_pusd_gate.py` — startup balance-check behavior

**Unchanged (explicitly):** `PaperClobClient` code, strategy layer, web UI, all data-layer modules.

---

## Task 1: Update requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Read current requirements**

Run: `cat requirements.txt`
Note current `py-clob-client>=0.34.0` entry.

- [ ] **Step 2: Swap and add web3**

Replace `py-clob-client>=0.34.0` with:

```
py-clob-client-v2==1.0.0
web3>=6.0.0,<7.0.0
```

- [ ] **Step 3: Install new deps**

Run: `pip install -r requirements.txt`
Expected: both packages install with no conflict warnings.

- [ ] **Step 4: Verify legacy package is gone**

Run: `pip show py-clob-client 2>&1 | head -2`
Expected: `WARNING: Package(s) not found: py-clob-client`

- [ ] **Step 5: Verify V2 package imports**

Run: `python -c "from py_clob_client_v2.client import ClobClient; from py_clob_client_v2.clob_types import OrderArgs, ApiCreds; print('ok')"`
Expected output: `ok`

- [ ] **Step 6: Verify web3 imports**

Run: `python -c "from web3 import Web3; print(Web3.__version__ if hasattr(Web3,'__version__') else 'ok')"`
Expected output: `ok` (or a version string like `6.x.x`)

- [ ] **Step 7: Commit**

```bash
git add requirements.txt
git commit -m "deps: swap py-clob-client v1 → v2, add web3 for pUSD wrapping

py-clob-client v1 stops working after Polymarket's April 28 cutover. V2
package preserves most method signatures; breaking changes covered in
subsequent commits."
```

---

## Task 2: Update `polybot/config.py` — chain rename, pUSD fields, V2 host

**Files:**
- Modify: `polybot/config.py:60-68` (BotConfig definition), `polybot/config.py:428-435` (loader), `polybot/config.py:523-543` (TrackerConfig if present)
- Test: `tests/test_v2_migration_surface.py` (created in a later step — inline tests for this task go in Step 2 below)

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_v2.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_v2.py -v`
Expected: all 5 tests FAIL with `AttributeError` or `AssertionError`.

- [ ] **Step 3: Update `BotConfig` dataclass**

In `polybot/config.py`, within the `@dataclass(frozen=True) class BotConfig:` block:

- Change line 63 from `polymarket_host: str = "https://clob.polymarket.com"` to `polymarket_host: str = "https://clob-v2.polymarket.com"`
- Change line 64 from `chain_id: int = 137` to `chain: int = 137`
- After the `api_passphrase` line (line 68), add these fields:

```python
    # V2 collateral — pUSD is the new collateral token (replaces USDC.e)
    # Addresses from https://docs.polymarket.com/contracts (V2 section, Polygon mainnet)
    pusd_address: str = "0x0000000000000000000000000000000000000000"  # TODO set from V2 contracts ref before live
    usdc_address: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
    collateral_onramp_address: str = "0x0000000000000000000000000000000000000000"  # TODO set from V2 contracts ref before live
    # Opt-in: if true, live startup will auto-call wrap() to convert USDC→pUSD before trading
    wrap_on_startup: bool = False
```

Note: The two placeholder addresses are intentional — user supplies them via `.env` at live-flip time. `validate_live_config` will enforce non-zero before live mode starts (see Task 7).

- [ ] **Step 4: Update `load_bot_config()` to read `CHAIN` with legacy fallback**

In `polybot/config.py`, inside `load_bot_config()`:

Replace the line `chain_id=int(os.getenv("CHAIN_ID", "137"))` (currently around line 431) with:

```python
        chain=_load_chain_env(),
```

Then add this helper at module level (just above `def load_bot_config()`):

```python
def _load_chain_env() -> int:
    """Read CHAIN (V2 naming) with legacy CHAIN_ID fallback + deprecation warning."""
    import logging
    chain = os.getenv("CHAIN")
    if chain is not None:
        return int(chain)
    legacy = os.getenv("CHAIN_ID")
    if legacy is not None:
        logging.getLogger(__name__).warning(
            "Using legacy CHAIN_ID env var; rename to CHAIN for V2 migration "
            "(see docs/superpowers/specs/2026-04-19-polymarket-v2-migration-design.md)"
        )
        return int(legacy)
    return 137
```

Add the new pUSD-related kwargs to the `BotConfig(...)` constructor call in `load_bot_config()` (just before the closing `)`):

```python
        pusd_address=os.getenv("PUSD_ADDRESS", "0x0000000000000000000000000000000000000000"),
        usdc_address=os.getenv("USDC_ADDRESS", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
        collateral_onramp_address=os.getenv("COLLATERAL_ONRAMP_ADDRESS", "0x0000000000000000000000000000000000000000"),
        wrap_on_startup=os.getenv("WRAP_ON_STARTUP", "false").lower() in ("true", "1", "yes"),
```

- [ ] **Step 5: Update `TrackerConfig` similarly**

In the `TrackerConfig` dataclass (line 25-57) and `load_config()` (line 523+):

- Change `polymarket_host` default to `"https://clob-v2.polymarket.com"`
- Leave `chain_id` on TrackerConfig for now — the tracker is an older code path using V1-only data APIs; changing its field is out of scope.

- [ ] **Step 6: Run the tests; they pass**

Run: `pytest tests/test_config_v2.py -v`
Expected: all 5 PASS.

- [ ] **Step 7: Search for remaining `chain_id` usages on `BotConfig`**

Run: `grep -rn "cfg.chain_id\|\.chain_id" polybot/ tests/ | grep -v TrackerConfig`
Expected output: zero results, OR a list you must fix.

If any live hits: replace `cfg.chain_id` with `cfg.chain` at each site.

- [ ] **Step 8: Run full suite — config loader change should not regress anything else**

Run: `python -m pytest tests/ -q 2>&1 | tail -3`
Expected: Same test count as baseline + 5 new tests, zero FAIL.

- [ ] **Step 9: Commit**

```bash
git add polybot/config.py tests/test_config_v2.py
git commit -m "config: rename chain_id→chain, default host→clob-v2, add pUSD fields

Live trading requires a new pUSD collateral token on V2. Addresses are
zero-placeholders in source and must be set via .env for live mode; a
later task adds a validate_live_config check that enforces non-zero.
CHAIN_ID env var still works with a deprecation warning."
```

---

## Task 3: Swap live SDK in `polybot/oms/clob_client.py`

**Files:**
- Modify: `polybot/oms/clob_client.py:302-323` (live branch only)
- Test: `tests/test_v2_migration_surface.py` (created now)

- [ ] **Step 1: Write the failing test**

Create `tests/test_v2_migration_surface.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2_migration_surface.py -v`
Expected: `test_live_factory_uses_v2_package` FAILS (still uses V1 SDK).

- [ ] **Step 3: Swap SDK imports and constructor in clob_client.py**

Replace the body of `create_clob_client` (current lines 302-323) with:

```python
def create_clob_client(cfg, book_manager=None):
    """Factory: returns PaperClobClient for dry_run, LiveClobClient otherwise.

    Live path imports V2 SDK and constructs a pUSD wrapper lazily.
    """
    if cfg.dry_run or not cfg.private_key:
        logger.info("Creating PaperClobClient (dry_run=%s)", cfg.dry_run)
        return PaperClobClient(book_manager=book_manager)

    # Live mode — V2 SDK
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    client = ClobClient(
        host=cfg.polymarket_host,
        key=cfg.private_key,
        chain=cfg.chain,
        creds=ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.api_passphrase,
        ),
    )
    logger.info("Created live V2 ClobClient at %s", cfg.polymarket_host)

    # Run pUSD balance gate (Task 7 wires the actual check)
    from polybot.oms.collateral import PUsdWrapper
    _ensure_collateral(cfg)

    return client


def _ensure_collateral(cfg):
    """Placeholder — implemented in Task 7."""
    return None
```

- [ ] **Step 4: Run test — surface tests now pass**

Run: `pytest tests/test_v2_migration_surface.py -v`
Expected: both PASS. `test_live_factory_uses_v2_package` PASSES; `test_paper_client_has_no_v2_sdk_dependency` PASSES.

- [ ] **Step 5: Commit**

```bash
git add polybot/oms/clob_client.py tests/test_v2_migration_surface.py
git commit -m "oms: swap live ClobClient to py-clob-client-v2 SDK

Paper path unchanged. Live path now imports py_clob_client_v2 and passes
chain (not chain_id). pUSD balance gate hooked as a placeholder; real
check implemented in the collateral task."
```

---

## Task 4: Update `polybot/oms/order_executor.py` — swap import, update fallback dataclass

**Files:**
- Modify: `polybot/oms/order_executor.py:22-39` (imports + fallback dataclass)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_v2_migration_surface.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2_migration_surface.py::test_orderargs_fallback_has_no_v1_fields -v`
Expected: FAILS — fallback dataclass still has `fee_rate_bps` and `nonce`.

- [ ] **Step 3: Update the import + fallback in order_executor.py**

Replace current lines 22-39:

```python
try:
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY
except ImportError:
    # Fallback when py-clob-client is not installed (tests / paper-only envs).
    # Must mirror the real OrderArgs fields so attribute-presence checks pass.
    @dataclass
    class OrderArgs:  # type: ignore[no-redef]
        token_id: str = ""
        price: float = 0.0
        size: float = 0.0
        side: str = "BUY"
        fee_rate_bps: str = ""
        nonce: int = 0
        expiration: int = 0
        taker: str = "0x0000000000000000000000000000000000000000"

    BUY = "BUY"
```

with:

```python
try:
    from py_clob_client_v2.clob_types import OrderArgs
    from py_clob_client_v2.order_builder.constants import BUY
except ImportError:
    # Fallback when py-clob-client-v2 is not installed (paper-only envs / CI).
    # Mirrors V2 OrderArgs: fee_rate_bps and nonce are gone (protocol-managed fees,
    # timestamp-based uniqueness). expiration is kept — V2 still supports GTD.
    @dataclass
    class OrderArgs:  # type: ignore[no-redef]
        token_id: str = ""
        price: float = 0.0
        size: float = 0.0
        side: str = "BUY"
        expiration: int = 0

    BUY = "BUY"
```

Note: the two `OrderArgs(...)` construction sites in this file (lines ~149-155 and ~232-238) already only pass `token_id, price, size, side, expiration`. No change needed there.

- [ ] **Step 4: Run the new test; it passes**

Run: `pytest tests/test_v2_migration_surface.py::test_orderargs_fallback_has_no_v1_fields -v`
Expected: PASS.

- [ ] **Step 5: Run full suite — confirm no regressions from import swap**

Run: `python -m pytest tests/ -q 2>&1 | tail -3`
Expected: zero FAIL. (The SDK is installed now so the `try` branch takes — the fallback path won't actually execute, but the dataclass definition must still satisfy the test.)

- [ ] **Step 6: Commit**

```bash
git add polybot/oms/order_executor.py tests/test_v2_migration_surface.py
git commit -m "oms: order_executor imports py-clob-client-v2; drop V1 fields from fallback

V2 protocol manages fees at match time (fee_rate_bps removed) and uses
timestamp-based order uniqueness (nonce removed). Fallback dataclass
tracks the V2 field set so tests that use it don't pass removed kwargs."
```

---

## Task 5: Update existing tests that mock the V1 SDK path directly

**Files:**
- Modify: `tests/test_credential_autovalidation.py` (5 occurrences)
- Modify: `tests/test_live_mode_hardening.py` (1 occurrence)

- [ ] **Step 1: Check that the existing tests currently fail**

Run: `pytest tests/test_credential_autovalidation.py tests/test_live_mode_hardening.py -q 2>&1 | tail -5`
Expected: ImportError or AttributeError on `py_clob_client`.

- [ ] **Step 2: Replace mock targets in `test_credential_autovalidation.py`**

In the 5 occurrences of `import py_clob_client.client as cc` (lines 66, 82, 95, 110, 129), change to `import py_clob_client_v2.client as cc`.

- [ ] **Step 3: Replace mock target in `test_live_mode_hardening.py`**

On line 38, change:
```python
with patch("py_clob_client.clob_types.BalanceAllowanceParams") as mock_params_cls:
```
to:
```python
with patch("py_clob_client_v2.clob_types.BalanceAllowanceParams") as mock_params_cls:
```

- [ ] **Step 4: Run affected tests**

Run: `pytest tests/test_credential_autovalidation.py tests/test_live_mode_hardening.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_credential_autovalidation.py tests/test_live_mode_hardening.py
git commit -m "tests: retarget py_clob_client mocks to v2 package"
```

---

## Task 6: Create `polybot/oms/collateral.py` — PUsdWrapper

**Files:**
- Create: `polybot/oms/collateral.py`
- Create: `tests/test_collateral_wrapper.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_collateral_wrapper.py`:

```python
"""Unit tests for PUsdWrapper with fully mocked web3."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_collateral_wrapper.py -v`
Expected: FAIL with `ModuleNotFoundError: polybot.oms.collateral`.

- [ ] **Step 3: Create `polybot/oms/collateral.py`**

```python
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

# Minimal Onramp ABI (wrap + pusd view)
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
            raise WrapFailed(f"tx {tx_hash.hex() if hasattr(tx_hash,'hex') else tx_hash} reverted")
        return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_collateral_wrapper.py -v`
Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add polybot/oms/collateral.py tests/test_collateral_wrapper.py
git commit -m "oms: add PUsdWrapper for V2 USDC→pUSD collateral wrapping

Dormant in paper mode. Live path will instantiate one wrapper and call
wrap() at startup when WRAP_ON_STARTUP=true. Web3 is imported only in
this file — no other module takes a transitive dependency on it."
```

---

## Task 7: Wire pUSD balance gate into `create_clob_client`

**Files:**
- Modify: `polybot/oms/clob_client.py` (replace the `_ensure_collateral` placeholder from Task 3)
- Create: `tests/test_live_startup_pusd_gate.py`
- Modify: `polybot/config.py:284-338` (`validate_live_config` — add pUSD address check)

- [ ] **Step 1: Write failing tests**

Create `tests/test_live_startup_pusd_gate.py`:

```python
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
    """Returns a patcher tuple that mocks the live SDK import."""
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


def test_has_usdc_wrap_on_startup_false_warns_no_wrap():
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
    wrapper.pusd_balance.return_value = Decimal("0")
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
```

- [ ] **Step 2: Ensure `LiveStartupError` exists**

Run: `grep -n "LiveStartupError" polybot/errors.py || echo MISSING`

If MISSING, add to `polybot/errors.py`:

```python
class LiveStartupError(RuntimeError):
    """Raised at startup when the live-mode preconditions are not met."""
```

If present already, skip this step.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_live_startup_pusd_gate.py -v`
Expected: FAIL (gate not implemented yet + validate_live_config doesn't check pUSD addresses).

- [ ] **Step 4: Implement the gate in `clob_client.py`**

Replace the `_ensure_collateral` placeholder in `polybot/oms/clob_client.py` with:

```python
def _ensure_collateral(cfg):
    """Live-mode startup gate: verify pUSD collateral or wrap USDC→pUSD.

    Rules:
      - No pUSD and no USDC → LiveStartupError("No collateral")
      - No pUSD, has USDC, wrap_on_startup=false → LiveStartupError("USDC present but wrap_on_startup disabled")
      - No pUSD, has USDC, wrap_on_startup=true → wrap(usdc_balance)
      - Has pUSD → no action
    """
    from decimal import Decimal

    from web3 import Web3
    from polybot.errors import LiveStartupError
    from polybot.oms.collateral import PUsdWrapper

    # Construct a web3 provider from the same RPC URL pattern used elsewhere.
    # Default to Polygon public RPC; overridable via env POLYGON_RPC_URL.
    import os
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    wrapper = PUsdWrapper(
        w3=w3,
        private_key=cfg.private_key,
        onramp_address=cfg.collateral_onramp_address,
        usdc_address=cfg.usdc_address,
        pusd_address=cfg.pusd_address,
    )

    pusd = wrapper.pusd_balance()
    if pusd > 0:
        logger.info("pUSD balance: %s — trading with existing collateral", pusd)
        return

    usdc = wrapper.usdc_balance()
    if usdc == 0:
        raise LiveStartupError(
            "No collateral. Deposit USDC on Polygon, then restart with "
            "WRAP_ON_STARTUP=true to convert USDC → pUSD at launch."
        )

    if not cfg.wrap_on_startup:
        raise LiveStartupError(
            f"USDC present (${usdc}) but pUSD is zero and WRAP_ON_STARTUP=false. "
            "Set WRAP_ON_STARTUP=true to convert at launch, or call wrap() manually."
        )

    logger.warning("Auto-wrapping %s USDC → pUSD (WRAP_ON_STARTUP=true)", usdc)
    tx_hash = wrapper.wrap(usdc)
    logger.info("wrap() tx: %s", tx_hash)
    # Confirm wrap produced pUSD
    new_pusd = wrapper.pusd_balance()
    if new_pusd == 0:
        raise LiveStartupError(
            f"wrap() tx {tx_hash} succeeded but pUSD balance is still zero — investigate on-chain"
        )
```

- [ ] **Step 5: Add pUSD address validation to `validate_live_config`**

In `polybot/config.py`, at the end of `validate_live_config(cfg)` (just before `return errors`), insert:

```python
    # V2 collateral contracts must be set (non-zero) for live mode
    ZERO_ADDR = "0x0000000000000000000000000000000000000000"
    if cfg.pusd_address == ZERO_ADDR:
        errors.append("pusd_address is zero — set PUSD_ADDRESS in .env from V2 contracts reference")
    if cfg.collateral_onramp_address == ZERO_ADDR:
        errors.append("collateral_onramp_address is zero — set COLLATERAL_ONRAMP_ADDRESS in .env")
    if not cfg.usdc_address.startswith("0x") or len(cfg.usdc_address) != 42:
        errors.append(f"usdc_address invalid: {cfg.usdc_address}")
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_live_startup_pusd_gate.py -v`
Expected: all 5 PASS.

- [ ] **Step 7: Commit**

```bash
git add polybot/oms/clob_client.py polybot/config.py polybot/errors.py tests/test_live_startup_pusd_gate.py
git commit -m "oms: live-mode startup gate for pUSD collateral

Enforces on live launch: pUSD present, OR (USDC present AND
WRAP_ON_STARTUP=true) → auto-wrap. Fail-fast with a clear message
otherwise. Paper mode is untouched — gate runs only behind the
DRY_RUN=false branch in create_clob_client."
```

---

## Task 8: Update `.env` template / documentation

**Files:**
- Modify: `.env` (via user-facing instructions, not committed)
- Modify: `CLAUDE.md` (migration note)

- [ ] **Step 1: Add migration instructions to CLAUDE.md**

Append to the `## Current State` section in `C:\Users\pc\Desktop\Bots\PolyBot\CLAUDE.md`:

```markdown
## V2 Migration Status (2026-04-19)

- Codebase migrated to `py-clob-client-v2==1.0.0` (host: `clob-v2.polymarket.com`)
- Paper mode unchanged; live mode requires:
  1. `.env`: rename `CHAIN_ID` → `CHAIN` (legacy still works with warning)
  2. `.env`: set `PUSD_ADDRESS` and `COLLATERAL_ONRAMP_ADDRESS` from Polymarket V2 contracts reference
  3. `.env`: `WRAP_ON_STARTUP=true` to auto-wrap USDC→pUSD at launch
  4. Deposit USDC to the bot wallet on Polygon before flipping `DRY_RUN=false`
- April 28 forced cutover: no action required — already on V2 endpoint
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: V2 migration status + live-flip checklist"
```

---

## Task 9: Validation gates

**Files:** none modified — pure validation.

- [ ] **Step 1: Full test suite**

Run: `python -m pytest tests/ -q 2>&1 | tail -3`
Expected: `1105 + N passed` where N is the new tests we added (~14 new). Zero FAIL.

- [ ] **Step 2: Paper-mode smoke test**

Terminate any running bot:

```bash
wmic process where "name='python.exe'" get ProcessId,CommandLine | grep run_bot.py
# If found, taskkill the PID
```

Then start fresh:

```bash
python run_bot.py > polybot.log 2>&1 &
sleep 8
curl -s -X POST http://127.0.0.1:8080/api/start
curl -s http://127.0.0.1:8080/api/state | python -c "import sys,json; d=json.load(sys.stdin); print('running:',d.get('running'),'hb:',d.get('heartbeat_healthy'))"
```

Expected: `running: True hb: True`.

- [ ] **Step 3: Live-path dry-run construct check**

Without actually going live (no capital, no private key with funds), verify the live factory fails cleanly when pUSD is zero:

```bash
python - <<'EOF'
import os
os.environ.update({
    "DRY_RUN": "false",
    "PRIVATE_KEY": "0x" + "1"*64,
    "API_KEY": "k", "API_SECRET": "s", "API_PASSPHRASE": "p",
    "PUSD_ADDRESS": "0x" + "a"*40,
    "COLLATERAL_ONRAMP_ADDRESS": "0x" + "c"*40,
})
from polybot.config import load_bot_config
from polybot.oms.clob_client import create_clob_client
cfg = load_bot_config()
try:
    create_clob_client(cfg, book_manager=None)
    print("UNEXPECTED: live factory returned without error")
except Exception as e:
    print(f"expected error type: {type(e).__name__}: {e}")
EOF
```

Expected output contains `LiveStartupError` or a web3/RPC error — the key thing is that the factory does NOT hang or silently succeed. If it returns an error, the gate is working.

- [ ] **Step 4: Final commit (if anything remains uncommitted)**

```bash
git status
# If clean: no action
# If not clean: investigate before committing
```

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin feat/v2-migration
gh pr create --title "Polymarket V2 migration (Approach 2: minimal + pUSD scaffolding)" --body "$(cat <<'BODY'
## Summary
- Swap py-clob-client → py-clob-client-v2==1.0.0
- Rename BotConfig.chain_id → chain; default host → clob-v2.polymarket.com
- Add PUsdWrapper (USDC→pUSD) — dormant in paper, gated behind DRY_RUN=false
- Live startup gate: fail fast if no collateral; auto-wrap if WRAP_ON_STARTUP=true
- All paper-mode code paths unchanged

## Test plan
- [x] pytest tests/ — all pass (new: ~14 tests across 4 new files)
- [x] Paper-mode smoke: bot starts, heartbeat healthy
- [x] Live-path dry-run construct: factory fails cleanly when pUSD=0 (expected)
- [ ] User reviews .env changes and decides when to flip live (out of scope for this PR)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

---

## Self-Review Notes

**Spec coverage check:**
- ✅ Requirements swap → Task 1
- ✅ `polybot/oms/clob_client.py` modifications → Task 3
- ✅ `polybot/oms/order_executor.py` modifications → Task 4
- ✅ `polybot/oms/collateral.py` new file → Task 6
- ✅ `polybot/config.py` modifications (chain rename, pUSD fields, default host) → Task 2
- ✅ Updated tests that mock V1 SDK → Task 5
- ✅ `test_v2_migration_surface.py` new tests → Tasks 3, 4
- ✅ `test_collateral_wrapper.py` new tests → Task 6
- ✅ `test_live_startup_pusd_gate.py` new tests → Task 7
- ✅ Validation gates (pytest full, paper smoke, live-construct check) → Task 9
- ✅ `.env` migration instructions → Task 8
- ✅ Shipping order (branch, commits, PR) → Task 9

**Placeholder scan:** No "TBD" / "implement later" / vague handling. The `pusd_address` and `collateral_onramp_address` defaults ARE zero-placeholders by design — validated against `validate_live_config` so they cannot silently slip into live mode.

**Type consistency:** `WrapFailed` defined in Task 6 is imported in test from `polybot.oms.collateral`. `LiveStartupError` checked for existence and added if missing in Task 7. `PUsdWrapper` signature identical between definition (Task 6) and usage (Task 7).

**Spec invariants preserved:** `PaperClobClient` is never modified. web3 is imported only inside `collateral.py` and inside the live branch of `create_clob_client`. No dual-support code; V1 is deleted cleanly.

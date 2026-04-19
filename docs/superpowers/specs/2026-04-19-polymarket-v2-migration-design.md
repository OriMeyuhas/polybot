# Polymarket V2 Migration — Design

**Date:** 2026-04-19
**Forced cutover:** 2026-04-28 ~11:00 UTC
**Strategy:** Migrate now to `clob-v2.polymarket.com`, stay in paper mode post-migration, defer live capital deposit to a later decision.

## Goal

Keep PolyBot trading past April 28 and have the live path ready to flip when the user decides to deposit capital. Paper mode behavior and strategy are unchanged.

## Scope

**In scope (Approach 2: Minimal SDK swap + pUSD scaffolding):**
- Swap `py-clob-client` → `py-clob-client-v2==1.0.0`
- Update `LiveClobClient` factory for V2 constructor (`chain` instead of `chain_id`, options-object style)
- Update `OrderArgs` construction (drop `fee_rate_bps`, `nonce`, `taker` — V2 protocol-managed)
- Point default host at `clob-v2.polymarket.com`
- Add `polybot/oms/collateral.py` with pUSD wrapping helper — dormant in paper, gated by `DRY_RUN=false`
- Add live-mode startup check for pUSD/USDC balance
- Delete V1 SDK code paths (clean cut; no dual-support)
- Update tests that mock V1 SDK

**Out of scope:**
- Actually depositing USDC / calling `wrap()` against mainnet — deferred to when user flips live
- Testnet dry-run of pUSD wrapping
- Strategy changes
- Web UI changes
- Builder program integration (we do not participate)

## Architecture

V2 migration is **contained inside `polybot/oms/`**. The rest of the codebase (bot orchestrator, strategy, data feeds, web UI) does not know which SDK is in use.

```
polybot/oms/
├── clob_client.py     [MODIFIED]  Factory + LiveClobClient — SDK swap
├── order_executor.py  [MODIFIED]  OrderArgs struct update
├── collateral.py      [NEW]        pUSD wrapping (dormant in paper)
├── heartbeat.py       [UNCHANGED]
polybot/config.py      [MODIFIED]  pUSD addresses, default host, chain field
requirements.txt       [MODIFIED]  Package swap, add web3
.env                   [USER-EDIT] POLYMARKET_HOST, CHAIN (renamed from CHAIN_ID)
```

**Key invariant:** `PaperClobClient` is unchanged. Paper mode never imports V2 SDK or web3. The migration cannot break the currently-running paper bot.

**V2-specific code is isolated in two files:** `LiveClobClient` factory and `OrderArgs` construction in `order_executor.py`. All other call sites use the client through methods whose signatures are preserved in `py-clob-client-v2`.

## Components & Contracts

### `polybot/oms/clob_client.py` (modified)

`create_clob_client(cfg, book_manager)` — signature unchanged. Live branch:

```python
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds

client = ClobClient(
    host=cfg.polymarket_host,   # defaults to https://clob-v2.polymarket.com
    key=cfg.private_key,
    chain=cfg.chain,            # renamed from chain_id
    creds=ApiCreds(
        api_key=cfg.api_key,
        api_secret=cfg.api_secret,
        api_passphrase=cfg.api_passphrase,
    ),
)
```

`PaperClobClient` — **unchanged**. No SDK imports.

### `polybot/oms/order_executor.py` (modified)

- Import swap: `from py_clob_client_v2.clob_types import OrderArgs`
- Fallback dataclass (used when SDK not installed, e.g. in unit tests): drop `fee_rate_bps` and `nonce`; keep `expiration` (still a GTD field in V2).
- Two call sites construct `OrderArgs` — both get the `fee_rate_bps=` and `nonce=` kwargs deleted. Current code does not pass `taker=`, so no action needed there.

### `polybot/oms/collateral.py` (new, ~80 LOC)

```python
class PUsdWrapper:
    def __init__(self, w3, private_key, onramp_address, usdc_address, pusd_address): ...
    def usdc_balance(self) -> Decimal: ...
    def pusd_balance(self) -> Decimal: ...
    def wrap(self, amount: Decimal) -> str:  # returns tx hash
        # 1. Check USDC allowance on onramp; approve if insufficient
        # 2. Call Onramp.wrap(amount)
        # 3. Wait for receipt; raise if reverted
```

- Constructed lazily in `create_clob_client` only when `not cfg.dry_run`.
- `web3` is imported only inside `collateral.py`. Other modules never touch it.
- Gas params use web3 default estimates; no custom priority-fee logic.

### `polybot/config.py` (modified)

New/changed fields:
- `polymarket_host: str = "https://clob-v2.polymarket.com"` (was `https://clob.polymarket.com`)
- `chain: int = 137` (renamed from `chain_id`)
- `pusd_address: str` (hardcoded Polygon mainnet pUSD contract address from Polymarket V2 Contracts reference)
- `usdc_address: str` (hardcoded USDC.e contract address on Polygon)
- `collateral_onramp_address: str` (hardcoded Onramp contract address)
- `wrap_on_startup: bool = False` — user must opt in to auto-wrap at startup

`.env` migration: user updates `POLYMARKET_HOST`, renames `CHAIN_ID` → `CHAIN`. A startup check emits a loud warning (not error) if `CHAIN_ID` is seen in env — helps catch the rename.

### `requirements.txt` (modified)

- Remove: `py-clob-client>=0.34.0`
- Add: `py-clob-client-v2==1.0.0`
- Add: `web3>=6.0.0`

## Data Flow

**Paper mode (`DRY_RUN=true`):**

```
strategy → OrderExecutor → PaperClobClient → in-memory fills vs real book data
```

Unchanged. No V2 SDK, no web3 imports.

**Live mode (`DRY_RUN=false`):**

```
startup:
  create_clob_client(cfg):
    ClobClient(host=clob-v2..., chain=137, ...)    # V2 SDK
    PUsdWrapper(w3, ...)                            # web3 client
    startup_balance_check():
      usdc = wrapper.usdc_balance()
      pusd = wrapper.pusd_balance()
      if pusd == 0 and usdc == 0:
        raise LiveStartupError("No collateral. Deposit USDC and restart.")
      if pusd == 0 and usdc > 0 and cfg.wrap_on_startup:
        wrapper.wrap(usdc)  # converts all USDC to pUSD
      # else: trade with existing pUSD balance

trading loop:
  OrderExecutor.place_limit_buy():
    OrderArgs(token_id, price, size, side, expiration=gtd)   # no nonce/fee/taker
    → client.create_order(args) → client.post_order(signed, orderType="GTC")
```

## Error Handling

**Import-time:**
- `order_executor.py` keeps existing try/except fallback-dataclass pattern. Fallback updated to V2 field set.
- `clob_client.py` live-branch imports SDK inside the `if not dry_run` block so paper mode never exercises the import path.

**Startup (live mode only):**
- `py-clob-client-v2` missing → `LiveStartupError` with install hint.
- `web3` missing → `LiveStartupError` with install hint.
- pUSD balance = 0 and USDC balance = 0 → `LiveStartupError("No collateral")`.
- Both balances zero after wrap → `LiveStartupError` (wrap silently failed).

**Runtime:**
- V2 SDK exceptions bubble through existing `OrderExecutor` try/except. If V2 raises new exception types, the outer `except Exception` in order_executor catches them.

**Rollback:**
- V1 is deleted. After April 28 there is no V1 to roll back to.
- Pre-April 28: `git revert` the migration commit and reinstall `py-clob-client` from `requirements.txt`. Documented in the commit message.

## Testing

**Tests modified (mocks of V1 SDK):**
- `tests/test_credential_validation.py`
- `tests/test_credential_autovalidation.py`
- `tests/test_live_startup_guards.py`
- `tests/test_live_degraded_startup.py`
- `tests/test_live_mode_hardening.py`
- `tests/test_clob_client.py`
- `tests/test_order_executor_new.py`
- `tests/test_gtd_expiration.py`
- `tests/test_reconciliation.py`
- `tests/test_stale_order_safety.py`

Most of these exercise the fallback dataclass and need only field-name updates.

**New tests:**
- `tests/test_v2_migration_surface.py`:
  - `OrderArgs` construction no longer passes `nonce` / `fee_rate_bps`
  - `cfg.polymarket_host` defaults to `clob-v2.polymarket.com`
  - `cfg.chain` exists (not `chain_id`)
  - Live factory constructs without error when SDK is mocked
- `tests/test_collateral_wrapper.py` (mocked web3):
  - `usdc_balance`, `pusd_balance` read ERC20 `balanceOf` correctly
  - `wrap(amount)` calls `approve` then `wrap` in sequence
  - Gas-estimation failure raises a clean exception
- `tests/test_live_startup_pusd_gate.py`:
  - No pUSD + no USDC → startup error
  - No pUSD + has USDC + `wrap_on_startup=false` → warning, no wrap called
  - No pUSD + has USDC + `wrap_on_startup=true` → `wrap()` invoked once

**Validation gates before shipping:**
1. `python -m pytest tests/ -q` — baseline 1105 pass; after change: ≥1105 + new tests, zero regressions.
2. Smoke: `python run_bot.py` with `DRY_RUN=true` — bot starts, heartbeat healthy, paper orders place cleanly, web UI serves.
3. Dry-run live construct: `DRY_RUN=false` with a test-wallet `PRIVATE_KEY` and zero on-chain balance — bot should fail fast with `LiveStartupError("No collateral")`. Confirms gate works; no real orders placed.

## Shipping order

1. Branch: `feat/v2-migration`
2. Commit 1: requirements.txt + config.py changes; `.env` guidance
3. Commit 2: `clob_client.py` V2 swap + `order_executor.py` OrderArgs update; update mocks in tests
4. Commit 3: `collateral.py` + `test_collateral_wrapper.py`
5. Commit 4: startup pUSD balance gate + `test_live_startup_pusd_gate.py`
6. Commit 5: `test_v2_migration_surface.py` + any remaining test fixes
7. PR review, merge to main
8. Live bot restart on V2 endpoint; paper continues
9. April 28 cutover: no action required — already on V2

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| `py-clob-client-v2` has subtle API differences from docs | Run smoke test in paper after SDK swap before merging |
| V2 endpoint has stricter rate limits | Monitor 429s in polybot.log for first 24h after cutover |
| Order struct field names differ from docs (`timestamp` naming) | Confirm against installed SDK's `OrderArgs` class; update fallback dataclass to match exactly |
| Polygon contract addresses for pUSD/USDC/Onramp get out of date | Hardcode in `config.py` with a comment citing the Polymarket V2 Contracts reference URL; user can override via `.env` if needed |
| Web3 version conflict with existing deps | Pin `web3>=6.0.0,<7.0.0`; run `pip check` after install |

## Non-Goals (explicit)

- Not rewriting the strategy, ladder manager, or fair value logic
- Not refactoring the paper client
- Not adding V2-specific features (builder program, new order types)
- Not running real pUSD wrap on mainnet — that is a live-flip decision, separate from this migration
- Not maintaining V1 and V2 in parallel

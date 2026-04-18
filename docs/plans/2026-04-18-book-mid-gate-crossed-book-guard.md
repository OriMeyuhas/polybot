# Plan — Reject Negative Spreads in Book-Mid Gate (Crossed-Book Guard)

**Cycle**: 20
**Type**: Correctness fix (defensive guard)
**Author**: manager-opus-cycle20-investigation
**Date**: 2026-04-18

## 1. Problem Statement

The cycle 19 instrumentation has captured 2/2 `BOOK MID GATE SKIP` events with
**negative spreads** on BOTH sides of a newly-opened market:

```
02:41:15 ... reason=certainty_too_low spread_up=-0.4200 spread_dn=-0.4200 cert=0.4300
02:44:30 ... reason=certainty_too_low spread_up=-0.1200 spread_dn=-0.1200 cert=0.1300
```

Spreads are identical on UP and DN because Polymarket binary tokens are
complementary (`ask_UP ≈ 1 - bid_DN`, `bid_UP ≈ 1 - ask_DN`), so a stale book
on one side mirrors into the other's spread computation.

These skips happened to exit via the `certainty_too_low` branch. However, the
current code path (`polybot/strategy/ladder_manager.py:1020-1023`) admits
*any* negative spread because `_spread_up <= _max_spread` evaluates `True` for
all negatives when `_max_spread=0.05`:

```python
if (
    _has_all_data
    and _spread_up is not None and _spread_up <= _max_spread
    and _spread_dn is not None and _spread_dn <= _max_spread
):
    _book_mid_up = _up_mid / (_up_mid + _dn_mid)
    _cert_book = 2.0 * abs(_book_mid_up - 0.5)
    if _cert_book >= self.cfg.book_mid_gate_certainty_threshold:
        # ... FIRES directional post on garbage book
```

A different stale book — one where the computed `_book_mid_up` happens to be
far from 0.5 — could trigger the gate to post a one-sided directional budget
based on inverted-book data. This is latent, not active, but it's a
correctness defect worth closing before lowering the threshold further.

## 2. Root Cause

The books serving the gate come from `BookManager.get_book()` which returns a
mutable `OrderBook` whose `bids`/`asks` are updated in-place by a WS listener
thread. At window-open the bot has just discovered a new market and the book
may still be coming up:

1. Initial HTTP seed may deliver a thin book (1-2 levels).
2. The first WS snapshot arrives slightly later and replaces it.
3. If the snapshot delivered by the CLOB has levels from two WS updates
   merged (common on thin new markets), the book can briefly be crossed.
4. The 4 sequential `get_best_bid/ask` calls + 2 HTTP midpoint calls in
   `ladder_manager` all take separate reads — none of them sees an atomic
   snapshot.

This is a known-category phenomenon for any market maker reading WS books
without a snapshot lock. The fix is not to eliminate the race (expensive and
unnecessary for a defensive gate) but to **detect the race and bail**.

## 3. Scope

**Single file, ~5 lines**: `polybot/strategy/ladder_manager.py`, book-mid
gate block at lines 1020-1029.

**New behavior**: treat negative spread as a non-fire with reason
`crossed_book`, adding a 4th instrumentation category. Existing
`spread_too_wide` (positive but > max_spread) stays unchanged.

Unit tests: extend `tests/test_book_mid_gate.py::TestBookMidGateInstrumentation`
with one new test that exercises the crossed-book branch.

**Out of scope**:
- Fixing the underlying book race (would require atomic-snapshot API on
  `BookManager`, large surface change).
- Changing `_max_spread` or the certainty threshold.
- Touching the cycle 19 instrumentation itself — ordering orders: add the
  `crossed_book` branch *before* `spread_too_wide` so a crossed book is
  never misclassified as wide.

## 4. Acceptance Criteria

1. `polybot.log` gains a new DEBUG category
   `reason=crossed_book` whenever `_spread_up < 0 OR _spread_dn < 0`
   (and `_has_all_data` is True).
2. Gate never fires when either spread is negative (acceptance via unit
   test: construct a crossed book with certainty > threshold and assert
   no directional budget assignment).
3. Existing tests in `TestBookMidGate` and `TestBookMidGateInstrumentation`
   still pass unchanged.
4. New test `test_non_fire_crossed_book_logged` asserts the debug line
   format matches the existing convention.
5. Full suite passes: `pytest tests/ -q` returns 1008/1008 (was 1007 after
   cycle 19).
6. No new dependencies, no config changes, no performance regression
   (single extra comparison).

## 5. Holdout Evidence

**Exemption requested**: this is a defensive correctness guard, not a
strategy change. The live backtester/Dome corpus does not contain
crossed-book events (Dome data is validated before ingestion, see
`feedback_fv_gate_hardcoded_0_80.md` note that Dome's FV is book-mid at
entry). Unit tests cover the branch; live paper tests confirm the gate's
firing decision is unchanged on non-crossed books.

**Pre-flight check before merge**: after fix deploys, tail `polybot.log`
for 1 hour and confirm:
- `reason=crossed_book` emerges on at least 1 market-open (cycle 19 data
  shows 2 crossed skips in ~60 minutes).
- `reason=certainty_too_low` count does NOT decrease by more than the
  count of `crossed_book` events (no false positives stealing from the
  real certainty-low bucket).

If both hold, the fix is behaving as intended and skip bucketing can
resume (cycle 21 target).

## 6. Implementation Steps

1. Add helper `_is_crossed = _spread_up < 0 or _spread_dn < 0` before
   the `_max_spread` comparison.
2. Change the outer `if` to also require `not _is_crossed`.
3. Add a new `elif _has_all_data and _is_crossed:` branch emitting
   `reason=crossed_book spread_up=... spread_dn=... cert=None` at DEBUG.
4. Add test: construct `BookManager` with crossed book on both tokens,
   call `run_pair` (or extract the gate block into a helper first if
   needed), assert `book_mid_gate_fired is False` and log contains
   `reason=crossed_book`.
5. Run full test suite. All 1008 pass.
6. Single-commit: `fix(strategy): reject crossed books in book-mid gate (cycle 20)`.

## 7. Rollback Plan

Single commit. Revert via `git revert <sha>` if instrumentation shows
unexpected behavior. No config state to unwind. Bot restart required.

## 8. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------:|-------:|------------|
| Crossed books never occur again and new branch is dead code | Medium | Low | Branch is cheap, provides forward protection if threshold lowers. |
| Extra branch misclassifies some certainty_too_low as crossed_book | Low | Low | Order branches so crossed is checked first; test covers both. |
| Gate now fires LESS often, reducing edge | Low | Low | Gate currently never fires on crossed books anyway (certainty was too low by chance) — we formalize what's already happening. |
| Regression in unit tests | Low | Medium | Full suite run before merge. |

## 9. Queued Follow-ups

- **Cycle 21**: resume original cycle 20 plan — wait for ≥50 total skips,
  bucket reason distribution, pick follow-up (live-spread corpus vs
  threshold re-tune vs feed investigation).
- **Cycle 22+ (if crossed_book persists > 5% of window-opens)**: audit
  `BookManager.get_book()` + `apply_book_snapshot` for a cleaner warmup
  sequence (delay gate check until `book._last_update > window_open_ts`
  or snapshot age > 500ms).

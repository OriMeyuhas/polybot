# Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the PolyBot dashboard with a futuristic Glass + Purple Gradient theme, live Polymarket book prices, start/stop controls, accurate capital tracking, and proper settlement display.

**Architecture:** Backend-first approach — add new data fields and API endpoints, then rewrite the frontend to consume them. Each backend task adds tests first (TDD). Frontend is a full rewrite of 3 static files.

**Note on line numbers:** Line numbers reference the original unmodified files. After earlier tasks modify a file, line numbers shift. Use method/function names as anchors when editing files already touched by prior tasks.

**Tech Stack:** Python/FastAPI (backend), vanilla JS/CSS (frontend), Google Fonts (Space Grotesk + Fira Code), pytest (testing)

**Spec:** `docs/superpowers/specs/2026-03-19-dashboard-redesign-design.md`

---

### Task 1: Add `start_paused` config and env var wiring

**Files:**
- Modify: `polybot/config.py:52-122` (BotConfig dataclass)
- Modify: `polybot/config.py:146-186` (load_bot_config function)

- [ ] **Step 1: Add `start_paused` field to BotConfig**

In `polybot/config.py`, add after line 122 (`web_port: int = 8080`):

```python
    start_paused: bool = True
```

- [ ] **Step 2: Wire `START_PAUSED` env var in `load_bot_config()`**

In `polybot/config.py`, add after line 185 (`web_port=...`):

```python
        start_paused=os.getenv("START_PAUSED", "true").lower() in ("true", "1", "yes"),
```

- [ ] **Step 3: Run tests to verify nothing breaks**

Run: `pytest tests/ -v --tb=short`
Expected: All 183 tests PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/config.py
git commit -m "feat: add start_paused config field"
```

---

### Task 2: Add `filled_count` and `total_count` to OrderTracker

**Files:**
- Modify: `polybot/order_tracker.py:88-102` (after filled_cost method)
- Create tests in: `tests/test_order_tracker.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_order_tracker.py`:

```python
class TestFilledCount:
    def test_filled_count_only_fully_filled(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0))
        tracker.add(TrackedOrder(order_id="o2", market_id="m1", token_id="t", side=Side.UP, price=0.46, size=10.0))
        tracker.update_fill("o1", 10.0)  # fully filled
        tracker.update_fill("o2", 3.0)   # partial
        assert tracker.filled_count("m1", Side.UP) == 1  # only o1

    def test_filled_count_excludes_other_side(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0))
        tracker.add(TrackedOrder(order_id="o2", market_id="m1", token_id="t", side=Side.DOWN, price=0.48, size=10.0))
        tracker.update_fill("o1", 10.0)
        tracker.update_fill("o2", 10.0)
        assert tracker.filled_count("m1", Side.UP) == 1
        assert tracker.filled_count("m1", Side.DOWN) == 1


class TestTotalCount:
    def test_total_count_excludes_cancelled(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0))
        tracker.add(TrackedOrder(order_id="o2", market_id="m1", token_id="t", side=Side.UP, price=0.46, size=10.0))
        tracker.add(TrackedOrder(order_id="o3", market_id="m1", token_id="t", side=Side.UP, price=0.47, size=10.0))
        tracker.cancel("o3")
        assert tracker.total_count("m1", Side.UP) == 2  # o1 resting, o2 resting, o3 cancelled

    def test_total_count_includes_filled_and_resting(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0))
        tracker.add(TrackedOrder(order_id="o2", market_id="m1", token_id="t", side=Side.UP, price=0.46, size=10.0))
        tracker.update_fill("o1", 10.0)  # filled
        assert tracker.total_count("m1", Side.UP) == 2  # both count
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_order_tracker.py::TestFilledCount -v`
Expected: FAIL — `AttributeError: 'OrderTracker' object has no attribute 'filled_count'`

- [ ] **Step 3: Implement `filled_count` and `total_count`**

In `polybot/order_tracker.py`, add after `filled_cost` method (after line 102):

```python
    def filled_count(self, market_id: str, side: Side) -> int:
        """Count of fully filled orders for a side."""
        count = 0
        for oid in self._by_market.get(market_id, []):
            o = self.orders.get(oid)
            if o and o.side == side and o.status == "filled":
                count += 1
        return count

    def total_count(self, market_id: str, side: Side) -> int:
        """Count of active orders (resting + partial + filled, excl. cancelled)."""
        count = 0
        for oid in self._by_market.get(market_id, []):
            o = self.orders.get(oid)
            if o and o.side == side and o.status in ("resting", "partial", "filled"):
                count += 1
        return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_order_tracker.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/order_tracker.py tests/test_order_tracker.py
git commit -m "feat: add filled_count and total_count to OrderTracker"
```

---

### Task 3: Extend LadderState with token IDs and ask price caching

**Files:**
- Modify: `polybot/ladder_manager.py:24-34` (LadderState dataclass)
- Modify: `polybot/ladder_manager.py:206-212` (post_ladder — store token IDs and ask prices)
- Modify: `polybot/ladder_manager.py:267-268,318,345` (reprice_if_needed — update cached asks)
- Modify: `polybot/ladder_manager.py:419-443` (get_ladder_stats — expose new fields)

- [ ] **Step 1: Extend LadderState dataclass**

In `polybot/ladder_manager.py`, add new fields to `LadderState` (after line 34):

```python
    up_token_id: str = ""
    dn_token_id: str = ""
    current_ask_up: float = 0.0
    current_ask_dn: float = 0.0
```

- [ ] **Step 2: Store token IDs and initial asks in `post_ladder()`**

Replace lines 206-212 in `polybot/ladder_manager.py`:

```python
            self.ladders[market.market_id] = LadderState(
                market_id=market.market_id,
                asset=market.asset,
                anchor_up=anchor_up,
                anchor_dn=anchor_dn,
                posted_at=now,
                up_token_id=market.up_token_id,
                dn_token_id=market.dn_token_id,
                current_ask_up=best_ask_up,
                current_ask_dn=best_ask_dn,
            )
```

- [ ] **Step 3: Update cached asks in `reprice_if_needed()`**

After line 268 (`best_ask_dn = ...`), add:

```python
            # Cache latest ask prices for dashboard
            state.current_ask_up = best_ask_up
            state.current_ask_dn = best_ask_dn
```

- [ ] **Step 4: Extend `get_ladder_stats()` with new fields**

Replace the return dict in `get_ladder_stats()` (lines 434-443):

```python
        state = self.ladders.get(market_id)

        return {
            "up_resting": up_resting,
            "dn_resting": dn_resting,
            "up_filled": up_filled,
            "dn_filled": dn_filled,
            "up_vwap": up_vwap,
            "dn_vwap": dn_vwap,
            "pair_cost": up_vwap + dn_vwap,
            "imbalance": imbalance,
            "ask_up": state.current_ask_up if state else 0.0,
            "ask_dn": state.current_ask_dn if state else 0.0,
            "up_filled_count": self.tracker.filled_count(market_id, Side.UP),
            "dn_filled_count": self.tracker.filled_count(market_id, Side.DOWN),
            "up_total_rungs": self.tracker.total_count(market_id, Side.UP),
            "dn_total_rungs": self.tracker.total_count(market_id, Side.DOWN),
        }
```

Note: the key was renamed from `combined_vwap` to `pair_cost`.

- [ ] **Step 5: Fix the server.py mapping**

In `polybot/web/server.py` line 54, change:

```python
            "pair_cost": round(stats["combined_vwap"], 4),
```

to:

```python
            "pair_cost": round(stats["pair_cost"], 4),
```

And add the new fields to the ladders dict (after the `"imbalance"` line):

```python
            "ask_up": round(stats["ask_up"], 4),
            "ask_dn": round(stats["ask_dn"], 4),
            "up_filled_count": stats["up_filled_count"],
            "dn_filled_count": stats["dn_filled_count"],
            "up_total_rungs": stats["up_total_rungs"],
            "dn_total_rungs": stats["dn_total_rungs"],
```

- [ ] **Step 6: Fix existing ladder snapshot test mock**

In `tests/test_web_server.py`, update the `test_snapshot_ladders_with_time_left` test's mock return value to include all new keys:

```python
    bot.ladder_manager.get_ladder_stats.return_value = {
        "up_resting": 5, "dn_resting": 6,
        "up_filled": 20.0, "dn_filled": 18.0,
        "up_vwap": 0.40, "dn_vwap": 0.45,
        "pair_cost": 0.85, "imbalance": 0.10,
        "ask_up": 0.43, "ask_dn": 0.48,
        "up_filled_count": 3, "dn_filled_count": 2,
        "up_total_rungs": 8, "dn_total_rungs": 8,
    }
```

- [ ] **Step 7: Run all tests**

Run: `pytest tests/ -v --tb=short`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add polybot/ladder_manager.py polybot/web/server.py tests/test_web_server.py
git commit -m "feat: cache live ask prices and rung counts in ladder stats"
```

---

### Task 4: Add `cancel_all_ladders()` and `clear_cancelled_ladders()`

**Files:**
- Modify: `polybot/ladder_manager.py` (add methods after `cleanup_ladder`)
- Test: `tests/test_ladder_manager.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ladder_manager.py`:

```python
class TestCancelAllLadders:
    def test_cancel_all_cancels_every_market(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.order_tracker import TrackedOrder
        # Add two ladders with resting orders
        mgr.tracker.add(TrackedOrder(order_id="o1", market_id="m1", token_id="t", side=Side.UP, price=0.45, size=10.0, placed_at=1000.0))
        mgr.tracker.add(TrackedOrder(order_id="o2", market_id="m2", token_id="t", side=Side.DOWN, price=0.48, size=10.0, placed_at=1000.0))
        from polybot.ladder_manager import LadderState
        mgr.ladders["m1"] = LadderState(market_id="m1", asset="BTC", anchor_up=0.45, anchor_dn=0.48, posted_at=1000.0)
        mgr.ladders["m2"] = LadderState(market_id="m2", asset="ETH", anchor_up=0.44, anchor_dn=0.49, posted_at=1000.0)

        cancelled = mgr.cancel_all_ladders()
        assert cancelled == 2
        assert len(mgr.tracker.get_resting("m1")) == 0
        assert len(mgr.tracker.get_resting("m2")) == 0
        # Ladder entries are NOT removed
        assert "m1" in mgr.ladders
        assert "m2" in mgr.ladders


class TestClearCancelledLadders:
    def test_clear_removes_ladders_with_no_resting(self, cfg, market, mock_clob):
        mgr = _make_manager(cfg, mock_clob)
        from polybot.ladder_manager import LadderState
        mgr.ladders["m1"] = LadderState(market_id="m1", asset="BTC", anchor_up=0.45, anchor_dn=0.48, posted_at=1000.0)
        mgr.ladders["m2"] = LadderState(market_id="m2", asset="ETH", anchor_up=0.44, anchor_dn=0.49, posted_at=1000.0)
        # m2 has a resting order
        from polybot.order_tracker import TrackedOrder
        mgr.tracker.add(TrackedOrder(order_id="o1", market_id="m2", token_id="t", side=Side.UP, price=0.44, size=10.0))

        mgr.clear_cancelled_ladders()
        assert "m1" not in mgr.ladders  # no resting -> removed
        assert "m2" in mgr.ladders      # has resting -> kept
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ladder_manager.py::TestCancelAllLadders -v`
Expected: FAIL — `AttributeError: 'LadderManager' object has no attribute 'cancel_all_ladders'`

- [ ] **Step 3: Implement both methods**

In `polybot/ladder_manager.py`, add after `cleanup_ladder` (after line 417):

```python
    def cancel_all_ladders(self) -> int:
        """Cancel all resting orders across all ladders. Returns total cancelled count.

        Ladder entries are NOT removed — they remain visible on the dashboard.
        """
        total = 0
        for mid in list(self.ladders.keys()):
            total += self.cancel_ladder(mid)
        return total

    def clear_cancelled_ladders(self) -> None:
        """Remove ladder entries that have no resting orders (all filled/cancelled).

        Called when the bot resumes from paused state so fresh ladders can be posted.
        """
        to_remove = [
            mid for mid in self.ladders
            if not self.tracker.has_orders(mid)
        ]
        for mid in to_remove:
            self.cleanup_ladder(mid)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ladder_manager.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/ladder_manager.py tests/test_ladder_manager.py
git commit -m "feat: add cancel_all_ladders and clear_cancelled_ladders"
```

---

### Task 5: Bot controls — start_paused, pending_cancel_all, connection_lost fix

**Files:**
- Modify: `polybot/bot.py:58` (_cancel_only_mode init)
- Modify: `polybot/bot.py:170-175` (_on_connection_lost)
- Modify: `polybot/bot.py:219-242` (trading loop — add pending_cancel_all check and resume transition)
- Modify: `tests/test_bot_integration.py` (update connection_lost test)

- [ ] **Step 1: Change `_cancel_only_mode` init to use config**

In `polybot/bot.py` line 58, change:

```python
        self._cancel_only_mode = False
```

to:

```python
        self._cancel_only_mode = cfg.start_paused
        self._prev_cancel_only = cfg.start_paused
        self._pending_cancel_all = False
```

- [ ] **Step 2: Fix `_on_connection_lost()` to preserve user's stop intent**

In `polybot/bot.py` line 175, remove the line:

```python
        self._cancel_only_mode = False
```

- [ ] **Step 3: Add pending_cancel_all handling and resume transition to trading loop**

At the top of `run_trading_loop()`, after the heartbeat gate (after line 225), add:

```python
            # Handle pending cancel-all from /api/stop
            if self._pending_cancel_all:
                self.ladder_manager.cancel_all_ladders()
                self._pending_cancel_all = False

            # Detect resume transition (stopped -> running)
            if self._prev_cancel_only and not self._cancel_only_mode:
                self.ladder_manager.clear_cancelled_ladders()
            self._prev_cancel_only = self._cancel_only_mode
```

- [ ] **Step 4: Update the connection_lost test**

In `tests/test_bot_integration.py`, change the test at line 131-144 to not assert `_cancel_only_mode` becomes False:

```python
class TestConnectionLost:
    def test_on_connection_lost_resets_state(self, cfg, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot._cancel_only_mode = True
        bot.ladder_manager.ladders["m1"] = MagicMock()
        bot.ladder_manager.cleanup_ladder = MagicMock()
        bot.order_tracker.mark_all_unknown = MagicMock()

        bot._on_connection_lost()

        bot.ladder_manager.cleanup_ladder.assert_called_once_with("m1")
        bot.order_tracker.mark_all_unknown.assert_called_once()
        # _cancel_only_mode is preserved (user's stop intent not overridden)
        assert bot._cancel_only_mode is True
```

- [ ] **Step 5: Ensure existing tests pass with `start_paused=False` in fixtures**

In `tests/test_bot_integration.py`, update the `cfg` fixture (line 9-20) to add `start_paused=False`:

```python
@pytest.fixture
def cfg():
    return BotConfig(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        poll_interval_ms=100,
        ladder_rungs=4,
        ladder_spacing=0.02,
        ladder_width=0.06,
        ladder_size_skew=1.5,
        start_paused=False,
    )
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/ -v --tb=short`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add polybot/bot.py tests/test_bot_integration.py
git commit -m "feat: bot starts paused, pending_cancel_all flag, preserve stop on connection loss"
```

---

### Task 6: Settlement activity detail with winning/losing breakdown

**Files:**
- Modify: `polybot/bot.py` (_settle_position method, lines 355-377)
- Test: `tests/test_bot_integration.py`

- [ ] **Step 1: Write failing tests for settlement detail format**

Append to `tests/test_bot_integration.py`:

```python
class TestSettlementDetail:
    def test_settle_two_sided_detail(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.redeemer = MagicMock()
        bot.position_manager.update_position(market.market_id, Side.UP, qty=100.0, cost=43.0)
        bot.position_manager.update_position(market.market_id, Side.DOWN, qty=100.0, cost=48.0)
        bot._expired_market_cache[market.market_id] = market
        bot.position_manager.mark_pending_settlement(market.market_id)

        bot._settle_position(market.market_id, market, "UP")

        assert len(bot._activity_log) == 1
        detail = bot._activity_log[0].detail
        assert "UP won" in detail
        assert "\u2191" in detail  # up arrow
        assert "\u2193" in detail  # down arrow (losing side)
        assert "net" in detail

    def test_settle_one_sided_detail(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.redeemer = MagicMock()
        bot.position_manager.update_position(market.market_id, Side.UP, qty=100.0, cost=43.0)
        # No DOWN side
        bot._expired_market_cache[market.market_id] = market
        bot.position_manager.mark_pending_settlement(market.market_id)

        bot._settle_position(market.market_id, market, "UP")

        detail = bot._activity_log[0].detail
        assert "UP won" in detail
        assert "\u2193" not in detail  # no down arrow when no losing side
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bot_integration.py::TestSettlementDetail -v`
Expected: FAIL — detail format doesn't match

- [ ] **Step 3: Update `_settle_position()` to show both sides**

Replace the entire `_settle_position` method in `polybot/bot.py`:

```python
    def _settle_position(self, mid: str, market: MarketWindow, outcome: str):
        """Settle a single position: compute PnL, update risk/bankroll, queue redemption."""
        pos = self.position_manager.positions.get(mid)
        if pos:
            if outcome in ("UP", "YES"):
                pnl = pos.profit_if_up()
                winning = pos.up_qty - pos.up_cost if pos.up_qty > 0 else 0.0
                losing = pos.dn_cost
            else:
                pnl = pos.profit_if_down()
                winning = pos.dn_qty - pos.dn_cost if pos.dn_qty > 0 else 0.0
                losing = pos.up_cost

            logger.info("Settled %s: %s, PnL=$%.2f", mid, outcome, pnl)
            self.risk_manager.update_pnl(pnl)
            self.position_manager.bankroll += pnl

            if losing > 0:
                detail = f"{outcome} won \u2192 \u2191 +${winning:.2f} \u2193 -${losing:.2f} = net ${pnl:+.2f}"
            else:
                detail = f"{outcome} won \u2192 \u2191 +${winning:.2f} = net ${pnl:+.2f}"
            self._record_activity("SETTLE", market.asset, detail, pnl=pnl)

            self.redeemer.queue_redemption(
                market.condition_id,
                [market.up_token_id, market.dn_token_id],
            )

        self.position_manager.complete_settlement(mid)
        self.position_manager.remove_position(mid)
        self._expired_market_cache.pop(mid, None)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/bot.py tests/test_bot_integration.py
git commit -m "feat: settlement activity shows winning/losing side breakdown"
```

---

### Task 7: Update state snapshot — wallet split, trade_count, positions timeframe

**Files:**
- Modify: `polybot/web/server.py:24-105` (build_state_snapshot)
- Modify: `tests/test_web_server.py`

- [ ] **Step 1: Write tests for new snapshot fields**

Add to `tests/test_web_server.py`:

```python
def test_snapshot_wallet_split():
    bot = _make_bot()
    bot.position_manager.update_position("m1", Side.UP, 50.0, 20.0)
    bot.ladder_manager.total_committed.return_value = 30.0  # resting + positions
    snap = build_state_snapshot(bot)
    w = snap["wallet"]
    assert "on_orders" in w
    assert "in_positions" in w
    assert w["in_positions"] == 20.0  # up_cost only
    assert w["on_orders"] == 10.0    # total_committed - in_positions
    assert "deployed" not in w


def test_snapshot_trade_count():
    bot = _make_bot()
    bot._trade_count = 42
    snap = build_state_snapshot(bot)
    assert snap["trade_count"] == 42


def test_snapshot_position_has_timeframe():
    bot = _make_bot()
    bot.position_manager.update_position("m1", Side.UP, 50.0, 20.0)
    now = int(time.time())
    bot.active_markets = [
        MarketWindow("m1", "0xcond", "BTC", 900, "up", "dn", now - 300, now + 600)
    ]
    snap = build_state_snapshot(bot)
    assert snap["positions"][0]["timeframe_sec"] == 900
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_server.py::test_snapshot_wallet_split -v`
Expected: FAIL

- [ ] **Step 3: Update `build_state_snapshot()`**

Update the wallet section (lines 77-91):

```python
    deployed = bot.ladder_manager.total_committed()
    in_positions = bot.position_manager.total_position_cost()
    on_orders = max(0.0, deployed - in_positions)
    balance = getattr(bot, "_wallet_balance", None)
    if balance is None:
        balance = bot.position_manager.bankroll if cfg.dry_run else 0.0

    address = "DRY RUN"
    if not cfg.dry_run and cfg.private_key:
        try:
            from eth_account import Account
            address = Account.from_key(cfg.private_key).address
            address = address[:6] + "..." + address[-4:]
        except Exception:
            address = "unknown"

    wallet = {
        "address": address,
        "usdc_balance": round(balance, 2),
        "on_orders": round(on_orders, 2),
        "in_positions": round(in_positions, 2),
        "available": round(balance - deployed, 2),
    }
```

Add `trade_count` to the return dict (after `"risk_halted"`):

```python
        "trade_count": bot._trade_count,
```

Add `timeframe_sec` to positions entries (after `"asset": asset,`):

```python
            "timeframe_sec": market.timeframe_sec if market else 0,
```

- [ ] **Step 4: Update existing test for wallet field change**

In `tests/test_web_server.py`, `test_api_balance_endpoint` (line 141): change `assert "deployed" in data` to `assert "on_orders" in data`.

- [ ] **Step 5: Run all tests**

Run: `pytest tests/ -v --tb=short`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add polybot/web/server.py tests/test_web_server.py
git commit -m "feat: wallet split, trade_count, position timeframe in state snapshot"
```

---

### Task 8: Add REST control endpoints (start, stop, set-bankroll)

**Files:**
- Modify: `polybot/web/server.py:108-162` (create_app function)
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_web_server.py`:

```python
@pytest.mark.asyncio
async def test_api_start_endpoint():
    bot = _make_bot()
    bot._cancel_only_mode = True
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/start")
        assert resp.status_code == 200
        assert bot._cancel_only_mode is False


@pytest.mark.asyncio
async def test_api_stop_endpoint():
    bot = _make_bot()
    bot._cancel_only_mode = False
    bot._pending_cancel_all = False
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stop")
        assert resp.status_code == 200
        assert bot._cancel_only_mode is True
        assert bot._pending_cancel_all is True


@pytest.mark.asyncio
async def test_api_set_bankroll_dry_run():
    bot = _make_bot()
    bot.cfg = BotConfig(dry_run=True)
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/set-bankroll", json={"bankroll": 5000.0})
        assert resp.status_code == 200
        assert bot.position_manager.bankroll == 5000.0
        assert bot.risk_manager.starting_bankroll == 5000.0


@pytest.mark.asyncio
async def test_api_set_bankroll_live_rejected():
    bot = _make_bot()
    bot.cfg = BotConfig(dry_run=False, private_key="0xfake")
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/set-bankroll", json={"bankroll": 5000.0})
        assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_server.py::test_api_start_endpoint -v`
Expected: FAIL — 404

- [ ] **Step 3: Add endpoints to `create_app()`**

In `polybot/web/server.py`, add after the `/api/balance` endpoint (after line 123), before `app.mount`:

```python
    @app.post("/api/start")
    async def api_start():
        bot._cancel_only_mode = False
        return JSONResponse({"status": "running"})

    @app.post("/api/stop")
    async def api_stop():
        bot._cancel_only_mode = True
        bot._pending_cancel_all = True
        return JSONResponse({"status": "stopped"})

    @app.post("/api/set-bankroll")
    async def api_set_bankroll(request):
        if not bot.cfg.dry_run:
            return JSONResponse({"error": "Cannot set bankroll in live mode"}, status_code=403)
        body = await request.json()
        bankroll = float(body.get("bankroll", 0))
        if bankroll <= 0:
            return JSONResponse({"error": "Bankroll must be positive"}, status_code=400)
        bot.position_manager.bankroll = bankroll
        bot.risk_manager.starting_bankroll = bankroll
        return JSONResponse({"status": "ok", "bankroll": bankroll})
```

Add the `Request` import at the top of the file:

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
```

- [ ] **Step 4: Run all tests**

Run: `pytest tests/ -v --tb=short`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/web/server.py tests/test_web_server.py
git commit -m "feat: add start/stop/set-bankroll REST endpoints"
```

---

### Task 9: Frontend rewrite — HTML structure

**Files:**
- Rewrite: `polybot/web/static/index.html`

- [ ] **Step 1: Write new `index.html`**

Full rewrite with the Glass + Purple Gradient layout. Key sections:
- Disconnect banner
- Top bar: logo, mode badge, uptime, heartbeat dot, start/stop buttons
- KPI row: 5 cards (bankroll, daily PnL, deployed, available, trades)
- Spot prices row: flexible grid
- Main grid (3:2): Active Ladders table (10 columns incl ASK ↑/↓ and FILL counts) + Positions table + Wallet card
- Activity feed panel
- Google Fonts link for Space Grotesk + Fira Code

All DOM element IDs must match what `dashboard.js` will reference. Use semantic IDs: `mode-badge`, `cancel-badge`, `halted-badge`, `uptime`, `bankroll`, `pnl`, `hb-dot`, `wallet-balance`, `spots`, `ladders-body`, `positions-body`, `wallet-detail`, `activity-feed`, `btn-start`, `btn-stop`, `kpi-deployed`, `kpi-available`, `kpi-trades`, `disconnect-banner`.

- [ ] **Step 2: Commit**

```bash
git add polybot/web/static/index.html
git commit -m "feat: rewrite dashboard HTML with futuristic layout"
```

---

### Task 10: Frontend rewrite — CSS theme

**Files:**
- Rewrite: `polybot/web/static/style.css`

- [ ] **Step 1: Write new `style.css`**

Full rewrite implementing the Glass + Purple Gradient design system from the spec:
- CSS custom properties for all theme colors
- Body: gradient background, Space Grotesk base font
- `.glass` card component
- `.badge` pill variants (dry, live, cancel, halted)
- `.kpi-card` for the 5 KPI cards
- `.spot-card` for price cards
- Grid layouts for KPI row, spots row, main 3:2 grid
- Table styling with Fira Code font for data cells
- `.activity-row` for the activity feed
- `.dot` with pulsing animation for heartbeat
- `.text-green`, `.text-red`, `.text-yellow`, `.text-muted` utilities
- Start/stop button styles
- Disconnect banner (fixed, full-width, red)
- `@keyframes pulse` for heartbeat dot

- [ ] **Step 2: Commit**

```bash
git add polybot/web/static/style.css
git commit -m "feat: rewrite CSS with Glass + Purple Gradient theme"
```

---

### Task 11: Frontend rewrite — JavaScript

**Files:**
- Rewrite: `polybot/web/static/dashboard.js`

- [ ] **Step 1: Write new `dashboard.js`**

Full rewrite with:

**WebSocket connection:**
- Connect to `ws://{host}/ws`
- Auto-reconnect every 3s on disconnect
- Show/hide disconnect banner
- Initial fetch from `GET /api/state`

**Controls:**
- `btn-start` onclick → `POST /api/start`
- `btn-stop` onclick → `POST /api/stop`
- Bankroll click-to-edit (dry run only): click shows input, Enter/blur → `POST /api/set-bankroll`

**Data update function `update(d)`:**
- Top bar: mode badge, uptime, heartbeat dot, running/stopped from `!d.cancel_only_mode`
- KPI row: bankroll, pnl (with %), deployed (with % of bankroll), available, trade count + position count
- Spots: render cards from `d.spots` object
- Ladders table: 10 columns — market (asset + id suffix), TF, ASK ↑, ASK ↓, FILL ↑ (count/total), FILL ↓ (count/total), PAIR$, IMBAL, TIME
- Positions table: market (asset + tfLabel), qty ↑, qty ↓, if ↑, if ↓, worst
- Wallet: address, balance (clickable in dry run), on_orders, in_positions, available
- Activity: reverse chronological, type badges color-coded

**Formatting functions:**
- `fmt(n, d)` — locale number format
- `fmtPct(ratio)` — percentage
- `fmtTime(sec)` — MM:SS
- `fmtUptime(sec)` — Xh Ym
- `tfLabel(sec)` — "5m", "15m", "1h"
- `pnlClass(v)`, `pairClass(v)`, `imbalClass(v)` — CSS class selectors

**Empty states:** "No active ladders", "No open positions", "No activity yet"

- [ ] **Step 2: Commit**

```bash
git add polybot/web/static/dashboard.js
git commit -m "feat: rewrite dashboard JS with controls, new data bindings, futuristic theme"
```

---

### Task 12: Final integration test — run all tests and verify

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS (should be 183 original + ~10 new = ~193 tests)

- [ ] **Step 2: Verify the bot starts and dashboard loads**

Run: `python run_bot.py` (starts in dry run + paused mode)
Open: `http://127.0.0.1:8080`

Verify:
- Dashboard loads with Glass + Purple Gradient theme
- Mode badge shows "DRY RUN"
- Bot is in STOPPED state
- Click "START" → bot begins posting ladders
- Ladders table shows ASK ↑ / ASK ↓ columns with prices
- FILL columns show rung counts (e.g., "3/36")
- Wallet shows On Orders / In Positions / Available split
- Click balance to edit bankroll
- Activity feed shows incremental fills
- Click "STOP" → bot cancels ladders
- Settlement entries show winning/losing side breakdown

- [ ] **Step 3: Final commit (if any uncommitted changes remain)**

```bash
git status
# Only add specific files — never use git add -A
git commit -m "feat: complete dashboard redesign — futuristic theme, controls, live prices"
```

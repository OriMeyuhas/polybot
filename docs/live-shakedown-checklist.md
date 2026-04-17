# V1 Live Shakedown Checklist

**Purpose:** Data collection & functionality validation on real Polymarket V1.
**Not for profit.** Researcher issued NO-GO on profit — you're overriding for instrumentation value only.
**Duration:** Max **4 hours wall clock, attended**, max **$50 total loss**.

> Author: polybot-manager
> Written: 2026-04-17
> DO NOT run unattended. DO NOT repeat before reviewing the session data.

---

## 1. What was already changed for you

Code (merged to `main` working tree; WIP, not committed):
- `polybot/config.py` — added `MAX_PAIR_COST > 1.00` live-mode guard inside `validate_live_config()`; tightened default `directional_budget_cap` 20.0 → **18.0**.
- `polybot/bot.py` — in `start()`, live mode now **queries on-chain USDC balance**, overrides `.env BANKROLL`, and **refuses to start** if the query fails/returns $0/returns a malformed payload. Paper mode unchanged.
- `tests/test_live_startup_guards.py` — new file, 9 tests covering Fix A & Fix B.
- `tests/test_ladder_manager.py` — added one test pinning the $18 default.

`.env` (already updated, `DRY_RUN` NOT flipped):
| Key                                | Old       | New     |
|------------------------------------|-----------|---------|
| `MAX_PAIR_COST`                    | 1.05      | **0.98** |
| `POSITION_SIZE_FRACTION`           | 0.05      | **0.01** |
| `MAX_CONCURRENT_POSITIONS`         | 8         | **1**   |
| `DIRECTIONAL_BUDGET_CAP`           | 20.0      | **18.0** |
| `TRADE_BTC`, `TRADE_ETH`, `TRADE_15M`, `TRADE_1H` | (unchanged — already correct) | `true, false, true, false` |
| `BOOK_MID_GATE_ENABLED`            | (unchanged: `true`) | — |
| `BOOK_MID_GATE_CERTAINTY_THRESHOLD`| (unchanged: `0.65`) | — |

Test suite: **957 passed, 0 failed** (baseline 947 + 10 new).

---

## 2. IMPORTANT: the `POSITION_SIZE_FRACTION` and `MAX_CONCURRENT_POSITIONS` caveat

The `.env` values `POSITION_SIZE_FRACTION=0.01` and `MAX_CONCURRENT_POSITIONS=1` are **overridden at runtime** by `get_trading_rules(bankroll)` inside `config.py`. The bot bucket-sizes everything off the (on-chain) bankroll:

| Bankroll         | Assets | Timeframes | Max concurrent | Position fraction |
|------------------|--------|------------|----------------|-------------------|
| `< $200` (Micro) | 1      | 15m only   | **2**          | **15%**           |
| `$200–$400`      | 1      | 15m only   | 3              | 10%               |
| `$400–$2000`     | 2      | 15m + 1h   | 4              | 10%               |
| `$2000+`         | all    | all        | 8              | 2–5%              |

**Implication for the shakedown:** the wallet's real USDC balance determines real sizing, not the `.env` values. If you fund the wallet with, say, **$100**, the bot will enter Micro tier: **2 concurrent, 15% per window = $15 per post**. Cap of $18 still binds any directional post.

**Recommendation for a $50-loss-cap session:**
- Fund the Polygon wallet with **$100 USDC** (not $500+ — lower tier = smaller posts = more margin of error).
- At Micro tier with 2 concurrent × $15 post × 15m windows, absolute worst case per window ≈ $15 one-sided. After the $18 cap and two windows in flight, gross at-risk ≈ $30. 4 hours of windows = 16×15m → plenty of data, bounded loss.

If you don't want to touch the tier system: **override the tier at runtime** by editing `get_trading_rules()` to gate on `DRY_RUN` / a new `LIVE_SHAKEDOWN_MODE` flag. Out of scope for this pass — flag it if you want it before running live.

---

## 3. Pre-flight checklist (run each, tick off before flipping)

### 3.1 Credentials
- [ ] `.env` `PRIVATE_KEY` set to the wallet's private key (0x-prefixed, 64 hex chars)
- [ ] `.env` `API_KEY`, `API_SECRET`, `API_PASSPHRASE` set to CLOB L2 creds (generate via `py-clob-client` if missing)
- [ ] Derived address matches the wallet you funded:
      ```bash
      python -c "from polybot.config import load_bot_config; from eth_account import Account; cfg=load_bot_config(); print(Account.from_key(cfg.private_key).address)"
      ```

### 3.2 Wallet & balance
- [ ] Wallet funded with **$100 USDC on Polygon** (not Ethereum mainnet; not USDC.e — check the Polymarket UI shows the balance)
- [ ] Enough MATIC for gas (0.5 MATIC is plenty; allowance setting costs ~0.05 MATIC)
- [ ] USDC allowance set for Polymarket CTF Exchange AND CTF Exchange via `py-clob-client.set_api_creds()` / Polymarket UI "Enable Trading"

### 3.3 Smoke tests (all from paper mode first — `DRY_RUN=true`)
- [ ] `python -m pytest tests/ -q` — 957 pass, 0 fail
- [ ] Bot starts without errors:
      ```bash
      tail -50 polybot.log
      ```
      expect `Starting PolyBot in PAPER mode` and no `ERROR`/`Traceback`
- [ ] Dashboard loads at `http://127.0.0.1:8080` and shows `bankroll`, `ladder count`, live prices
- [ ] `BOOK_MID_GATE` log lines appear when certainty > 0.65 (seen in smoke test run: `BOOK MID GATE ... cap=$18.00`)
- [ ] `cancel_all` works — stop the paper bot; in live, `bot.stop()` calls `order_executor.cancel_all()` with a 10s timeout. Verify no exception is logged.

### 3.4 Live-only guard rehearsal (paper — DRY_RUN=true)
- [ ] Temporarily set `MAX_PAIR_COST=1.05` in `.env`, run `python -c "from polybot.config import load_bot_config, validate_live_config; errors=validate_live_config(load_bot_config()); print(errors)"` — expect the guaranteed-loss error string
- [ ] Revert `.env` to `MAX_PAIR_COST=0.98`

---

## 4. Flipping to live (the commands)

Only after every checkbox above is ticked.

### 4.1 Edit `.env`
```
DRY_RUN=false
```
(Leave everything else — `BANKROLL`, `POSITION_SIZE_FRACTION`, etc. — as they are. Live startup will override `BANKROLL` from on-chain and log the override loudly.)

### 4.2 Stop the paper bot
```bash
wmic process where "name='python.exe'" get ProcessId,CommandLine 2>/dev/null | grep run_bot
taskkill //PID <pid> //F
```

### 4.3 Start live
```bash
python run_bot.py
```
`run_bot.py` will:
1. Print `!!  LIVE TRADING MODE — real orders will be placed!`
2. Prompt `Type 'CONFIRM' to proceed:` — type `CONFIRM`
3. Run `validate_live_config(cfg)` — aborts if `MAX_PAIR_COST > 1.00` (Fix A)
4. `start()` will fetch on-chain balance and log `[LIVE] Bankroll from on-chain: $X.XX (ignoring .env BANKROLL=$Y)` (Fix B)
5. Enter standby — then open `http://127.0.0.1:8080` and hit **Start**

If step 4 fails (balance query errors / balance is zero / malformed), the bot will raise a `RuntimeError` and exit. That is intentional — do not silently fall back to `.env`.

### 4.4 Start the clock
Record the UTC start time. **Wall-clock limit: 4 hours.** Set a phone timer.

---

## 5. Session hard-caps & auto-kill

**Manual kill the moment any one of these trips:**

| Condition                              | How you see it                                      | Action                 |
|----------------------------------------|-----------------------------------------------------|------------------------|
| Cumulative PnL ≤ **−$50**              | Dashboard `session pnl` value, or tail settlement_log | `taskkill //F` + review |
| 3 consecutive losses                   | `tail -3 data/settlement_log.jsonl` — all negative  | kill + review          |
| Connection loss > 30 s                 | `PRICE FEED STALE` log line for >30s                | kill + investigate     |
| `cancel_all` times out or errors       | `ERROR` line from `order_executor.cancel_all`       | kill; cancel via Polymarket UI; audit fills |
| `MAX_PAIR_COST` guard trips on restart | startup exit with the `guaranteed loss` message     | fix `.env`, do not bypass |
| Runaway order count                    | Dashboard `resting orders` > ~40                    | kill; `cancel_all` on restart |
| 4-hour wall clock                      | Your phone alarm                                    | clean stop             |

**Clean stop:**
```bash
# Ctrl-C if running in foreground, or:
taskkill //PID <pid> //F
# Verify no resting orders remain (live):
python -c "from polybot.config import load_bot_config; from polybot.oms.clob_client import create_clob_client; c=create_clob_client(load_bot_config()); print(c.get_orders())"
```

---

## 6. Post-session review

Do these BEFORE planning another live run.

### 6.1 Settlement summary
```bash
# Last N settlements this session (adjust N):
tail -30 data/settlement_log.jsonl | python -c "
import sys, json
rows=[json.loads(l) for l in sys.stdin if l.strip()]
pnl=sum(r.get('pnl',0) for r in rows)
wins=sum(1 for r in rows if r.get('pnl',0)>0)
print(f'{len(rows)} settlements, {wins} wins ({wins/max(len(rows),1)*100:.0f}%), total PnL=\${pnl:+.2f}')
for r in rows: print(f\"  {r.get('pnl'):+7.2f}  up={r.get('up_qty',0):.1f} dn={r.get('dn_qty',0):.1f}  pair_cost={r.get('pair_cost')}  {r.get('market_id')}\")
"
```

### 6.2 Live vs paper WR comparison
```bash
# Paper baseline: last 50 paper settlements
tail -50 data/settlement_log.jsonl  # use tail indexes for the paper range
# Live: only rows with bankroll derived from on-chain (you'll know the range from timestamps)
```
Flag any systemic difference > 10pp win rate — that's adverse selection you didn't see in paper.

### 6.3 Log audit
```bash
grep -E "ERROR|CRITICAL|Traceback|STALE|ABORT|CIRCUIT BREAKER" polybot.log
grep "\[LIVE\]" polybot.log
grep "BOOK MID GATE" polybot.log | wc -l     # how often the gate fired
grep "ONE-SIDED ABORT" polybot.log | wc -l   # did the abort path trigger?
```

### 6.4 Root-cause pending: paired-ladder adverse selection
Observed in paper: a **−$22.62** settlement on a paired-ladder window where only the UP side filled. `directional_budget_cap` did **NOT** bind — it only bounds intentional one-sided posts (FV gate / book-mid gate / spot skip). Paired ladders with one-sided fills are a separate code path.

If live shows the same pattern, the next proposal is: extend the cap to paired post budgets, OR add a tighter per-side post-fill exposure cap. DO NOT ship either speculatively — collect data first.

---

## 7. Files referenced

- `.env` — `C:\Users\pc\Desktop\Bots\PolyBot\.env`
- `polybot/config.py` — `MAX_PAIR_COST` guard, cap default
- `polybot/bot.py` — live balance override in `start()`
- `tests/test_live_startup_guards.py` — regression for Fix A & Fix B
- `data/settlement_log.jsonl` — one line per settled market
- `polybot.log` — runtime log (truncated on every bot restart)
- `docs/plans/2026-04-17-live-readiness-proposal.md` — the researcher's NO-GO

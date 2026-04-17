# Agent Team Refactor — Design Spec

**Date**: 2026-04-07
**Status**: Draft

## Goal

Make the agent team maintenance-free and effective: update CLAUDE.md as the single source of truth, make all execution agents generic (no hardcoded numbers), and upgrade the researcher from reactive data analyst to domain expert strategist.

## Scope

Three workstreams:

1. **Rewrite CLAUDE.md** — current version is 18 days stale. Make it the definitive project context file that all agents reference.
2. **Make 4 execution agents generic** — planner, coder, debugger, tester stop hardcoding facts and discover them at runtime.
3. **Upgrade researcher to strategist** — deep Polymarket domain knowledge, innovation mandate, three operating modes.

Plus: update the manager to allow skipping the planner for small fixes.

---

## 1. CLAUDE.md Rewrite

The single file every agent reads on startup. Covers what the project is, how it works, and how to operate it. No hardcoded numbers that change — reference `.env` and `config.py` instead.

### Structure

```markdown
# PolyBot — Claude Context

## What Is This
Binary options market-making + arbitrage bot for Polymarket crypto up/down markets.

## Strategy
Two interlocking edges:
1. **Market-making**: Passive limit order ladders on both UP and DN sides.
   When both sides fill, pair cost < $1.00 = guaranteed profit on settlement.
2. **Arbitrage**: FV brain computes fair value from Binance spot price in
   real-time. Polymarket CLOB prices lag — the bot cancels the losing side
   (FV cancel at 60% certainty), exits held losers (FV exit at 30%), and
   buys winners directionally (FV directional at 92%). This is Binance→Polymarket
   information arbitrage.

## Architecture
polybot/
├── data/           # Binance WS, CLOB midpoints, Gamma discovery, books, data recorder
├── oms/            # Paper/live CLOB client, order executor, heartbeat
├── strategy/       # Ladder manager, order tracker, position manager, fair value, vol estimator
├── web/            # Dashboard (aiohttp + static JS/CSS/HTML)
├── bot.py          # Central orchestrator (standby → Start button → trading loop)
├── config.py       # BotConfig (frozen dataclass), LadderParams, env var loading
├── types.py        # MarketWindow, Position, Side, OrderRecord
└── risk_manager.py # Position limits, exposure factor

## Config
All tunable parameters live in `.env` with defaults in `config.py`.
Read these files for current values — do not assume or hardcode them.
Key params: BANKROLL, MAX_PAIR_COST, LADDER_WIDTH, LADDER_RUNGS,
LADDER_SIZE_SKEW, POSITION_SIZE_FRACTION, BOT_POLL_INTERVAL_MS.

## Data Files
- data/settlement_log.jsonl — every settlement (PnL, fills, pair cost, outcome)
- data/order_log_YYYY-MM-DD.jsonl — every order post/fill/cancel
- data/book_log_YYYY-MM-DD.jsonl — Polymarket book snapshots
- data/price_log_YYYY-MM-DD.jsonl — Binance + Chainlink prices
- data/trade_log_YYYY-MM-DD.jsonl — Polymarket trades on our markets
- data/manager_log.jsonl — manager agent health checks and actions
- data/manager_state.jsonl — manager agent state checkpoint
- polybot.log — bot runtime log

## Bot Operations
- Start: `python run_bot.py > polybot.log 2>&1 &`
- Activate trading: `curl -s -X POST http://127.0.0.1:8080/api/start`
- Dashboard: http://127.0.0.1:8080
- Stop: find PID via `wmic process where "name='python.exe'" get ProcessId,CommandLine`,
  then `taskkill //PID <pid> //F`
- IMPORTANT: always update BANKROLL in .env from last settlement before restart
- IMPORTANT: always hit /api/start after restart — bot starts in standby

## Testing
Run: `python -m pytest tests/ -q`
All tests must pass before any deploy. Check the count from pytest output.

## Agent Team
6 agents in .claude/agents/:
- polybot-manager — autonomous operator (monitor, dispatch, deploy)
- polybot-researcher — domain expert strategist (analyze, innovate, propose)
- polybot-planner — writes implementation plans to docs/plans/
- polybot-coder — implements plans with TDD
- polybot-tester — validates (pytest + paper mode)
- polybot-debugger — root cause analysis

## Current State
- Paper mode, BTC only, 15m + 1h windows
- Polymarket V2 migration coming in 2-3 weeks (clob-client v6 required)
- Platform: Windows 10, Python 3.14, bash shell
```

### Principles
- No hardcoded test counts, thresholds, or config values
- "Read from X" instead of "the value is Y"
- Architecture section updated when files are added/removed
- Current State section is the only part that needs periodic updates

---

## 2. Generic Execution Agents

### Changes to all 4 agents (planner, coder, debugger, tester)

**Remove from all:**
- Hardcoded test counts ("302 tests", "690 tests")
- Hardcoded config values ("max_pair_cost = 0.90")
- References to functions that may not exist
- Stale file paths

**Replace with:**
- "Read CLAUDE.md for project context"
- "Check `.env` and `config.py` for current config values"
- "Run `pytest tests/ -q` — all tests must pass"
- "Grep the codebase before referencing specific functions"

**Keep unchanged:**
- Role descriptions and identity
- Process workflows (TDD for coder, systematic debugging for debugger, etc.)
- Output contracts (Root Cause + Fix for debugger, PASS/FAIL for tester, etc.)
- Safety rails (never break tests, never force-push, etc.)
- Skills references

### Per-agent specifics

**polybot-planner.md:**
- Remove hardcoded invariant values
- Replace with: "Read current invariants from CLAUDE.md and `.env`"
- Keep: architecture map (but say "verify against actual file structure"), output to `docs/plans/`, ordered change list format

**polybot-coder.md:**
- Remove hardcoded invariant values and function references
- Replace with: "Read CLAUDE.md, check `.env` for thresholds, grep for function names before referencing"
- Keep: TDD workflow, isolation (worktree when available), code style guidelines

**polybot-debugger.md:**
- Remove hardcoded debugging map function names
- Replace with: "Grep for the relevant function in the actual codebase"
- Keep: systematic process (reproduce → trace → isolate → fix → verify), memory of known bugs
- Change model from sonnet to opus (the fill bug today needed deep reasoning)

**polybot-tester.md:**
- Remove hardcoded test count
- Replace with: "Run pytest, all must pass — check the count from output"
- Keep: validation steps, PASS/FAIL verdict, cannot go idle while failing

### Manager update

Add to polybot-manager.md prompt, in the Dispatch Chain section:

> **Small fix shortcut:** If the debugger or researcher identifies a specific, contained fix (1-5 lines, single file), dispatch the coder directly without the planner. For changes touching 3+ files or introducing new features, dispatch the planner first.

---

## 3. Researcher Upgrade to Domain Expert Strategist

The researcher becomes the brain of the operation — a Polymarket specialist who thinks like a quant trader.

### New agent definition: polybot-researcher.md

**Frontmatter:**
```yaml
name: polybot-researcher
description: PolyBot domain expert and strategist. Analyzes performance, hunts for alpha, proposes innovations. Thinks like a quant trader specializing in Polymarket binary options.
tools: Read, Glob, Grep, Bash, Write, WebSearch, WebFetch
disallowedTools: Edit
model: opus
memory: project
```

**Three operating modes:**

1. **Reactive** (dispatched by manager on problems):
   - "Win rate dropped" / "consecutive losses" / "errors in log"
   - Root cause analysis with data evidence
   - Output: Improvement Proposal (observation, evidence, proposed change, expected impact, risk)

2. **Proactive** (dispatched every 10 settlements):
   - Analyze recent settlement data for patterns
   - Ask: "What's the biggest source of drag? Can we improve fill rates? Reduce adverse selection?"
   - Output: Improvement Proposal, or "healthy — no changes needed"

3. **Strategic** (dispatched every 50 settlements (~12 hours), or on user request):
   - Think bigger: new strategies, new markets, competitive analysis
   - Ask: "What are we not doing that we should be? What's the next edge?"
   - Output: Strategic Memo (idea, rationale, research needed, estimated impact)

**Domain knowledge (concepts, not numbers):**

- **Polymarket mechanics**: Binary options settle at $1.00 or $0.00. Maker fee is 0%, taker fee is `0.072 * p * (1-p)`. 20% rebate in crypto. Resolution uses Chainlink (not Binance — important for outcome prediction).
- **Arbitrage edge**: Binance spot price is real-time, Polymarket CLOB prices lag by seconds to minutes. This information asymmetry is the primary edge. Fair value computation from spot delta predicts which side wins.
- **Market microstructure**: Binary token books are thin. Best ask is often $0.99 (seed liquidity). Real depth forms within first 1-2 minutes. Queue position matters — passive orders fill faster when closer to midpoint.
- **Adverse selection**: When we fill only one side, it's usually the losing side (the market moved against us). The FV cancel at 60% certainty is the primary defense. Structural adverse selection rate on these markets is 50-72%.
- **Competition**: Other market makers (bots) compete for the same fills. Tighter spreads and faster repricing win more fills but increase adverse selection risk.
- **Pair cost economics**: Revenue = $1.00 per paired share. Cost = UP fill price + DN fill price. Profit per pair = $1.00 - pair_cost. At pair_cost=0.95, profit is $0.05/pair (5%).
- **Time decay**: Binary option certainty increases as the window progresses. Early: 50/50. Late: near-certain. The FV brain models this via volatility and elapsed fraction.

**Analysis toolkit:**
```bash
# Settlement analysis
tail -N data/settlement_log.jsonl | python -m json.tool

# Order flow for specific market
grep "market-id" data/order_log_*.jsonl | grep "fill"

# Bot errors
grep -a "ERROR\|WARNING\|CRITICAL" polybot.log | tail -20

# Competitive analysis
# WebSearch for Polymarket market-making strategies, bot competition
# WebFetch Polymarket docs and API endpoints
```

**Innovation areas to explore:**
- New timeframes (5m profitability, 4h markets if they exist)
- Multi-asset correlation (BTC move predicts ETH move)
- Dynamic width adjustment based on volatility regime
- Optimal entry timing within window (early vs late)
- Exit strategies for losing positions
- Fee optimization and rebate capture
- Order book imbalance signals from Polymarket WS feed

**Output contracts:**

Improvement Proposal (for reactive/proactive):
```
## Improvement Proposal #N
**Observation:** [what the data shows — specific numbers]
**Evidence:** [exact data, log lines, computations]
**Proposed Change:** [exact file, function, config to change]
**Expected Impact:** [quantified estimate]
**Risk:** [what could break]
**Priority:** [CRITICAL / HIGH / MEDIUM / LOW]
```

Strategic Memo (for strategic mode):
```
## Strategic Memo: [Title]
**Idea:** [one paragraph]
**Rationale:** [why this could work — market structure, data, competitive advantage]
**Research Needed:** [what to validate before implementing]
**Estimated Impact:** [rough magnitude]
**Effort:** [small / medium / large]
```

---

## What Does NOT Change

- Manager agent definition (except adding the small-fix shortcut)
- TeammateIdle hook
- Bot code (no Python changes)
- Agent memory directories and content
- Plan/spec document structure

## Implementation Order

1. Rewrite CLAUDE.md
2. Update 4 execution agents (planner, coder, debugger, tester)
3. Rewrite researcher agent
4. Add small-fix shortcut to manager
5. Smoke test: dispatch manager, verify it can orchestrate the updated team

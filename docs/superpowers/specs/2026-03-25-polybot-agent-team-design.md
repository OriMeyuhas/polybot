# PolyBot Agent Team Design

**Date:** 2026-03-25
**Goal:** 5 role-based development agents to accelerate PolyBot iteration — focused on implementation (pain point B) and testing/validation (pain point C).

---

## Overview

Five agents organized by role, forming a linear chain with a feedback loop. Each agent has a single clear purpose and defined output contract. Agents live in `.claude/agents/` inside the PolyBot project directory — project-scoped and version-controlled alongside the code.

---

## Workflow

```
Researcher → Planner → Coder → Tester
                                  │
                            (fail) ↓
                              Debugger → Coder
```

1. **Researcher** always runs first. Proactively hunts for improvements via codebase analysis, trade log inspection, whale data, and live market research. Produces a structured Improvement Proposal.
2. **Planner** takes the Researcher's proposal and produces a concrete spec file at `docs/plans/YYYY-MM-DD-<topic>.md`. Never touches code.
3. **Coder** reads the plan file and implements in an isolated git worktree. Writes tests first (TDD). Has full tool access.
4. **Tester** validates — runs `pytest` + paper mode. Cannot go idle while tests are failing (TeammateIdle hook enforces this).
5. **Debugger** activates on test/run failures. Does systematic root-cause analysis. Produces Root Cause + Minimal Fix. Hands back to Coder.

---

## Agent Specifications

### 1. `polybot-researcher`

| Field | Value |
|---|---|
| **Model** | Opus |
| **Tools** | Read, Glob, Grep, Bash, WebSearch, WebFetch |
| **Disallowed tools** | Write, Edit |
| **Permission mode** | default (read + run only, no code edits) |
| **Memory** | `project` scope — accumulates strategy journal across sessions |
| **Skills** | `1.0.0:web3-polymarket` |
| **MCP tools** | `context7` — available for looking up `py_clob_client` and library docs |

**Role:** The strategist who always wants to make the bot better. Not a passive lookup tool — it proactively investigates, forms hypotheses, and proposes changes with evidence. Runs `analyze_trader.py` and `backtest_decisions.py`, reads PnL logs, queries Polymarket market data via web, searches for prediction market research, and uses context7 to look up library documentation when investigating API behavior.

**Output contract — always ends with:**
```
## Improvement Proposal
**Observation:** [what the data shows]
**Evidence:** [specific numbers, log lines, script output]
**Proposed Change:** [exact config param / module / logic to change]
**Expected Impact:** [quantified if possible]
**Risk:** [what could break]
```

---

### 2. `polybot-planner`

| Field | Value |
|---|---|
| **Model** | Opus |
| **Tools** | Read, Glob, Grep, Write |
| **Disallowed tools** | Bash, Edit |
| **Permission mode** | `acceptEdits` |
| **Skills** | `superpowers:writing-plans` |

**Role:** Turns the Researcher's Improvement Proposal into a concrete implementation spec. Maps all affected files, defines the order of changes, identifies test cases needed. Never modifies Python files — only writes to `docs/plans/`. No `Bash` access means it cannot run or test code.

**Output contract — always writes plan to:**
`docs/plans/YYYY-MM-DD-<topic>.md`

The Coder reads this file as its source of truth, not the chat history.

---

### 3. `polybot-coder`

| Field | Value |
|---|---|
| **Model** | Opus |
| **Tools** | All tools |
| **Permission mode** | `acceptEdits` |
| **Isolation** | `worktree` — works in an isolated git branch |
| **Skills** | `superpowers:test-driven-development` |

**Role:** Implements the plan from `docs/plans/`. Follows TDD — writes the failing test first, then implements. Knows the PolyBot codebase deeply: `bot.py`, `config.py`, `ladder_manager.py`, `risk_manager.py`, `data/`, `oms/`.

**Key invariants the Coder must never break:**
- `pair_cost < 1.00` is the profit condition — any change touching cost calculation must preserve this
- Settlement de-duplication via `_settled_markets` set must remain
- Bankroll tier system in `get_trading_rules()` must remain the source of truth for position sizing
- All 302 existing tests must continue to pass

---

### 4. `polybot-tester`

| Field | Value |
|---|---|
| **Model** | Sonnet |
| **Tools** | Read, Glob, Grep, Bash, Write |
| **Permission mode** | `acceptEdits` |
| **Skills** | `superpowers:verification-before-completion` |
| **Hook** | `TeammateIdle` — cannot go idle while any test is failing |

**Role:** Validates every change. Runs the full test suite (`pytest`) and paper mode (`python run_bot.py --paper`) for a minimum 2-minute run to check for crashes and fill logic. Can write new test files. Evidence before assertions — never claims "tests pass" without showing the output.

**Output contract — always ends with:**
```
## Validation Result
**pytest:** X passed, Y failed [paste output]
**Paper mode:** [passed/crashed] — [key log lines]
**Verdict:** PASS / FAIL
```

---

### 5. `polybot-debugger`

| Field | Value |
|---|---|
| **Model** | Sonnet |
| **Tools** | Read, Glob, Grep, Bash |
| **Permission mode** | `acceptEdits` |
| **Memory** | `project` scope — accumulates known bugs and fixes journal |
| **Skills** | `superpowers:systematic-debugging` |

**Role:** Activated when Tester returns FAIL. Systematically investigates root cause — traces the error through the call stack, checks if it's a known issue (via memory), finds the minimal reproduction. Never guesses — always confirms the root cause before proposing a fix.

**Output contract — always ends with:**
```
## Root Cause
[Exact cause — file:line, what broke, why]

## Minimal Fix
[The smallest possible change that resolves the issue]
[Must not break existing invariants]
```

---

## File Locations

```
PolyBot/
  .claude/
    agents/
      polybot-researcher.md
      polybot-planner.md
      polybot-coder.md
      polybot-tester.md
      polybot-debugger.md
  docs/
    superpowers/
      specs/
        2026-03-25-polybot-agent-team-design.md   ← this file
    plans/
      YYYY-MM-DD-<topic>.md                        ← Planner outputs here
```

---

## Model Selection Rationale

| Agent | Model | Reason |
|---|---|---|
| Researcher | Opus | Interprets complex market data, forms strategy hypotheses, reasons across multiple evidence sources |
| Planner | Opus | Architectural decisions, maps complex dependencies across tightly-coupled modules |
| Coder | Opus | `bot.py` + `ladder_manager.py` + `config.py` interplay is complex; wrong edits cause real financial loss |
| Tester | Sonnet | Mechanical work — run commands, check output, write assertions |
| Debugger | Sonnet | Systematic process — trace errors, check stack, find line — doesn't require Opus-level reasoning |

---

## Cost Notes

- 3 Opus + 2 Sonnet agents
- Researcher and Planner are read-only — lower token cost than Coder
- Coder uses worktree isolation — no wasted work on polluted main branch
- TeammateIdle hook on Tester prevents it from looping indefinitely on failures

# PolyBot Manager Agent — Design Spec

**Date**: 2026-04-07
**Status**: Draft

## Goal

An autonomous manager agent that monitors the bot's health, dispatches the agent team to investigate and fix problems, proactively hunts for improvements, and deploys changes — all without human intervention. Escalates only when it fails 3 times on the same issue.

## Agent Definition

File: `.claude/agents/polybot-manager.md`

```yaml
name: polybot-manager
description: Autonomous bot operator. Monitors health, dispatches agents, deploys fixes, hunts improvements.
tools: Read, Glob, Grep, Bash, Write, Edit, Agent, SendMessage
model: opus
permissionMode: bypassPermissions
memory: project
```

- **opus** — makes judgment calls about when to investigate and how to synthesize findings
- **bypassPermissions** — fully autonomous, no approval prompts
- **All tools** — Bash for bot restart, Agent for dispatching team, Read/Write for logs and config
- **Project memory** — persists learnings to `.claude/agent-memory/polybot-manager/`

## Core Loop

The manager runs an infinite loop with two modes:

### Monitor Cycle (every ~60 seconds)

1. Read last line of `data/settlement_log.jsonl`
2. Compare timestamp to last-seen (skip if no new settlement)
3. If new settlement, run health checks:
   - **Consecutive losses**: 3+ in a row → trigger investigation
   - **Win rate**: < 40% over last 10 settlements → trigger investigation
   - **Bankroll drawdown**: > 15% from session start → trigger investigation
   - **Error log**: scan `polybot.log` for ERROR/CRITICAL/Traceback → trigger investigation
4. Log health status to `data/manager_log.jsonl`
5. Check context usage — after every 20 monitor cycles (~20 min), perform context rotation (see below). This is a heuristic proxy for 35-40% context usage since agents cannot directly measure their own context consumption.

### Improvement Cycle (every 10 settlements)

1. Dispatch `polybot-researcher` to analyze recent settlement data
2. If researcher returns a proposal → route through dispatch chain
3. If no proposal → log "no improvements found", continue monitoring

### Dispatch Chain (sequential, one at a time)

```
Problem detected or improvement proposed
  → Agent(polybot-debugger) or Agent(polybot-researcher): investigate
  → Read findings
  → Agent(polybot-planner): create plan from findings
  → Agent(polybot-coder): implement plan (isolated worktree)
  → Agent(polybot-tester): validate (full test suite + paper mode check)
  → If PASS: merge worktree, update .env bankroll, restart bot, hit /api/start
  → If FAIL: Agent(polybot-debugger): investigate test failures
  → Retry up to 3x total
  → If still failing: revert all changes, escalate to user
```

Only one dispatch chain runs at a time. The next improvement waits for the current chain to complete.

### Bot Restart Procedure

1. Read last bankroll from `data/settlement_log.jsonl`
2. Update `BANKROLL=` in `.env` to match
3. Find bot PID: `wmic process where "name='python.exe'" get ProcessId,CommandLine` and find `run_bot.py`
4. `taskkill //PID <pid> //F`
5. `cd PolyBot && python run_bot.py > polybot.log 2>&1 &`
6. Wait 5 seconds
7. `curl -s -X POST http://127.0.0.1:8080/api/start`
8. Verify "Trading started" appears in `polybot.log`
9. If restart fails after 3 attempts → escalate

## Context Rotation

When the manager detects its context usage is approaching 35-40%:

1. Write checkpoint to `data/manager_state.jsonl`:
   ```json
   {
     "last_settlement_ts": 1775516429,
     "session_start_bankroll": 500.0,
     "current_bankroll": 455.87,
     "settlements_since_last_research": 7,
     "in_progress_chain": null,
     "retry_count": 0,
     "escalations": []
   }
   ```
2. Spawn a new instance of itself via Agent tool:
   ```
   Agent(polybot-manager): "Continue managing. Read state from data/manager_state.jsonl. Resume the monitor/improvement loop."
   ```
3. Exit cleanly

The new instance reads the state file and continues where the previous left off.

## Escalation

### Triggers (stop autonomous work, notify user)

- 3 failed fix attempts on the same issue
- Bankroll drops below $200 (hard floor — also stop the bot)
- Bot process dies and can't be restarted after 3 attempts
- Researcher proposes a change touching >5 files

### Method

1. Write to `data/escalation.jsonl`:
   ```json
   {
     "ts": 1775516429,
     "reason": "3 failed fix attempts",
     "issue": "description of what went wrong",
     "attempts": ["attempt 1 summary", "attempt 2", "attempt 3"],
     "current_state": "bot running on last working version",
     "recommended_action": "manual investigation of X"
   }
   ```
2. Log `ESCALATE: <reason>` to manager log
3. Keep bot running on last working version (do not stop it)
4. Stop the improvement loop, continue monitoring only
5. Wait for user to dispatch manager again with instructions

## Safety Rails

- Never deploy code that fails tests (all 690+ tests must pass)
- Never modify `.env` credentials or API keys — only BANKROLL
- Always restart with bankroll from last settlement
- Always hit `/api/start` after restart
- If bot down >5 min and unrecoverable → escalate immediately
- One dispatch chain at a time (sequential, no parallel code changes)
- Never force-push or delete git branches without user approval

## Manager Log Format

`data/manager_log.jsonl` — one line per check:

```json
{
  "ts": 1775516429,
  "event": "health_check",
  "bankroll": 455.87,
  "pnl_session": -44.13,
  "last_settlement_pnl": -19.07,
  "consecutive_losses": 3,
  "win_rate_10": 0.40,
  "action": "trigger_investigation",
  "reason": "3 consecutive losses"
}
```

## How to Start

Dispatch once:
```
Agent(polybot-manager): "Start managing the bot. Monitor health, fix problems, find improvements."
```

It runs from there. If it stops (context limit, error, user interrupt), dispatch again — it reads `manager_state.jsonl` and resumes.

## Existing Team (unchanged)

| Agent | Role | When Dispatched |
|-------|------|-----------------|
| polybot-researcher | Analyzes data, proposes improvements | Every 10 settlements, or on problem detection |
| polybot-planner | Writes implementation plan from proposals | After researcher/debugger findings |
| polybot-coder | Implements plan in isolated worktree (TDD) | After planner writes plan |
| polybot-tester | Runs tests + paper mode validation | After coder finishes |
| polybot-debugger | Root-cause analysis of failures | On problem detection, or when tester fails |

## What This Does NOT Change

- No changes to the 5 existing agent definitions
- No changes to the bot code itself
- No changes to the TeammateIdle hook
- The manager is a new agent added alongside the existing team

# PolyBot — Claude Context

## What Is This

PolyBot is an automated **market-making + arbitrage bot** for Polymarket's short-duration crypto prediction markets (15m, 1h windows). It trades binary UP/DOWN contracts on BTC using passive limit order ladders and Binance→Polymarket information arbitrage.

## Strategy

Two interlocking edges:

1. **Market-making** — Posts passive limit order ladders on both UP and DN sides of binary option markets. When both sides fill, the combined pair cost is < $1.00. At settlement, the winning side pays $1.00/share = guaranteed profit on the spread.

2. **Arbitrage** — The Fair Value (FV) brain computes real-time probability from Binance spot price movement. Polymarket CLOB prices lag behind spot by seconds to minutes. The bot exploits this:
   - **FV cancel** (60% certainty): cancels resting orders on the losing side before they fill
   - **FV exit** (30% certainty): sells held losing positions
   - **FV directional** (92% certainty): buys the winning side at a discount
   - **FV gate**: blocks posting the losing side entirely when entering mid-window

## Architecture

```
polybot/
├── data/           # Binance WS, CLOB midpoints, Gamma discovery, books, data recorder
├── oms/            # Paper/live CLOB client, order executor, heartbeat
├── strategy/       # Ladder manager, order tracker, position manager, fair value, vol estimator
├── web/            # Dashboard (aiohttp + static JS/CSS/HTML)
├── bot.py          # Central orchestrator (standby → Start button → trading loop)
├── config.py       # BotConfig (frozen dataclass), LadderParams, env var loading
├── types.py        # MarketWindow, Position, Side, OrderRecord
└── risk_manager.py # Position limits, exposure factor
```

## Config

All tunable parameters live in `.env` with defaults in `config.py`. **Read these files for current values — never hardcode them.** Key params include: BANKROLL, MAX_PAIR_COST, LADDER_WIDTH, LADDER_RUNGS, LADDER_SIZE_SKEW, POSITION_SIZE_FRACTION, BOT_POLL_INTERVAL_MS.

Asset selection: `TRADE_BTC=true`, `TRADE_ETH=false`, etc.

## Data Files

- `data/settlement_log.jsonl` — every settlement (PnL, fills, pair cost, outcome)
- `data/order_log_YYYY-MM-DD.jsonl` — every order post/fill/cancel
- `data/book_log_YYYY-MM-DD.jsonl` — Polymarket book snapshots
- `data/price_log_YYYY-MM-DD.jsonl` — Binance + Chainlink prices
- `data/trade_log_YYYY-MM-DD.jsonl` — Polymarket trades on our markets
- `data/manager_log.jsonl` — manager agent health checks and actions
- `data/manager_state.jsonl` — manager agent state checkpoint
- `polybot.log` — bot runtime log

## Bot Operations

```bash
# Start
python run_bot.py > polybot.log 2>&1 &

# Activate trading (bot starts in standby — this is REQUIRED)
sleep 5 && curl -s -X POST http://127.0.0.1:8080/api/start

# Dashboard
# http://127.0.0.1:8080

# Stop
wmic process where "name='python.exe'" get ProcessId,CommandLine  # find run_bot.py PID
taskkill //PID <pid> //F
```

**Before every restart:** update BANKROLL in `.env` from the last settlement in `settlement_log.jsonl`.

## Testing

```bash
python -m pytest tests/ -q    # All tests must pass — check count from output
```

## Agent Team

6 agents in `.claude/agents/`:

| Agent | Role |
|-------|------|
| polybot-manager | Autonomous operator: monitor health, dispatch agents, deploy fixes |
| polybot-researcher | Domain expert strategist: analyze, innovate, propose improvements |
| polybot-planner | Write implementation plans to `docs/plans/` |
| polybot-coder | Implement plans with TDD |
| polybot-tester | Validate: pytest + paper mode check |
| polybot-debugger | Root cause analysis of bugs and failures |

The manager orchestrates the others. For small fixes (1-5 lines), it can skip the planner and dispatch the coder directly.

## Key Design Decisions

- **Standby mode**: Bot starts data feeds immediately but waits for `/api/start` to begin trading
- **Dynamic sizing**: `get_trading_rules(assets, bankroll)` in `config.py` scales position size with bankroll
- **Paper vs Live**: `DRY_RUN=true` uses `PaperClobClient` for fill simulation; live mode uses `py-clob-client` SDK
- **Resolution**: Polymarket resolves via Chainlink oracle (not Binance) — this matters for outcome prediction accuracy

## Polymarket Notes

- **Fees**: Maker 0%, taker `0.072 * p * (1-p)`, 20% rebate in crypto
- **APIs**: Gamma API for market discovery, CLOB API for midpoints/books/orders
- **SDK**: `py-clob-client` for order placement and balance queries
- **V2 Migration**: Exchange upgrade coming in ~2 weeks (announced 2026-04-06). Will require `clob-client v6`, new order struct, new collateral token. Do not go live on V1.

## Current State

- Paper mode, BTC only, 15m + 1h windows
- Platform: Windows 10, Python 3.14, bash shell
- Polymarket V2 migration blocks live deployment
- Use this time to validate strategy in paper mode

## V2 Migration Status (2026-04-19)

- Codebase migrated to `py-clob-client-v2==1.0.0` (host: `clob-v2.polymarket.com`)
- Paper mode unchanged; live mode requires:
  1. `.env`: rename `CHAIN_ID` → `CHAIN` (legacy still works with warning)
  2. `.env`: set `PUSD_ADDRESS` and `COLLATERAL_ONRAMP_ADDRESS` from Polymarket V2 contracts reference
  3. `.env`: `WRAP_ON_STARTUP=true` to auto-wrap USDC→pUSD at launch
  4. Deposit USDC to the bot wallet on Polygon before flipping `DRY_RUN=false`
- April 28 forced cutover: no action required — already on V2 endpoint

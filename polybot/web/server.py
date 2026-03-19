"""Web dashboard server: FastAPI app, state serializer, WebSocket broadcast."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polybot.bot import Bot


def build_state_snapshot(bot: Bot) -> dict:
    """Build a JSON-serializable snapshot of all bot state."""
    cfg = bot.cfg
    now = time.time()
    now_epoch = int(now)

    daily_pnl = bot.risk_manager.daily_pnl
    starting = bot.risk_manager.starting_bankroll
    pnl_pct = (daily_pnl / starting * 100) if starting > 0 else 0.0

    spots = {}
    for asset in cfg.assets:
        price = bot.spot_prices.get(asset, 0.0)
        delta = bot.compute_spot_delta(asset) if price > 0 else 0.0
        spots[asset] = {"price": price, "delta": round(delta, 6)}

    market_map = {m.market_id: m for m in list(bot.active_markets)}

    ladders = []
    for mid in list(bot.ladder_manager.ladders):
        stats = bot.ladder_manager.get_ladder_stats(mid)
        market = market_map.get(mid)
        asset = market.asset if market else ""
        tf = market.timeframe_sec if market else 0
        time_left = market.remaining(now_epoch) if market else 0
        ladders.append({
            "market_id": mid, "asset": asset, "timeframe_sec": tf,
            "up_resting": stats["up_resting"], "dn_resting": stats["dn_resting"],
            "up_filled": stats["up_filled"], "dn_filled": stats["dn_filled"],
            "up_vwap": round(stats["up_vwap"], 4), "dn_vwap": round(stats["dn_vwap"], 4),
            "pair_cost": round(stats["combined_vwap"], 4),
            "imbalance": round(stats["imbalance"], 4),
            "time_left_sec": time_left,
        })

    positions = []
    for mid, pos in list(bot.position_manager.positions.items()):
        market = market_map.get(mid) or bot._expired_market_cache.get(mid)
        asset = market.asset if market else ""
        pnl_up = pos.profit_if_up()
        pnl_dn = pos.profit_if_down()
        positions.append({
            "market_id": mid, "asset": asset,
            "up_qty": round(pos.up_qty, 2), "up_cost": round(pos.up_cost, 2),
            "dn_qty": round(pos.dn_qty, 2), "dn_cost": round(pos.dn_cost, 2),
            "pnl_if_up": round(pnl_up, 2), "pnl_if_down": round(pnl_dn, 2),
            "pnl_worst_case": round(min(pnl_up, pnl_dn), 2),
        })

    activity = []
    for ev in list(bot._activity_log):
        activity.append({"ts": ev.timestamp, "type": ev.event_type, "asset": ev.asset, "detail": ev.detail, "pnl": ev.pnl})

    deployed = bot.ladder_manager.total_committed()
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

    wallet = {"address": address, "usdc_balance": round(balance, 2), "deployed": round(deployed, 2), "available": round(balance - deployed, 2)}

    return {
        "mode": "dry_run" if cfg.dry_run else "live",
        "uptime_sec": round(now - bot._start_time, 1),
        "bankroll": bot.position_manager.bankroll,
        "daily_pnl": round(daily_pnl, 2), "daily_pnl_pct": round(pnl_pct, 2),
        "heartbeat_healthy": bot.heartbeat.is_healthy() if bot.heartbeat else True,
        "cancel_only_mode": bot._cancel_only_mode,
        "risk_halted": bot.risk_manager.is_halted(),
        "wallet": wallet, "spots": spots, "ladders": ladders, "positions": positions,
        "pending_settlements": list(bot.position_manager.get_pending_settlements()),
        "failed_settlements": list(bot.position_manager.get_failed_settlements()),
        "activity": activity,
    }

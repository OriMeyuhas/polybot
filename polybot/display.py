"""Rich terminal dashboard for the PolyBot trading engine."""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from polybot.utils.time_utils import format_duration, format_elapsed


@dataclass
class ActivityEvent:
    timestamp: float
    event_type: str  # SPREAD, DIRECTIONAL, SETTLE, STOP_LOSS
    asset: str
    detail: str
    pnl: float | None = None


def build_display(bot) -> Layout:
    """Build the rich terminal layout from live bot state."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="spot", size=3),
        Layout(name="positions", ratio=2),
        Layout(name="activity", ratio=2),
        Layout(name="risk", size=3),
    )

    # -- Header --
    uptime = format_duration(time.time() - bot._start_time)
    mode_badge = "[bold yellow] DRY RUN [/]" if bot.cfg.dry_run else "[bold red] LIVE [/]"
    daily_pnl = bot.risk_manager.daily_pnl
    bankroll = bot.position_manager.bankroll
    pnl_pct = (daily_pnl / bot.risk_manager.starting_bankroll * 100) if bot.risk_manager.starting_bankroll > 0 else 0.0
    pnl_color = "green" if daily_pnl >= 0 else "red"
    pos_count = bot.position_manager.active_position_count()
    status = "[red]HALTED[/]" if bot.risk_manager.is_halted() else "[green]ACTIVE[/]"

    header_text = Text.from_markup(
        f"[bold cyan]POLYBOT TRADING ENGINE[/] {mode_badge}  "
        f"Uptime: {uptime}  |  "
        f"Bankroll: [bold]${bankroll:,.2f}[/]  |  "
        f"PnL: [{pnl_color}]${daily_pnl:+,.2f} ({pnl_pct:+.2f}%)[/{pnl_color}]  |  "
        f"Trades: {bot._trade_count}  |  "
        f"Positions: {pos_count}/{bot.cfg.max_concurrent_positions}  |  "
        f"Status: {status}"
    )
    layout["header"].update(Panel(header_text, style="bold"))

    # -- Spot Prices --
    spot_parts = []
    for asset in bot.cfg.assets:
        price = bot.spot_prices.get(asset, 0.0)
        if price > 0:
            delta = bot.compute_spot_delta(asset)
            delta_color = "green" if delta >= 0 else "red"
            spot_parts.append(
                f"[bold]{asset}:[/] ${price:,.2f} [{delta_color}]({delta:+.3%})[/{delta_color}]"
            )
        else:
            spot_parts.append(f"[dim]{asset}:[/] --")
    spot_text = "   ".join(spot_parts)
    layout["spot"].update(
        Panel(Text.from_markup(f"[bold]SPOT PRICES[/]  {spot_text}"), style="blue")
    )

    # -- Active Positions --
    pos_table = Table(
        title="ACTIVE POSITIONS", expand=True, show_lines=False,
        title_style="bold white",
    )
    pos_table.add_column("Market", style="dim", width=20)
    pos_table.add_column("Side", width=6)
    pos_table.add_column("Qty", justify="right", width=8)
    pos_table.add_column("Avg", justify="right", width=8)
    pos_table.add_column("PnL", justify="right", width=10)
    pos_table.add_column("Strategy", width=12)

    for mid, pos in bot.position_manager.positions.items():
        short_id = mid.split("_")[-1] if "_" in mid else mid[-8:]
        has_up = pos.up_qty > 0
        has_dn = pos.dn_qty > 0
        is_spread = has_up and has_dn

        if has_up:
            avg_up = pos.up_cost / pos.up_qty if pos.up_qty > 0 else 0
            pnl_up = pos.profit_if_up()
            pnl_color = "green" if pnl_up >= 0 else "red"
            strategy = "SPREAD" if is_spread else "DIRECTIONAL"
            pos_table.add_row(
                short_id, Text("UP", style="green"),
                f"{pos.up_qty:.0f}", f"${avg_up:.2f}",
                Text(f"${pnl_up:+.2f}", style=pnl_color),
                Text(strategy, style="magenta" if is_spread else "cyan"),
            )
        if has_dn:
            avg_dn = pos.dn_cost / pos.dn_qty if pos.dn_qty > 0 else 0
            pnl_dn = pos.profit_if_down()
            pnl_color = "green" if pnl_dn >= 0 else "red"
            strategy = "SPREAD" if is_spread else "DIRECTIONAL"
            pos_table.add_row(
                short_id if not has_up else "",
                Text("DN", style="red"),
                f"{pos.dn_qty:.0f}", f"${avg_dn:.2f}",
                Text(f"${pnl_dn:+.2f}", style=pnl_color),
                Text(strategy, style="magenta" if is_spread else "cyan"),
            )

    layout["positions"].update(Panel(pos_table))

    # -- Recent Activity --
    activity_table = Table(
        title="RECENT ACTIVITY", expand=True, show_lines=False,
        title_style="bold white",
    )
    activity_table.add_column("Time", style="dim", width=8)
    activity_table.add_column("Type", width=12)
    activity_table.add_column("Asset", width=6)
    activity_table.add_column("Detail", ratio=1)
    activity_table.add_column("PnL", justify="right", width=10)

    type_styles = {
        "SPREAD": "magenta",
        "DIRECTIONAL": "cyan",
        "SETTLE": "yellow",
        "STOP_LOSS": "red",
        "EARLY_EXIT": "green",
    }

    for event in list(bot._activity_log)[-8:]:
        ts = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        style = type_styles.get(event.event_type, "white")
        pnl_str = ""
        if event.pnl is not None:
            pnl_color = "green" if event.pnl >= 0 else "red"
            pnl_str = f"[{pnl_color}]${event.pnl:+.2f}[/{pnl_color}]"

        activity_table.add_row(
            ts,
            Text(event.event_type, style=style),
            event.asset,
            event.detail,
            Text.from_markup(pnl_str) if pnl_str else Text(""),
        )

    layout["activity"].update(Panel(activity_table))

    # -- Risk --
    halted = bot.risk_manager.is_halted()
    cb_status = "[red]HALTED[/]" if halted else "[green]OK[/]"
    max_loss = bot.risk_manager.starting_bankroll * bot.cfg.max_daily_drawdown_pct
    drawdown_pct = (abs(daily_pnl) / bot.risk_manager.starting_bankroll * 100) if daily_pnl < 0 else 0.0
    max_dd_pct = bot.cfg.max_daily_drawdown_pct * 100

    # Active windows with remaining time
    now = int(time.time())
    window_parts = []
    for m in bot.active_markets:
        if m.is_active(now):
            remaining = m.remaining(now)
            window_parts.append(f"{m.asset} {format_elapsed(remaining)}")
    windows_str = " | ".join(window_parts) if window_parts else "none"

    risk_text = Text.from_markup(
        f"[bold]RISK[/]  Circuit Breaker: {cb_status}  |  "
        f"Drawdown: {drawdown_pct:.1f}% / {max_dd_pct:.1f}% max (${max_loss:,.2f})  |  "
        f"Windows: {windows_str}"
    )
    layout["risk"].update(Panel(risk_text, style="green"))

    return layout

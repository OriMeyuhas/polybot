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
    event_type: str  # LADDER, FILL, SETTLE, STOP_LOSS, EARLY_EXIT, CANCEL
    asset: str
    detail: str
    pnl: float | None = None


def build_display(bot) -> Layout:
    """Build the rich terminal layout from live bot state."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="spot", size=3),
        Layout(name="ladders", ratio=2),
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
    ladder_count = len(bot.ladder_manager.ladders)
    status = "[red]HALTED[/]" if bot.risk_manager.is_halted() else "[green]ACTIVE[/]"

    header_text = Text.from_markup(
        f"[bold cyan]POLYBOT LADDER ENGINE[/] {mode_badge}  "
        f"Uptime: {uptime}  |  "
        f"Bankroll: [bold]${bankroll:,.2f}[/]  |  "
        f"PnL: [{pnl_color}]${daily_pnl:+,.2f} ({pnl_pct:+.2f}%)[/{pnl_color}]  |  "
        f"Fills: {bot._trade_count}  |  "
        f"Ladders: {ladder_count}  |  "
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

    # -- Active Ladders --
    ladder_table = Table(
        title="ACTIVE LADDERS", expand=True, show_lines=False,
        title_style="bold white",
    )
    ladder_table.add_column("Market", style="dim", width=16)
    ladder_table.add_column("Side", width=4)
    ladder_table.add_column("Resting", justify="right", width=7)
    ladder_table.add_column("Filled", justify="right", width=7)
    ladder_table.add_column("VWAP", justify="right", width=7)
    ladder_table.add_column("Combined", justify="right", width=9)
    ladder_table.add_column("Imbal", justify="right", width=7)

    for mid in bot.ladder_manager.ladders:
        stats = bot.ladder_manager.get_ladder_stats(mid)
        short_id = mid.split("_")[-1] if "_" in mid else mid[-8:]

        combined = stats["combined_vwap"]
        combined_color = "green" if 0 < combined < 1.0 else "red" if combined > 0 else "dim"
        imbal = stats["imbalance"]
        imbal_color = "green" if imbal < 0.30 else "yellow" if imbal < 0.60 else "red"

        ladder_table.add_row(
            short_id, Text("UP", style="green"),
            str(stats["up_resting"]),
            f"{stats['up_filled']:.0f}",
            f"${stats['up_vwap']:.2f}" if stats["up_vwap"] > 0 else "--",
            Text(f"${combined:.3f}" if combined > 0 else "--", style=combined_color),
            Text(f"{imbal:.0%}", style=imbal_color),
        )
        ladder_table.add_row(
            "", Text("DN", style="red"),
            str(stats["dn_resting"]),
            f"{stats['dn_filled']:.0f}",
            f"${stats['dn_vwap']:.2f}" if stats["dn_vwap"] > 0 else "--",
            "", "",
        )

    layout["ladders"].update(Panel(ladder_table))

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
        "LADDER": "blue",
        "FILL": "magenta",
        "SETTLE": "yellow",
        "STOP_LOSS": "red",
        "EARLY_EXIT": "green",
        "CANCEL": "dim",
        "IMBALANCE": "red",
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

    now = int(time.time())
    window_parts = []
    for m in bot.active_markets:
        if m.is_active(now):
            remaining = m.remaining(now)
            tf = f"{m.timeframe_sec // 60}m" if m.timeframe_sec < 3600 else f"{m.timeframe_sec // 3600}h"
            window_parts.append(f"{m.asset}/{tf} {format_elapsed(remaining)}")
    windows_str = " | ".join(window_parts) if window_parts else "none"

    risk_text = Text.from_markup(
        f"[bold]RISK[/]  Circuit Breaker: {cb_status}  |  "
        f"Drawdown: {drawdown_pct:.1f}% / {max_dd_pct:.1f}% max (${max_loss:,.2f})  |  "
        f"Windows: {windows_str}"
    )
    layout["risk"].update(Panel(risk_text, style="green"))

    return layout

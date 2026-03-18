import time

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from polybot.config import TrackerConfig
from polybot.tracker.state import TrackerState
from polybot.utils.time_utils import format_duration


def build_display(cfg: TrackerConfig, state: TrackerState, start_time: float) -> Layout:
    """Build a minimal Rich status panel."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="spot", size=3),
        Layout(name="status", size=5),
    )

    # Header
    uptime = format_duration(time.time() - start_time)
    wallet_short = f"{cfg.tracked_wallet[:6]}...{cfg.tracked_wallet[-4:]}"
    trade_count = sum(len(t) for t in state.whale_trades.values())
    header_text = Text.from_markup(
        f"[bold cyan]0x8dxd TRACKER — Modular Pipeline[/]\n"
        f"Session: {state.session_id} | Tracking: {wallet_short} | "
        f"Uptime: {uptime} | Trades: {trade_count}"
    )
    layout["header"].update(Panel(header_text, style="bold"))

    # Spot prices
    spot_parts = []
    for asset in cfg.assets:
        p = state.spot_buffer.get_price_now(asset)
        if p > 0:
            spot_parts.append(f"[bold]{asset}:[/] ${p:,.2f}")
        else:
            spot_parts.append(f"[dim]{asset}:[/] --")
    spot_text = "   ".join(spot_parts)
    layout["spot"].update(Panel(Text.from_markup(f"[bold]SPOT PRICES[/]  {spot_text}"), style="blue"))

    # Status
    active = len(state.active_markets)
    status_text = Text.from_markup(
        f"[bold]STATUS[/]\n"
        f"Active markets: {active} | "
        f"Dedup buffer: {len(state.seen_trade_keys)}/200"
    )
    layout["status"].update(Panel(status_text, style="green"))

    return layout

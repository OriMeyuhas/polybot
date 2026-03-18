import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import websockets

from polybot.config import TrackerConfig
from polybot.tracker.state import TrackerState
from polybot.tracker.csv_writer import TrackerCSVWriter

log = logging.getLogger(__name__)


async def run_spot_recorder(
    cfg: TrackerConfig, state: TrackerState, writer: TrackerCSVWriter
) -> None:
    """Connect to Binance WS for real-time spot prices and periodically write to CSV."""

    streams = [f"{a.lower()}usdt@ticker" for a in cfg.assets]
    ws_url = f"{cfg.binance_ws_url}/{'/'.join(streams)}"

    async def _ws_listener() -> None:
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    log.info("Binance WS connected: %s", ws_url)
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        symbol = data["s"].replace("USDT", "")
                        price = float(data["c"])
                        state.spot_buffer.record(symbol, price)
            except Exception as exc:
                log.warning("Binance WS error (%s), reconnecting in 5s ...", exc)
                await asyncio.sleep(5)

    async def _csv_writer_loop() -> None:
        while True:
            await asyncio.sleep(cfg.spot_record_interval_sec)
            now_str = datetime.now(tz=timezone.utc).isoformat()
            for asset in cfg.assets:
                price_now = state.spot_buffer.get_price_now(asset)
                if price_now == 0:
                    continue
                price_1m = state.spot_buffer.get_price_at(asset, 60)
                delta_1m = (
                    ((price_now - price_1m) / price_1m * 100)
                    if price_1m > 0
                    else 0.0
                )
                writer.write_spot(
                    {
                        "timestamp": now_str,
                        "asset": asset,
                        "price": price_now,
                        "price_1m_ago": price_1m,
                        "delta_1m_pct": round(delta_1m, 4),
                    }
                )

    await asyncio.gather(_ws_listener(), _csv_writer_loop())

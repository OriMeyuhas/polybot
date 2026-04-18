"""Unified data recorder -- captures 6 streams to JSONL files for post-trade analysis.

Streams:
  1. price_log         -- every Binance/Chainlink price tick (throttled to 1/sec per asset)
  2. book_log          -- every Polymarket order book update (full depth)
  3. order_log         -- every order we post, reprice, cancel, fill
  4. trade_log         -- every Polymarket trade on our active markets
  5. strategy_log      -- bot model state per active market (every ~5s)
  6. market_event_log  -- market lifecycle: discovered, activated, settled, dropped

All writes are append-only, line-buffered JSONL. Daily file rotation based on UTC date.
Failures are silently swallowed -- logging must NEVER crash the bot.
"""

import json
import logging
import pathlib
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Throttle price logging to max 1 per second per asset (Binance sends ~5/sec)
_PRICE_THROTTLE_SEC = 1.0


class DataRecorder:
    """Append-only JSONL recorder with daily file rotation."""

    def __init__(self, data_dir: pathlib.Path | str = "data"):
        self._data_dir = pathlib.Path(data_dir)
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass  # will fail silently on writes
        self._handles: dict[str, object] = {}  # stream_name -> file handle
        self._current_date: dict[str, str] = {}  # stream_name -> YYYY-MM-DD
        self._last_price_ts: dict[str, float] = {}  # asset -> last logged ts

    def _get_handle(self, stream: str, ts: float):
        """Get or rotate file handle for a stream based on date."""
        try:
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None
        if self._current_date.get(stream) != date_str:
            # Close old handle
            old = self._handles.pop(stream, None)
            if old:
                try:
                    old.close()
                except Exception:
                    pass
            self._current_date[stream] = date_str
            path = self._data_dir / f"{stream}_{date_str}.jsonl"
            try:
                self._handles[stream] = open(path, "a", buffering=1)  # line-buffered
            except Exception as e:
                logger.debug("Failed to open %s: %s", path, e)
                return None
        return self._handles.get(stream)

    def _append(self, stream: str, record: dict, ts: float | None = None):
        """Append a JSON record to a stream file. Never raises."""
        try:
            t = ts or record.get("ts", time.time())
            fh = self._get_handle(stream, t)
            if fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass  # logging must never crash the bot

    # --- Stream 1: Price ticks ---

    def log_price(self, ts: float, asset: str, price: float, source: str):
        """Log a price tick. Throttled to 1/sec per asset."""
        last = self._last_price_ts.get(asset, 0)
        if ts - last < _PRICE_THROTTLE_SEC:
            return
        self._last_price_ts[asset] = ts
        self._append("price_log", {
            "ts": round(ts, 3),
            "asset": asset,
            "price": price,
            "source": source,
        }, ts)

    # --- Stream 2: Order book updates ---

    def log_book_update(self, ts: float, token_id: str, event_type: str, raw_msg: dict):
        """Log raw order book message from Polymarket WS."""
        self._append("book_log", {
            "ts": round(ts, 3),
            "token_id": token_id[:20] if token_id else "",
            "event_type": event_type,
            "data": raw_msg,
        }, ts)

    # --- Stream 3: Our order lifecycle ---

    def log_order(self, ts: float, event: str, market_id: str, side: str,
                  price: float, size: float, order_id: str = "", reason: str = "",
                  # Gate-decision context — only included on POST events
                  gate_fired: bool | None = None,
                  gate_reason: str | None = None,
                  book_mid: float | None = None,
                  fv_price: float | None = None,
                  fv_certainty: float | None = None,
                  spread: float | None = None,
                  origin: str | None = None,
                  # Reprice-path telemetry (cycle 28): persisted gate state, never re-evaluated live
                  gate_persisted: bool | None = None,
                  gate_reevaluated: bool | None = None):
        """Log an order lifecycle event (post, reprice, cancel, fill).

        Gate context fields (gate_fired, gate_reason, book_mid, fv_price,
        fv_certainty, spread, origin) are written only when event == 'post'
        and gate_fired is not None.  Older log entries without these fields
        will not break analyzers that use .get() with defaults.

        Reprice-path orders use gate_persisted/gate_reevaluated instead of
        gate_fired to distinguish persisted decisions from live evaluations.
        When gate_persisted is not None, it is written instead of gate_fired.
        """
        record: dict = {
            "ts": round(ts, 3),
            "event": event,
            "market_id": market_id,
            "side": side,
            "price": price,
            "size": size,
            "order_id": order_id[:16] if order_id else "",
            "reason": reason,
        }
        if event == "post":
            if gate_persisted is not None:
                # Reprice-origin: emit persisted decision, not a live gate fire
                record["gate_persisted"] = gate_persisted
                record["gate_reevaluated"] = False if gate_reevaluated is None else gate_reevaluated
                record["gate_reason"] = gate_reason if gate_reason is not None else "no_eval"
                record["book_mid"] = book_mid
                record["fv_price"] = fv_price
                record["fv_certainty"] = fv_certainty
                record["spread"] = spread
                record["origin"] = origin if origin is not None else "reprice"
            elif gate_fired is not None:
                # Initial-post: live gate evaluation result
                record["gate_fired"] = gate_fired
                record["gate_reason"] = gate_reason if gate_reason is not None else "no_eval"
                record["book_mid"] = book_mid
                record["fv_price"] = fv_price
                record["fv_certainty"] = fv_certainty
                record["spread"] = spread
                record["origin"] = origin if origin is not None else "initial_post"
        self._append("order_log", record, ts)

    # --- Stream 4: Polymarket trades ---

    def log_trade(self, ts: float, token_id: str, side: str, price: float, size: float = 0):
        """Log a trade observed on Polymarket (from WS last_trade_price)."""
        self._append("trade_log", {
            "ts": round(ts, 3),
            "token_id": token_id[:20] if token_id else "",
            "side": side,
            "price": price,
            "size": size,
        }, ts)

    # --- Stream 5: Strategy state ---

    def log_strategy_state(self, ts: float, market_id: str, asset: str, data_dict: dict):
        """Log the bot's internal model state for a market."""
        self._append("strategy_log", {
            "ts": round(ts, 3),
            "market_id": market_id,
            "asset": asset,
            "data": data_dict,
        }, ts)

    # --- Stream 6: Market lifecycle events ---

    def log_market_event(self, ts: float, event: str, market_id: str, asset: str,
                         timeframe_sec: int, metadata: dict | None = None):
        """Log a market lifecycle event (discovered, activated, settled, dropped)."""
        self._append("market_event_log", {
            "ts": round(ts, 3),
            "event": event,
            "market_id": market_id,
            "asset": asset,
            "timeframe_sec": timeframe_sec,
            "metadata": metadata or {},
        }, ts)

    def close(self):
        """Flush and close all file handles."""
        for fh in self._handles.values():
            try:
                fh.close()
            except Exception:
                pass
        self._handles.clear()

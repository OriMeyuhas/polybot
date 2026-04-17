"""GUI state holder — mutable state dict with broadcast trigger."""

import asyncio
import copy
import logging
from decimal import Decimal
from typing import Any, Callable

logger = logging.getLogger(__name__)

_INITIAL_STATE = {
    "mode": "dry_run",
    "running": False,
    "connected": False,
    "heartbeat_healthy": True,
    "cancel_only_mode": False,
    "total_pnl": 0.0,
    "realized_pnl": 0.0,
    "unrealized_pnl": 0.0,
    "trade_count": 0,
    "position_count": 0,
    "pairs_completed": 0,
    "avg_pair_cost": 0.0,
    "imbalance_ratio": 0.0,
    "runtime_sec": 0,
    "markets_active": 0,
    "win_rate": 0.0,
    "prices": {},
    "binance_prices": {},
    "spots": {},
    "active_markets": [],
    "activity_feed": [],
    "trades": [],
    "pending_settlements": [],
    "usdc_balance": 0.0,
    "wallet": None,
    "price_feed_stale": False,
    "stale_order_alert": "",
}


def _serialize_value(obj: Any) -> Any:
    """Recursively convert Decimals to floats for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _serialize_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_value(item) for item in obj]
    return obj


class GuiStateHolder:
    """Thread-safe state holder with optional async broadcast."""

    def __init__(self):
        self._data: dict[str, Any] = copy.deepcopy(_INITIAL_STATE)
        self._broadcast_fn: Callable | None = None
        self._augmenter: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def set_broadcast(self, fn: Callable) -> None:
        self._broadcast_fn = fn

    def set_augmenter(self, fn: Callable[[dict[str, Any]], dict[str, Any]] | None) -> None:
        """Register a callable that adds extra fields to serialized state.

        Called on every serialize() with the already-serialized dict; should
        return a dict of extra fields to merge in. Used to surface info that
        lives outside the in-memory state (e.g. .env configured_mode)."""
        self._augmenter = fn

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            self._data[key] = value

    def replace(self, snapshot: dict[str, Any]) -> None:
        """Replace the entire state dict with a new snapshot and broadcast."""
        self._data = snapshot
        if self._broadcast_fn:
            asyncio.ensure_future(self._broadcast_fn())

    def get(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def serialize(self) -> dict[str, Any]:
        out = _serialize_value(copy.deepcopy(self._data))
        if self._augmenter is not None:
            try:
                extras = self._augmenter(out)
                if extras:
                    out.update(extras)
            except Exception as exc:
                logger.debug("state_augmenter_error: %s", exc)
        return out

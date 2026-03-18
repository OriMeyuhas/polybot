import time
from collections import deque
from dataclasses import dataclass, field


class SpotBuffer:
    """Per-asset ring buffer storing (timestamp, price) tuples. 5 min of history."""

    def __init__(self):
        self._buffers: dict[str, deque] = {}

    def record(self, asset: str, price: float) -> None:
        if asset not in self._buffers:
            self._buffers[asset] = deque(maxlen=300)
        self._buffers[asset].append((time.time(), price))

    def get_price_now(self, asset: str) -> float:
        buf = self._buffers.get(asset)
        if not buf:
            return 0.0
        return buf[-1][1]

    def get_price_at(self, asset: str, seconds_ago: float) -> float:
        """Get the price closest to `seconds_ago` seconds in the past."""
        buf = self._buffers.get(asset)
        if not buf:
            return 0.0
        target_time = time.time() - seconds_ago
        # Linear scan (fast for <=300 entries)
        closest = buf[0]
        for entry in buf:
            if abs(entry[0] - target_time) < abs(closest[0] - target_time):
                closest = entry
        return closest[1]


@dataclass
class TrackerState:
    session_id: str
    spot_buffer: SpotBuffer = field(default_factory=SpotBuffer)
    active_markets: dict[str, dict] = field(default_factory=dict)
    whale_trades: dict[str, list] = field(default_factory=dict)
    spot_at_discovery: dict[str, float] = field(default_factory=dict)
    seen_trade_keys: deque = field(default_factory=lambda: deque(maxlen=200))
    market_sides: dict[str, set] = field(default_factory=dict)  # for strategy classification

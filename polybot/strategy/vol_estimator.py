"""Realized volatility estimator from Binance tick data.

Consumes (timestamp, price) pairs, resamples to 1-second bars,
and computes rolling annualized volatility from log returns.

Adapted from polytrader's vol_estimator.py — no external dependencies.
"""

import math
from collections import deque
from decimal import Decimal

SECONDS_PER_YEAR = 365.25 * 24 * 3600
MAX_BARS = 1200  # 20 minutes of 1-second bars


class VolEstimator:
    """Rolling realized volatility from 1-second price bars."""

    def __init__(self, min_samples: int = 30, fallback_vol_annual: float = 0.50):
        self._min_samples = min_samples
        self._fallback_vol_annual = fallback_vol_annual
        self._bars: deque[tuple[float, float]] = deque(maxlen=MAX_BARS)
        self._current_sec: int = 0
        self._current_price: float = 0.0

    @property
    def sample_count(self) -> int:
        return len(self._bars)

    @property
    def is_ready(self) -> bool:
        return len(self._bars) >= self._min_samples

    def push(self, epoch_sec: float, price: Decimal | float) -> None:
        """Record a price tick. Internally resampled to 1-second close bars."""
        p = float(price)
        if p <= 0:
            return
        sec = int(epoch_sec)
        self._current_price = p
        if sec != self._current_sec:
            if self._current_sec > 0:
                self._bars.append((float(self._current_sec), self._current_price))
            self._current_sec = sec

    def vol_annualized(self, window_sec: int = 300) -> float:
        """Return annualized realized volatility over the last `window_sec` seconds.

        Falls back to `fallback_vol_annual` if not enough data.
        """
        bars = self._bars
        n = len(bars)
        if n < self._min_samples:
            return self._fallback_vol_annual

        use_n = min(n, window_sec)
        if use_n < 2:
            return self._fallback_vol_annual

        recent = list(bars)[-use_n:]
        log_returns: list[float] = []
        for i in range(1, len(recent)):
            prev_price = recent[i - 1][1]
            cur_price = recent[i][1]
            if prev_price > 0 and cur_price > 0:
                log_returns.append(math.log(cur_price / prev_price))

        if len(log_returns) < 2:
            return self._fallback_vol_annual

        mean_r = sum(log_returns) / len(log_returns)
        var = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
        std_per_sec = math.sqrt(var)
        return std_per_sec * math.sqrt(SECONDS_PER_YEAR)

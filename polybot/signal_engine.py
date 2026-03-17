"""Signal engine: detects directional and spread capture opportunities."""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.types import MarketWindow, Opportunity, Side, StrategyType

logger = logging.getLogger(__name__)


class SignalEngine:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    def check_directional(
        self,
        market: MarketWindow,
        spot_delta: float,
        best_asks: dict[str, float],
        now_epoch: int,
    ) -> Opportunity | None:
        elapsed = market.elapsed(now_epoch)
        remaining = market.remaining(now_epoch)

        if elapsed < self.cfg.window_min_elapsed_sec:
            return None
        if remaining < self.cfg.no_trade_final_sec:
            return None
        if abs(spot_delta) < self.cfg.min_directional_move:
            return None

        if spot_delta > 0:
            side = Side.UP
            price = best_asks.get("UP", 0.0)
        else:
            side = Side.DOWN
            price = best_asks.get("DOWN", 0.0)

        if price > self.cfg.max_directional_price:
            return None
        if price < self.cfg.min_directional_price:
            return None

        return Opportunity(
            strategy=StrategyType.DIRECTIONAL,
            market_id=market.market_id,
            side=side,
            price=price,
            edge=1.0 - price,
            confidence=abs(spot_delta),
        )

    def check_spread(
        self,
        market: MarketWindow,
        best_asks: dict[str, float],
        now_epoch: int,
    ) -> Opportunity | None:
        remaining = market.remaining(now_epoch)
        if remaining < self.cfg.no_trade_final_sec:
            return None

        up_price = best_asks.get("UP", 1.0)
        dn_price = best_asks.get("DOWN", 1.0)
        t = up_price + dn_price
        edge = 1.0 - t

        if edge < self.cfg.min_spread_edge:
            return None

        return Opportunity(
            strategy=StrategyType.SPREAD,
            market_id=market.market_id,
            up_price=up_price,
            dn_price=dn_price,
            edge=edge,
        )

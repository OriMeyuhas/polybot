"""Signal engine: detects directional and spread capture opportunities."""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.types import MarketWindow, Opportunity, Position, Side, StrategyType

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

        # Spread can enter early — after spread_min_elapsed_pct of the window
        elapsed = market.elapsed(now_epoch)
        min_elapsed = int(market.timeframe_sec * self.cfg.spread_min_elapsed_pct)
        if elapsed < min_elapsed:
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

    def check_early_exit(
        self,
        market: MarketWindow,
        pos: Position,
        best_asks: dict[str, float],
        now_epoch: int,
    ) -> Side | None:
        """Check if a spread position should be exited early for profit.

        When one side of a spread becomes very likely to win (price near 1.0),
        the losing side can be sold at a profit relative to its cost.
        Returns the side to EXIT (sell), or None.
        """
        if not (pos.up_qty > 0 and pos.dn_qty > 0):
            return None  # only for spread positions

        # Check if either side's current value exceeds cost by threshold
        # "Value" of a side = qty * (1 - best_ask), i.e. what you'd get if it won
        # But for early exit we look at the *other* side's best ask as a sell price
        # If UP is winning, DOWN tokens become cheap -> sell UP at high price
        up_ask = best_asks.get("UP", 1.0)
        dn_ask = best_asks.get("DOWN", 1.0)

        avg_up = pos.up_cost / pos.up_qty if pos.up_qty > 0 else 1.0
        avg_dn = pos.dn_cost / pos.dn_qty if pos.dn_qty > 0 else 1.0

        # If UP side appreciated: current ask is much higher than our avg entry
        # That means we can sell UP tokens at the current bid (~ask) for profit
        up_gain_pct = (up_ask - avg_up) / avg_up if avg_up > 0 else 0.0
        dn_gain_pct = (dn_ask - avg_dn) / avg_dn if avg_dn > 0 else 0.0

        threshold = self.cfg.early_exit_profit_pct

        # Exit the side that appreciated most
        if up_gain_pct >= threshold and up_gain_pct > dn_gain_pct:
            return Side.UP
        if dn_gain_pct >= threshold and dn_gain_pct > up_gain_pct:
            return Side.DOWN

        return None

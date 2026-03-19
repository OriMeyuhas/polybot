"""Order book builder — snapshot + deltas from Market WebSocket."""
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    asset_id: str
    market: str
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    tick_size: str = "0.01"
    last_trade_price: Decimal | None = None
    last_trade_side: str | None = None
    _last_update: float = 0
    sequence_ok: bool = True

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def is_stale(self, threshold_sec: float) -> bool:
        """Return True if the book has not been updated within threshold_sec seconds."""
        return (time.time() - self._last_update) > threshold_sec


def _parse_levels(items: list[dict]) -> list[PriceLevel]:
    levels: list[PriceLevel] = []
    for item in items:
        try:
            price = Decimal(str(item.get("price", 0)))
            size = Decimal(str(item.get("size", 0)))
            if size > 0:
                levels.append(PriceLevel(price=price, size=size))
        except (ValueError, TypeError):
            continue
    return levels


def apply_book_snapshot(book: OrderBook, msg: dict[str, Any], ts: float | None = None) -> None:
    if ts is None:
        ts = time.time()
    book.bids = _parse_levels(msg.get("bids", []))
    book.asks = _parse_levels(msg.get("asks", []))
    book.bids.sort(key=lambda lvl: lvl.price, reverse=True)
    book.asks.sort(key=lambda lvl: lvl.price)
    if "asset_id" in msg:
        book.asset_id = str(msg["asset_id"])
    if "market" in msg:
        book.market = str(msg["market"])
    book._last_update = ts
    book.sequence_ok = True


def apply_price_change(book: OrderBook, changes: list[dict], ts: float) -> None:
    for ch in changes:
        price = Decimal(str(ch.get("price", 0)))
        size = Decimal(str(ch.get("size", 0)))
        side = (ch.get("side") or "").upper()
        if side == "BUY":
            book.bids = [lvl for lvl in book.bids if lvl.price != price]
            if size > 0:
                book.bids.append(PriceLevel(price=price, size=size))
                book.bids.sort(key=lambda lvl: lvl.price, reverse=True)
        elif side == "SELL":
            book.asks = [lvl for lvl in book.asks if lvl.price != price]
            if size > 0:
                book.asks.append(PriceLevel(price=price, size=size))
                book.asks.sort(key=lambda lvl: lvl.price)
    book._last_update = ts


def apply_tick_size_change(book: OrderBook, msg: dict[str, Any]) -> None:
    book.tick_size = str(msg.get("new_tick_size", book.tick_size))


def apply_last_trade(book: OrderBook, msg: dict[str, Any]) -> None:
    price = Decimal(str(msg.get("price", 0)))
    side = str(msg.get("side", ""))
    if price == Decimal("0.5") and side == "":
        return
    book.last_trade_price = price
    book.last_trade_side = side
    book._last_update = time.time()

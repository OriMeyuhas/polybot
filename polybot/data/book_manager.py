"""Multi-market order book manager — tracks books for multiple token IDs."""
import time

from polybot.data.book import (
    OrderBook,
    apply_book_snapshot,
    apply_last_trade,
    apply_price_change,
    apply_tick_size_change,
)


class BookManager:
    """Manages OrderBook instances keyed by token/asset ID."""

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def get_book(self, token_id: str) -> OrderBook | None:
        return self._books.get(token_id)

    def update_assets(self, token_ids: list[str]) -> None:
        """Add books for new token IDs; preserve existing ones."""
        for tid in token_ids:
            if tid not in self._books:
                self._books[tid] = OrderBook(asset_id=tid, market="")

    def is_stale(self, token_id: str, threshold_sec: float) -> bool:
        """Return True if the book for token_id has never been updated or
        if the time since its last update exceeds threshold_sec."""
        book = self._books.get(token_id)
        if book is None:
            return True
        if book._last_update == 0:
            return True
        return (time.time() - book._last_update) > threshold_sec

    def process_message(self, msg: dict | list) -> None:
        if isinstance(msg, list):
            for m in msg:
                if isinstance(m, dict):
                    self.process_message(m)
            return

        event_type = msg.get("event_type", "")
        raw_ts = msg.get("timestamp")
        if raw_ts is not None:
            ts = float(raw_ts)
            if ts > 1e12:
                ts /= 1000.0
        else:
            ts = time.time()

        if event_type == "book":
            aid = str(msg.get("asset_id", ""))
            if aid in self._books:
                apply_book_snapshot(self._books[aid], msg, ts)

        elif event_type == "price_change":
            applied: set[str] = set()
            for ch in msg.get("price_changes", []):
                aid = str(ch.get("asset_id", ""))
                # Fall back to top-level asset_id if per-change id is absent
                if not aid:
                    aid = str(msg.get("asset_id", ""))
                if aid in self._books and aid not in applied:
                    apply_price_change(self._books[aid], msg.get("price_changes", []), ts)
                    applied.add(aid)

        elif event_type == "tick_size_change":
            aid = str(msg.get("asset_id", ""))
            if aid in self._books:
                apply_tick_size_change(self._books[aid], msg)

        elif event_type == "last_trade_price":
            aid = str(msg.get("asset_id", ""))
            if aid in self._books:
                apply_last_trade(self._books[aid], msg)

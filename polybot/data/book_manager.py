"""Multi-market order book manager — tracks books for multiple token IDs."""
import logging
import time

from polybot.data.book import (
    OrderBook,
    apply_book_snapshot,
    apply_last_trade,
    apply_price_change,
    apply_tick_size_change,
)

logger = logging.getLogger(__name__)


class BookManager:
    """Manages OrderBook instances keyed by token/asset ID."""

    def __init__(self, data_recorder=None) -> None:
        self._books: dict[str, OrderBook] = {}
        self._data_recorder = data_recorder

    def get_book(self, token_id: str) -> OrderBook | None:
        return self._books.get(token_id)

    def set_active_tokens(self, token_ids: list[str]) -> None:
        """Replace the active token set: add new books, remove stale ones."""
        new_set = set(token_ids)
        # Add new
        for tid in token_ids:
            if tid not in self._books:
                self._books[tid] = OrderBook(asset_id=tid, market="")
        # Remove stale
        stale = set(self._books.keys()) - new_set
        for tid in stale:
            del self._books[tid]

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

        # Log book updates to data recorder
        if self._data_recorder and event_type in ("book", "price_change", "last_trade_price"):
            try:
                aid = str(msg.get("asset_id", ""))
                self._data_recorder.log_book_update(ts, aid, event_type, msg)
            except Exception:
                pass

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
            # Also log as a separate trade event
            if self._data_recorder:
                try:
                    side = (msg.get("side") or "").upper()
                    price = float(msg.get("price", 0))
                    size = float(msg.get("size", 0))
                    self._data_recorder.log_trade(ts, aid, side, price, size)
                except Exception:
                    pass

    async def seed_book_http(self, token_id: str, clob_host: str) -> bool:
        """Fetch initial book snapshot via HTTP for a token that has no data yet."""
        book = self._books.get(token_id)
        if book is not None and book.asks:
            return True  # already have data
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{clob_host}/book",
                    params={"token_id": token_id},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if token_id not in self._books:
                        self._books[token_id] = OrderBook(asset_id=token_id, market="")
                    apply_book_snapshot(self._books[token_id], data, time.time())
                    logger.info("HTTP book seed: %s — %d asks, %d bids",
                                token_id[:16],
                                len(self._books[token_id].asks),
                                len(self._books[token_id].bids))
                    return True
        except Exception as e:
            logger.debug("HTTP book seed failed for %s: %s", token_id[:16], e)
        return False

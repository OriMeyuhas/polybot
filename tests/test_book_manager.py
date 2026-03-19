from decimal import Decimal
from polybot.data.book_manager import BookManager


def test_register_and_get_book():
    bm = BookManager()
    bm.update_assets(["token_abc", "token_def"])
    book = bm.get_book("token_abc")
    assert book is not None
    assert book.best_bid is None  # Empty book


def test_process_book_message():
    bm = BookManager()
    bm.update_assets(["token_abc"])
    bm.process_message({
        "event_type": "book",
        "asset_id": "token_abc",
        "market": "token_abc",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
        "timestamp": "1710850000",
    })
    book = bm.get_book("token_abc")
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.55")


def test_process_price_change():
    """Uses 'price_changes' key (matching actual Polymarket WS format)."""
    bm = BookManager()
    bm.update_assets(["token_abc"])
    bm.process_message({
        "event_type": "book",
        "asset_id": "token_abc",
        "market": "token_abc",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
        "timestamp": "1710850000",
    })
    bm.process_message({
        "event_type": "price_change",
        "asset_id": "token_abc",
        "market": "token_abc",
        "price_changes": [{"price": "0.46", "size": "50", "side": "BUY"}],
        "timestamp": "1710850001",
    })
    assert bm.get_book("token_abc").best_bid == Decimal("0.46")


def test_unknown_asset_ignored():
    bm = BookManager()
    bm.process_message({
        "event_type": "book",
        "asset_id": "unknown",
        "market": "unknown",
        "bids": [],
        "asks": [],
        "timestamp": "1710850000",
    })
    assert bm.get_book("unknown") is None


def test_stale_check():
    bm = BookManager()
    bm.update_assets(["token_abc"])
    assert bm.is_stale("token_abc", 30) is True  # Never updated

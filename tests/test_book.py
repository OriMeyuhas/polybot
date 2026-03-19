import time
from decimal import Decimal
from polybot.data.book import OrderBook, PriceLevel, apply_book_snapshot, apply_price_change


def test_empty_book():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.mid is None
    assert book.spread is None


def test_apply_snapshot():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    now = time.time()
    apply_book_snapshot(book, {
        "bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "200"}],
        "asks": [{"price": "0.55", "size": "100"}, {"price": "0.56", "size": "200"}],
    }, ts=now)
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.55")
    assert book.mid == Decimal("0.50")
    assert book.spread == Decimal("0.10")


def test_apply_price_change_add():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    now = time.time()
    apply_book_snapshot(book, {
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
    }, ts=now)
    apply_price_change(book, [{"price": "0.46", "size": "50", "side": "BUY"}], ts=now)
    assert book.best_bid == Decimal("0.46")


def test_apply_price_change_remove():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    now = time.time()
    apply_book_snapshot(book, {
        "bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "200"}],
        "asks": [{"price": "0.55", "size": "100"}],
    }, ts=now)
    apply_price_change(book, [{"price": "0.45", "size": "0", "side": "BUY"}], ts=now)
    assert book.best_bid == Decimal("0.44")


def test_stale_detection():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    book._last_update = time.time() - 60
    assert book.is_stale(30) is True
    book._last_update = time.time()
    assert book.is_stale(30) is False

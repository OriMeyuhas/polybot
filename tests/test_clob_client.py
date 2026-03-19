from polybot.oms.clob_client import PaperClobClient


def test_paper_post_order_returns_mock_id():
    client = PaperClobClient(book_manager=None)
    result = client.post_order(
        {"order": "signed_data"},
        orderType="GTC",
    )
    assert "orderID" in result
    assert result["orderID"].startswith("paper-")


def test_paper_resting_orders():
    client = PaperClobClient(book_manager=None)
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    orders = client.get_open_orders()
    assert len(orders) == 1


def test_paper_cancel_order():
    client = PaperClobClient(book_manager=None)
    result = client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    oid = result["orderID"]
    client.cancel(oid)
    assert len(client.get_open_orders()) == 0


def test_paper_cancel_all():
    client = PaperClobClient(book_manager=None)
    for i in range(5):
        client.post_order(
            {"order": f"data_{i}", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
            orderType="GTC",
        )
    client.cancel_all()
    assert len(client.get_open_orders()) == 0


def test_paper_tick_fills_buy_when_ask_crosses():
    """Paper fill simulation: buy at 0.45, best ask drops to 0.44 -> fills."""
    from decimal import Decimal
    from polybot.data.book import OrderBook, apply_book_snapshot
    from polybot.data.book_manager import BookManager
    import time

    bm = BookManager()
    bm.update_assets(["tok_a"])
    apply_book_snapshot(bm.get_book("tok_a"), {
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.44", "size": "200"}],
    }, ts=time.time())

    client = PaperClobClient(book_manager=bm)
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    assert len(client.get_open_orders()) == 1

    fills = client.tick()
    assert len(fills) >= 1
    assert len(client.get_open_orders()) == 0


def test_paper_tick_no_fill_when_ask_above():
    """Buy at 0.45, best ask is 0.55 -> no fill."""
    from polybot.data.book import apply_book_snapshot
    from polybot.data.book_manager import BookManager
    import time

    bm = BookManager()
    bm.update_assets(["tok_a"])
    apply_book_snapshot(bm.get_book("tok_a"), {
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.55", "size": "200"}],
    }, ts=time.time())

    client = PaperClobClient(book_manager=bm)
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    fills = client.tick()
    assert len(fills) == 0
    assert len(client.get_open_orders()) == 1

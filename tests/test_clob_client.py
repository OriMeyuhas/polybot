from decimal import Decimal
from unittest.mock import MagicMock

from polybot.oms.clob_client import PaperClobClient
from polybot.oms.order_executor import OrderArgs, BUY, OrderExecutor
from polybot.types import Side


# ---------------------------------------------------------------------------
# Helpers for book-gated fill tests (Fix #49)
# ---------------------------------------------------------------------------

def _make_book(best_bid: float, best_ask: float, has_asks: bool = True):
    """Create a minimal mock OrderBook object matching what _get_real_book_depth reads."""
    book = MagicMock()
    book._last_update = 9e9  # very fresh
    book.bids = [MagicMock(price=Decimal(str(best_bid)), size=Decimal("500"))]
    if has_asks:
        book.asks = [MagicMock(price=Decimal(str(best_ask)), size=Decimal("500"))]
        book.best_ask = Decimal(str(best_ask))
    else:
        book.asks = []
        book.best_ask = None
    book.best_bid = Decimal(str(best_bid))
    return book


def _make_paper_client_with_book(best_bid: float, best_ask: float, has_asks: bool = True) -> PaperClobClient:
    bk = _make_book(best_bid, best_ask, has_asks)
    bm = MagicMock()
    bm.get_book.return_value = bk
    return PaperClobClient(book_manager=bm)


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


def test_paper_tick_fills_buy_when_midpoint_crosses():
    """Paper fill: buy at 0.45, midpoint drops to 0.44 -> fills (near-market = 90% prob)."""
    client = PaperClobClient(book_manager=None)
    client._rng.seed(42)  # deterministic for testing
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    assert len(client.get_open_orders()) == 1

    # Midpoint at 0.50 — no fill
    fills = client.tick({"tok_a": 0.50})
    assert len(fills) == 0
    assert len(client.get_open_orders()) == 1

    # Midpoint drops to 0.44 — within 0.01, 90% fill probability
    # With seed 42, first random() = 0.639..., < 0.90 → fills
    fills = client.tick({"tok_a": 0.44})
    assert len(fills) == 1
    assert len(client.get_open_orders()) == 0


def test_paper_tick_no_fill_when_midpoint_above():
    """Buy at 0.45, midpoint is 0.55 -> no fill."""
    client = PaperClobClient(book_manager=None)
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    fills = client.tick({"tok_a": 0.55})
    assert len(fills) == 0
    assert len(client.get_open_orders()) == 1


def test_paper_tick_no_fill_without_midpoints():
    """No midpoints -> no fills (safe default)."""
    client = PaperClobClient(book_manager=None)
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    fills = client.tick()
    assert len(fills) == 0
    fills = client.tick({})
    assert len(fills) == 0


def test_paper_tick_one_fill_per_token_per_tick():
    """Only one order fills per token per tick (sequential, nearest-market first)."""
    client = PaperClobClient(book_manager=None)
    client._rng.seed(1)  # deterministic
    # Place two orders: one near market (0.45), one deep (0.40)
    client.post_order(
        {"order": "d1", "token_id": "tok_a", "price": "0.45", "size": "10", "side": "BUY"},
        orderType="GTC",
    )
    client.post_order(
        {"order": "d2", "token_id": "tok_a", "price": "0.40", "size": "10", "side": "BUY"},
        orderType="GTC",
    )
    assert len(client.get_open_orders()) == 2

    # Midpoint at 0.44 — 0.45 order is within 0.01 (90% prob), fills first
    # Only 1 per token per tick regardless
    fills = client.tick({"tok_a": 0.44})
    assert len(fills) == 1
    assert len(client.get_open_orders()) == 1

    # Second tick fills the next order (0.40 at distance 0.04 = 20% prob)
    # May or may not fill depending on RNG — that's the realistic behavior
    # Eventually fills after enough ticks (probabilistic)
    filled_second = False
    for _ in range(50):  # 50 attempts at 20% = ~99.99% chance
        fills = client.tick({"tok_a": 0.39})
        if fills:
            filled_second = True
            break
    assert filled_second, "Deep order should eventually fill with enough ticks"
    assert len(client.get_open_orders()) == 0


# -------------------------------------------------------------------
# OrderArgs field completeness tests (live mode compatibility)
# -------------------------------------------------------------------


def test_order_args_has_fee_rate_bps():
    """OrderArgs must have fee_rate_bps for live SDK signing."""
    args = OrderArgs(token_id="abc", price=0.50, size=10.0, side=BUY)
    assert hasattr(args, "fee_rate_bps")


def test_order_args_has_nonce():
    """OrderArgs must have nonce for live SDK signing."""
    args = OrderArgs(token_id="abc", price=0.50, size=10.0, side=BUY)
    assert hasattr(args, "nonce")


def test_order_args_has_expiration():
    """OrderArgs must have expiration for live SDK signing."""
    args = OrderArgs(token_id="abc", price=0.50, size=10.0, side=BUY)
    assert hasattr(args, "expiration")


def test_order_args_has_taker():
    """OrderArgs must have taker for live SDK signing."""
    args = OrderArgs(token_id="abc", price=0.50, size=10.0, side=BUY)
    assert hasattr(args, "taker")


def test_order_args_through_paper_client():
    """Real OrderArgs passes through PaperClobClient.create_order() correctly."""
    client = PaperClobClient(book_manager=None)
    args = OrderArgs(token_id="tok_abc", price=0.55, size=20.0, side=BUY)
    signed = client.create_order(args)
    assert signed["token_id"] == "tok_abc"
    assert signed["price"] == "0.55"
    assert signed["size"] == "20.0"
    assert signed["side"] == BUY


def test_adhoc_order_args_fails_field_check():
    """A minimal 4-field class must NOT pass the fee_rate_bps check."""

    class _AdHocOrderArgs:
        def __init__(self, token_id, price, size, side):
            self.token_id = token_id
            self.price = price
            self.size = size
            self.side = side

    bad = _AdHocOrderArgs("abc", 0.50, 10.0, "BUY")
    assert not hasattr(bad, "fee_rate_bps")


def test_order_args_fallback_has_all_fields():
    """Fallback OrderArgs (no SDK installed) must have all 7 fields."""
    args = OrderArgs(token_id="t", price=0.5, size=1.0, side=BUY)
    for field in ("token_id", "price", "size", "side", "fee_rate_bps",
                  "nonce", "expiration", "taker"):
        assert hasattr(args, field), f"Missing field: {field}"


def test_executor_place_limit_buy_with_paper_client():
    """OrderExecutor.place_limit_buy() works with PaperClobClient and real OrderArgs."""
    from polybot.config import BotConfig
    cfg = BotConfig()
    client = PaperClobClient(book_manager=None)
    executor = OrderExecutor(cfg, client)
    record = executor.place_limit_buy(
        token_id="tok_test",
        price=0.45,
        size=10.0,
        market_id="mkt_1",
        side=Side.UP,
    )
    assert record.order_id.startswith("paper-")
    assert record.market_id == "mkt_1"


def test_executor_place_batch_limit_buys_with_paper_client():
    """OrderExecutor.place_batch_limit_buys() works with PaperClobClient."""
    from polybot.config import BotConfig
    cfg = BotConfig()
    client = PaperClobClient(book_manager=None)
    executor = OrderExecutor(cfg, client)
    orders = [
        {"token_id": "tok_a", "price": 0.40, "size": 5.0, "market_id": "m1", "side": Side.UP},
        {"token_id": "tok_b", "price": 0.35, "size": 5.0, "market_id": "m2", "side": Side.DOWN},
    ]
    results = executor.place_batch_limit_buys(orders)
    assert len(results) == 2
    assert all(r.order_id.startswith("paper-") for r in results)


# ---------------------------------------------------------------------------
# Fix #49 — BUY orders priced above real best_ask should be blocked
# ---------------------------------------------------------------------------

class TestBuyFillAboveBestAsk:
    def test_buy_blocked_when_price_above_best_ask(self):
        """BUY at 0.40 when real best_ask=0.31 is phantom — must be blocked."""
        client = _make_paper_client_with_book(best_bid=0.30, best_ask=0.31)
        can_fill, size = client._get_real_book_depth("tok_a", order_price=0.40, side="BUY")
        assert can_fill is False
        assert size == 0.0

    def test_buy_allowed_when_price_within_tick_of_best_ask(self):
        """BUY at 0.312 when best_ask=0.31 — gap 0.002 < tolerance 0.005 — should be allowed."""
        client = _make_paper_client_with_book(best_bid=0.30, best_ask=0.31)
        can_fill, size = client._get_real_book_depth("tok_a", order_price=0.312, side="BUY")
        assert can_fill is True

    def test_buy_allowed_at_best_ask(self):
        """BUY at exactly best_ask=0.31 — gap 0.0 — should be allowed."""
        client = _make_paper_client_with_book(best_bid=0.30, best_ask=0.31)
        can_fill, size = client._get_real_book_depth("tok_a", order_price=0.31, side="BUY")
        assert can_fill is True

    def test_buy_unchanged_when_no_asks(self):
        """Empty asks side — existing bid logic applies unchanged (no block from best_ask check)."""
        client = _make_paper_client_with_book(best_bid=0.30, best_ask=0.31, has_asks=False)
        # With no asks, we skip the best_ask check; BUY at 0.40 falls to bid logic
        # best_bid=0.30, order_price=0.40 → gap_below_bid = -0.10 (above bid) → within 3c → allows fill
        can_fill, _size = client._get_real_book_depth("tok_a", order_price=0.40, side="BUY")
        assert can_fill is True  # no asks means no block on this path

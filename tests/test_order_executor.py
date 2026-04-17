import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from polybot.order_executor import OrderExecutor, _make_clob_error
from polybot.types import Side, OrderRecord
from polybot.config import BotConfig
from polybot.errors import ClobApiError


@pytest.fixture
def cfg():
    return BotConfig(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        dry_run=False,
    )


@pytest.fixture
def dry_cfg():
    return BotConfig(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        dry_run=True,
    )


@pytest.fixture
def mock_clob():
    client = MagicMock()
    client.create_order.return_value = {"signed": True}
    client.post_order.return_value = {"orderID": "order-123", "status": "matched"}
    client.cancel.return_value = {"cancelled": True}
    client.get_order_book.return_value = MagicMock(
        bids=[MagicMock(price="0.45", size="1000")],
        asks=[MagicMock(price="0.55", size="500")],
    )
    return client


@pytest.fixture
def executor(cfg, mock_clob):
    return OrderExecutor(cfg, clob_client=mock_clob)


@pytest.fixture
def dry_executor(dry_cfg, mock_clob):
    return OrderExecutor(dry_cfg, clob_client=mock_clob)


class TestMakeClobError:
    def test_basic_exception(self):
        err = _make_clob_error(Exception("boom"))
        assert isinstance(err, ClobApiError)
        assert str(err) == "boom"
        assert err.status_code is None
        assert err.retry_after is None
        assert err.cancel_only is False

    def test_429_with_retry_after(self):
        exc = Exception("rate limited")
        exc.response = MagicMock(status_code=429, headers={"Retry-After": "10"})
        err = _make_clob_error(exc)
        assert err.status_code == 429
        assert err.retry_after == 10.0
        assert err.cancel_only is False

    def test_503_cancel_only(self):
        exc = Exception("service unavailable")
        exc.response = MagicMock(status_code=503, headers={})
        err = _make_clob_error(exc)
        assert err.status_code == 503
        assert err.cancel_only is True
        assert err.retry_after is None


class TestPlaceOrder:
    def test_place_limit_buy(self, executor, mock_clob):
        record = executor.place_limit_buy(
            token_id="tok_up",
            price=0.85,
            size=100.0,
            market_id="m1",
            side=Side.UP,
        )
        assert record.order_id == "order-123"
        assert record.status == "matched"
        mock_clob.create_order.assert_called_once()
        mock_clob.post_order.assert_called_once()

    def test_place_limit_buy_raises_on_error(self, executor, mock_clob):
        mock_clob.post_order.side_effect = Exception("API error")
        with pytest.raises(ClobApiError, match="API error"):
            executor.place_limit_buy(
                token_id="tok_up", price=0.85, size=100.0,
                market_id="m1", side=Side.UP,
            )

    def test_place_limit_buy_dry_run(self, dry_executor, mock_clob):
        """OMS executor delegates dry_run to the client, so it still calls create_order/post_order."""
        record = dry_executor.place_limit_buy(
            token_id="tok_up",
            price=0.85,
            size=100.0,
            market_id="m1",
            side=Side.UP,
        )
        assert record.order_id == "order-123"
        assert record.status == "matched"


class TestCancelOrder:
    def test_cancel_order(self, executor, mock_clob):
        result = executor.cancel_order("order-123")
        assert result is True
        mock_clob.cancel.assert_called_once_with("order-123")

    def test_cancel_order_raises_on_error(self, executor, mock_clob):
        mock_clob.cancel.side_effect = Exception("not found")
        with pytest.raises(ClobApiError, match="not found"):
            executor.cancel_order("bad-id")


class TestGetOpenOrders:
    def test_get_open_orders(self, executor, mock_clob):
        mock_clob.get_open_orders.return_value = [{"id": "o1"}]
        result = executor.get_open_orders()
        assert result == [{"id": "o1"}]

    def test_get_open_orders_raises_on_error(self, executor, mock_clob):
        mock_clob.get_open_orders.side_effect = Exception("timeout")
        with pytest.raises(ClobApiError, match="timeout"):
            executor.get_open_orders()


class TestGetBestAsk:
    def test_get_best_ask(self, executor):
        result = executor.get_best_ask("tok_up")
        assert result == 0.55

    def test_get_best_ask_raises_on_error(self, executor, mock_clob):
        mock_clob.get_order_book.side_effect = Exception("network error")
        with pytest.raises(ClobApiError, match="network error"):
            executor.get_best_ask("tok_up")


class TestOrderBook:
    def test_get_best_asks(self, executor, mock_clob):
        bids, asks = executor.get_book_summary("tok_up")
        assert asks[0] == ("0.55", "500")
        assert bids[0] == ("0.45", "1000")

    def test_get_book_depth_at_price(self, executor):
        depth = executor.get_book_depth_at_price("tok_up", 0.60)
        assert depth >= 0


class TestBatchMethods:
    def test_place_batch_limit_buys(self, executor, mock_clob):
        orders = [
            {"token_id": "t1", "price": 0.5, "size": 10, "market_id": "m1", "side": Side.UP},
            {"token_id": "t2", "price": 0.6, "size": 20, "market_id": "m2", "side": Side.DOWN},
        ]
        results = executor.place_batch_limit_buys(orders)
        assert len(results) == 2
        assert all(isinstance(r, OrderRecord) for r in results)
        assert all(r.order_id == "order-123" for r in results)

    def test_place_batch_limit_buys_partial_failure(self, executor, mock_clob):
        call_count = 0
        original_post = mock_clob.post_order.side_effect

        def alternate_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("API error")
            return {"orderID": "order-123", "status": "matched"}

        mock_clob.post_order.side_effect = alternate_post
        orders = [
            {"token_id": "t1", "price": 0.5, "size": 10, "market_id": "m1", "side": Side.UP},
            {"token_id": "t2", "price": 0.6, "size": 20, "market_id": "m2", "side": Side.DOWN},
            {"token_id": "t3", "price": 0.7, "size": 30, "market_id": "m3", "side": Side.UP},
        ]
        results = executor.place_batch_limit_buys(orders)
        assert len(results) == 2  # 1st and 3rd succeed, 2nd fails

    def test_cancel_batch(self, executor, mock_clob):
        cancelled = executor.cancel_batch(["o1", "o2", "o3"])
        assert cancelled == ["o1", "o2", "o3"]

    def test_cancel_batch_partial_failure(self, executor, mock_clob):
        call_count = 0

        def alternate_cancel(oid):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("cancel failed")

        mock_clob.cancel.side_effect = alternate_cancel
        cancelled = executor.cancel_batch(["o1", "o2", "o3"])
        assert cancelled == ["o1", "o3"]

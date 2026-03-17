import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from polybot.order_executor import OrderExecutor
from polybot.types import Side, OrderRecord
from polybot.config import BotConfig


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

    def test_place_limit_buy_handles_error(self, executor, mock_clob):
        mock_clob.post_order.side_effect = Exception("API error")
        record = executor.place_limit_buy(
            token_id="tok_up", price=0.85, size=100.0,
            market_id="m1", side=Side.UP,
        )
        assert record.status == "error"


class TestCancelOrder:
    def test_cancel_order(self, executor, mock_clob):
        result = executor.cancel_order("order-123")
        assert result is True
        mock_clob.cancel.assert_called_once_with("order-123")

    def test_cancel_order_handles_error(self, executor, mock_clob):
        mock_clob.cancel.side_effect = Exception("not found")
        result = executor.cancel_order("bad-id")
        assert result is False


class TestOrderBook:
    def test_get_best_asks(self, executor, mock_clob):
        bids, asks = executor.get_book_summary("tok_up")
        assert asks[0] == ("0.55", "500")
        assert bids[0] == ("0.45", "1000")

    def test_get_book_depth_at_price(self, executor):
        depth = executor.get_book_depth_at_price("tok_up", 0.60)
        assert depth >= 0

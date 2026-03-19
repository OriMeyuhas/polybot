from polybot.config import BotConfig
from polybot.oms.clob_client import PaperClobClient
from polybot.oms.order_executor import OrderExecutor
from polybot.types import Side


def test_place_limit_buy():
    cfg = BotConfig()
    client = PaperClobClient(book_manager=None)
    executor = OrderExecutor(cfg=cfg, clob_client=client)
    record = executor.place_limit_buy(
        token_id="tok_up",
        price=0.45,
        size=50.0,
        market_id="test-market",
        side=Side.UP,
    )
    assert record.order_id != ""
    assert record.price == 0.45
    assert record.size == 50.0


def test_get_open_orders():
    cfg = BotConfig()
    client = PaperClobClient(book_manager=None)
    executor = OrderExecutor(cfg=cfg, clob_client=client)
    executor.place_limit_buy("tok_up", 0.45, 50.0, "test-market", Side.UP)
    orders = executor.get_open_orders()
    assert len(orders) >= 1


def test_cancel_order():
    cfg = BotConfig()
    client = PaperClobClient(book_manager=None)
    executor = OrderExecutor(cfg=cfg, clob_client=client)
    record = executor.place_limit_buy("tok_up", 0.45, 50.0, "test-market", Side.UP)
    assert executor.cancel_order(record.order_id) is True


def test_error_propagation_not_swallowed():
    """Verify ClobApiError propagates up instead of being silently caught."""
    from unittest.mock import MagicMock
    from polybot.errors import ClobApiError
    import pytest

    cfg = BotConfig()
    mock_client = MagicMock()
    mock_client.create_order.side_effect = ClobApiError("rate limited", status_code=429, retry_after=5.0)
    executor = OrderExecutor(cfg=cfg, clob_client=mock_client)

    with pytest.raises(ClobApiError) as exc_info:
        executor.place_limit_buy("tok_up", 0.45, 50.0, "test-market", Side.UP)
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 5.0

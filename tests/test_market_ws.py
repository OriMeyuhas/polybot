from polybot.data.market_ws import MarketWSClient


def test_build_subscribe_message():
    client = MarketWSClient(
        url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_message=lambda msg: None,
    )
    msg = client._build_subscribe_msg(["token_a", "token_b"])
    assert msg["type"] == "market"
    assert set(msg["assets_ids"]) == {"token_a", "token_b"}


def test_backoff_capped():
    client = MarketWSClient(
        url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_message=lambda msg: None,
    )
    client._reconnect_count = 10
    delay = client._backoff_delay()
    assert delay <= 60.0


def test_initial_state():
    client = MarketWSClient(
        url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_message=lambda msg: None,
    )
    assert client.is_connected is False
    assert client._reconnect_count == 0

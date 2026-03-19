from decimal import Decimal
from polybot.data.price_feed import MultiAssetPriceFeed


def test_initial_state():
    feed = MultiAssetPriceFeed(assets=("BTC", "ETH", "SOL", "XRP"))
    assert feed.get_price("BTC") is None
    assert feed.get_price("ETH") is None


def test_set_price():
    feed = MultiAssetPriceFeed(assets=("BTC",))
    feed._update_price("BTC", Decimal("65000.50"))
    assert feed.get_price("BTC") == Decimal("65000.50")


def test_binance_stream_url():
    feed = MultiAssetPriceFeed(assets=("BTC", "ETH"))
    url = feed._build_ws_url()
    assert "stream?streams=" in url
    assert "btcusdt@trade" in url
    assert "ethusdt@trade" in url


def test_coingecko_asset_mapping():
    feed = MultiAssetPriceFeed(
        assets=("BTC", "ETH", "SOL", "XRP"),
        coingecko_ids=("bitcoin", "ethereum", "solana", "ripple"),
    )
    assert feed._coingecko_id("BTC") == "bitcoin"
    assert feed._coingecko_id("XRP") == "ripple"


def test_unknown_asset_returns_none():
    feed = MultiAssetPriceFeed(assets=("BTC",))
    assert feed.get_price("DOGE") is None

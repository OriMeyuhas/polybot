from polybot.data.gamma import MarketInfo, to_market_window


def test_to_market_window_basic():
    info = MarketInfo(
        condition_id="cond_123",
        question="Will BTC go up in the next 15 minutes?",
        slug="btc-updown-15m-2026-03-19",
        clob_token_ids=["token_up", "token_dn"],
        outcomes=["Up", "Down"],
        event_start_iso="2026-03-19T12:00:00Z",
        end_date_iso="2026-03-19T12:15:00Z",
        price_to_beat="65000.00",
        active=True,
        liquidity=50000.0,
    )
    mw = to_market_window(info, asset="BTC")
    assert mw.market_id == "btc-updown-15m-2026-03-19"
    assert mw.condition_id == "cond_123"
    assert mw.asset == "BTC"
    assert mw.up_token_id == "token_up"
    assert mw.dn_token_id == "token_dn"
    assert mw.timeframe_sec == 900  # 15 minutes
    assert mw.close_epoch > mw.open_epoch


def test_to_market_window_reversed_outcomes():
    """If outcomes are ["Down", "Up"], tokens should map correctly."""
    info = MarketInfo(
        condition_id="cond_456",
        question="Will ETH go up?",
        slug="eth-updown-5m-2026-03-19",
        clob_token_ids=["token_dn", "token_up"],
        outcomes=["Down", "Up"],
        event_start_iso="2026-03-19T12:00:00Z",
        end_date_iso="2026-03-19T12:05:00Z",
        price_to_beat="3200.00",
        active=True,
        liquidity=10000.0,
    )
    mw = to_market_window(info, asset="ETH")
    assert mw.up_token_id == "token_up"
    assert mw.dn_token_id == "token_dn"


def test_slug_pattern_matching():
    from polybot.data.gamma import CRYPTO_SLUG_PATTERNS
    assert any("btc" in p for p in CRYPTO_SLUG_PATTERNS)
    assert any("eth" in p for p in CRYPTO_SLUG_PATTERNS)
    assert any("sol" in p for p in CRYPTO_SLUG_PATTERNS)
    assert any("xrp" in p for p in CRYPTO_SLUG_PATTERNS)

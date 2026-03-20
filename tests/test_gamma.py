from polybot.data.gamma import MarketInfo, to_market_window, _detect_asset, _parse_json_field


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


def test_detect_asset_prefix_matching():
    """Asset detection uses slug prefix — no false positives on 'netherlands'."""
    assert _detect_asset("btc-updown-15m-2026") == "BTC"
    assert _detect_asset("eth-updown-5m-12345") == "ETH"
    assert _detect_asset("sol-updown-15m-12345") == "SOL"
    assert _detect_asset("xrp-updown-5m-12345") == "XRP"
    # Should NOT match non-crypto slugs
    assert _detect_asset("will-netherlands-win") is None
    assert _detect_asset("ethereum-classic-something") is None  # no 'eth-' prefix


def test_parse_json_field():
    """Gamma API returns JSON strings for outcomes and clobTokenIds."""
    assert _parse_json_field('["Up", "Down"]') == ["Up", "Down"]
    assert _parse_json_field('["tok1", "tok2"]') == ["tok1", "tok2"]
    assert _parse_json_field(["already", "a", "list"]) == ["already", "a", "list"]
    assert _parse_json_field(None) == []
    assert _parse_json_field("invalid json{", default=["Yes", "No"]) == ["Yes", "No"]


from polybot.data.gamma import parse_slug_timing


def test_parse_slug_timing_epoch():
    """Slug with epoch suffix: btc-updown-5m-1773942300."""
    result = parse_slug_timing("btc-updown-5m-1773942300")
    assert result is not None
    asset, timeframe_sec, open_epoch, close_epoch = result
    assert asset == "BTC"
    assert timeframe_sec == 300
    assert open_epoch == 1773942300
    assert close_epoch == 1773942600  # open + 300


def test_parse_slug_timing_date():
    """Slug with date suffix: btc-updown-15m-2026-03-19."""
    result = parse_slug_timing("btc-updown-15m-2026-03-19")
    assert result is not None
    asset, timeframe_sec, open_epoch, close_epoch = result
    assert asset == "BTC"
    assert timeframe_sec == 900
    assert open_epoch > 0
    assert close_epoch == open_epoch + 900


def test_parse_slug_timing_unknown():
    """Non-matching slug returns None."""
    assert parse_slug_timing("will-trump-win") is None
    assert parse_slug_timing("") is None


def test_parse_slug_timing_1h():
    """1-hour window slug."""
    result = parse_slug_timing("eth-updown-1h-1773942300")
    assert result is not None
    _, timeframe_sec, _, close_epoch = result
    assert timeframe_sec == 3600
    assert close_epoch == 1773942300 + 3600


import logging


def test_discovery_logs_filter_reasons(caplog):
    """Discovery should log why each market is filtered out."""
    from polybot.data.gamma import _filter_market_from_event

    # Market with missing token IDs (clobTokenIds is empty JSON array)
    market = {
        "slug": "btc-updown-5m-1773942300",
        "clobTokenIds": "[]",
        "outcomes": '["Up", "Down"]',
        "endDate": "2026-03-20T12:05:00Z",
        "active": True,
        "liquidityNum": 100.0,
    }
    with caplog.at_level(logging.DEBUG, logger="polybot.data.gamma"):
        result = _filter_market_from_event(market, event={}, patterns=["btc-updown-5m-"], now_epoch=1773942330)
    assert result is None
    assert "token" in caplog.text.lower() or "skip" in caplog.text.lower()

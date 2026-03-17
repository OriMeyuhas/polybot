import pytest
from polybot.market_discovery import parse_market_to_window, is_crypto_updown_market


def test_is_crypto_updown_market_btc_15m():
    market = {
        "condition_id": "0xabc",
        "question": "Will Bitcoin go up or down in the next 15 minutes?",
        "tokens": [
            {"token_id": "tok_up", "outcome": "Up"},
            {"token_id": "tok_dn", "outcome": "Down"},
        ],
        "end_date_iso": "2026-03-17T15:00:00Z",
        "game_start_time": "2026-03-17T14:45:00Z",
    }
    assert is_crypto_updown_market(market, assets=("BTC", "ETH", "SOL", "XRP"))


def test_is_not_crypto_updown_for_political():
    market = {
        "condition_id": "0xdef",
        "question": "Will Trump win the 2028 election?",
        "tokens": [
            {"token_id": "tok_yes", "outcome": "Yes"},
            {"token_id": "tok_no", "outcome": "No"},
        ],
    }
    assert not is_crypto_updown_market(market, assets=("BTC", "ETH", "SOL", "XRP"))


def test_parse_market_to_window():
    market = {
        "condition_id": "0xabc",
        "question": "Will Bitcoin go up or down?",
        "tokens": [
            {"token_id": "tok_up", "outcome": "Up"},
            {"token_id": "tok_dn", "outcome": "Down"},
        ],
        "end_date_iso": "2026-03-17T15:00:00Z",
        "game_start_time": "2026-03-17T14:45:00Z",
    }
    mw = parse_market_to_window(market, "btc-updown-15m-123")
    assert mw is not None
    assert mw.asset == "BTC"
    assert mw.up_token_id == "tok_up"
    assert mw.dn_token_id == "tok_dn"
    assert mw.timeframe_sec == 900


def test_parse_market_missing_tokens():
    market = {
        "condition_id": "0xabc",
        "question": "Will Bitcoin go up?",
        "tokens": [{"token_id": "tok_up", "outcome": "Up"}],
    }
    mw = parse_market_to_window(market, "btc-updown-15m-123")
    assert mw is None

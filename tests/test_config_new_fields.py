from polybot.config import BotConfig


def test_new_data_layer_defaults():
    cfg = BotConfig()
    assert cfg.binance_ws_url == "wss://stream.binance.com:9443/ws"
    assert cfg.binance_fallback_interval_sec == 2.0
    assert cfg.clob_midpoint_poll_sec == 2.0
    assert cfg.market_ws_ping_sec == 10.0
    assert cfg.book_stale_sec == 30.0
    assert cfg.market_discovery_interval_sec == 60


def test_existing_fields_unchanged():
    cfg = BotConfig()
    assert cfg.ladder_rungs == 36
    assert cfg.ladder_spacing == 0.02
    assert cfg.max_pair_cost == 0.95
    assert cfg.web_port == 8080
    assert cfg.dry_run is True

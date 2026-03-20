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
    assert cfg.ladder_rungs == 31
    assert cfg.ladder_spacing == 0.01
    assert cfg.max_pair_cost == 0.92
    assert cfg.web_port == 8080
    assert cfg.dry_run is True


def test_get_ladder_params_dynamic_bankroll():
    """get_ladder_params should use current_bankroll, not self.bankroll."""
    from polybot.config import BotConfig

    cfg = BotConfig(dry_run=True, bankroll=1000.0)

    lp_100 = cfg.get_ladder_params(900, current_bankroll=100.0)
    lp_50k = cfg.get_ladder_params(900, current_bankroll=50000.0)

    assert lp_100.position_size_fraction > lp_50k.position_size_fraction
    assert lp_100.rungs < lp_50k.rungs


def test_get_ladder_params_5m_dynamic_bankroll():
    """5m params should also scale with current_bankroll."""
    from polybot.config import BotConfig

    cfg = BotConfig(dry_run=True, bankroll=1000.0)

    lp_100 = cfg.get_ladder_params(300, current_bankroll=100.0)
    lp_50k = cfg.get_ladder_params(300, current_bankroll=50000.0)

    assert lp_100.position_size_fraction > lp_50k.position_size_fraction
    assert lp_100.position_size_fraction < 0.10

from polybot.config import BotConfig, effective_assets


def test_new_data_layer_defaults():
    cfg = BotConfig()
    assert cfg.binance_ws_url == "wss://stream.binance.com:9443/ws"
    assert cfg.binance_fallback_interval_sec == 2.0
    assert cfg.clob_midpoint_poll_sec == 2.0
    assert cfg.market_ws_ping_sec == 10.0
    assert cfg.book_stale_sec == 30.0
    assert cfg.market_discovery_interval_sec == 15


def test_existing_fields_unchanged():
    cfg = BotConfig()
    assert cfg.ladder_rungs == 15
    assert cfg.ladder_spacing == 0.01
    assert cfg.max_pair_cost == 0.93
    assert cfg.web_port == 8080
    assert cfg.dry_run is True


def test_get_ladder_params_dynamic_bankroll():
    """get_ladder_params should use current_bankroll, not self.bankroll."""
    from polybot.config import BotConfig

    cfg = BotConfig(dry_run=True, bankroll=1000.0)

    lp_100 = cfg.get_ladder_params(900, current_bankroll=100.0)
    lp_50k = cfg.get_ladder_params(900, current_bankroll=50000.0)

    assert lp_100.position_size_fraction > lp_50k.position_size_fraction
    assert lp_100.rungs <= lp_50k.rungs  # may be equal when both hit cap


def test_get_ladder_params_5m_dynamic_bankroll():
    """5m params should also scale with current_bankroll."""
    from polybot.config import BotConfig

    cfg = BotConfig(dry_run=True, bankroll=1000.0)

    lp_100 = cfg.get_ladder_params(300, current_bankroll=100.0)
    lp_50k = cfg.get_ladder_params(300, current_bankroll=50000.0)

    assert lp_100.position_size_fraction > lp_50k.position_size_fraction
    assert lp_100.position_size_fraction < 0.10


# --- balance_poll_sec tests ---

def test_balance_poll_sec_default():
    cfg = BotConfig()
    assert cfg.balance_poll_sec == 60.0


def test_balance_poll_sec_override(monkeypatch):
    monkeypatch.setenv("BALANCE_POLL_SEC", "30")
    from polybot.config import load_bot_config
    cfg = load_bot_config()
    assert cfg.balance_poll_sec == 30.0


# --- effective_assets tests ---

def test_effective_assets_low_bankroll():
    """<$500 -> 1 asset (BTC by priority)."""
    assets = ("BTC", "ETH", "SOL", "XRP")
    result = effective_assets(assets, 300.0)
    assert result == ("BTC",)


def test_effective_assets_medium_bankroll():
    """$500-$2000 -> 2 assets (BTC, ETH by priority)."""
    assets = ("BTC", "ETH", "SOL", "XRP")
    result = effective_assets(assets, 1000.0)
    assert result == ("BTC", "ETH")


def test_effective_assets_high_bankroll():
    """$2000+ -> all enabled."""
    assets = ("BTC", "ETH", "SOL", "XRP")
    result = effective_assets(assets, 5000.0)
    assert result == ("BTC", "ETH", "SOL", "XRP")


def test_effective_assets_respects_trade_env_vars():
    """User disables some assets -> effective_assets can only narrow further."""
    assets = ("ETH", "SOL")  # user disabled BTC and XRP
    result = effective_assets(assets, 300.0)
    assert len(result) == 1
    assert result == ("ETH",)  # ETH has higher priority than SOL


def test_effective_assets_priority_order():
    """BTC is always preferred over others."""
    assets = ("XRP", "SOL", "BTC")  # out of priority order
    result = effective_assets(assets, 800.0)
    assert result == ("BTC", "SOL")  # BTC first, then SOL (index 2 < XRP index 3)


def test_effective_assets_boundary_values():
    """Boundary: $400 exactly -> 2 assets, $2000 exactly -> all."""
    assets = ("BTC", "ETH", "SOL", "XRP")
    # $400 is in the "< 2000" bracket -> 2 assets
    assert len(effective_assets(assets, 400.0)) == 2
    # $2000 is in the ">= 2000" bracket -> all
    assert len(effective_assets(assets, 2000.0)) == 4
    # Just below boundaries
    assert len(effective_assets(assets, 399.99)) == 1
    assert len(effective_assets(assets, 1999.99)) == 2


# --- fv_gate_enabled tests (2026-04-11 kill) ---

def test_fv_gate_enabled_default_is_false():
    """BotConfig default for fv_gate_enabled must be False.

    Gate disabled by default since 2026-04-11 (33% win rate at 80-89% cert,
    500ms-delay removal killed info-arb edge).
    """
    cfg = BotConfig()
    assert cfg.fv_gate_enabled is False, (
        f"BotConfig.fv_gate_enabled default should be False, got {cfg.fv_gate_enabled}"
    )


def test_config_reads_fv_gate_enabled_true_from_env(monkeypatch):
    """FV_GATE_ENABLED=true in env -> cfg.fv_gate_enabled == True."""
    monkeypatch.setenv("FV_GATE_ENABLED", "true")
    from polybot.config import load_bot_config
    cfg = load_bot_config()
    assert cfg.fv_gate_enabled is True


def test_config_reads_fv_gate_enabled_false_from_env(monkeypatch):
    """FV_GATE_ENABLED=false in env -> cfg.fv_gate_enabled == False."""
    monkeypatch.setenv("FV_GATE_ENABLED", "false")
    from polybot.config import load_bot_config
    cfg = load_bot_config()
    assert cfg.fv_gate_enabled is False


def test_config_fv_gate_enabled_default_env_missing(monkeypatch):
    """When FV_GATE_ENABLED is not set, cfg.fv_gate_enabled defaults to False."""
    monkeypatch.delenv("FV_GATE_ENABLED", raising=False)
    from polybot.config import load_bot_config
    cfg = load_bot_config()
    assert cfg.fv_gate_enabled is False

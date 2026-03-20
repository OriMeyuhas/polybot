import os
from dataclasses import dataclass, field
from typing import NamedTuple
from dotenv import load_dotenv


class LadderParams(NamedTuple):
    """Timeframe-specific ladder parameters."""
    rungs: int
    spacing: float
    width: float
    size_skew: float
    max_pair_cost: float
    position_size_fraction: float


@dataclass(frozen=True)
class TrackerConfig:
    # Polymarket
    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_data_api: str = "https://data-api.polymarket.com"
    chain_id: int = 137

    # Tracker target
    tracked_wallet: str = "0x63ce342161250d705dc0b16df89036c8e5f9ba9a"

    # Binance
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"

    # Polling
    poll_interval_sec: int = 5

    # Logging
    log_level: str = "INFO"

    # Assets we care about
    assets: tuple = ("BTC", "ETH", "SOL", "XRP")

    # Polygonscan (optional)
    polygonscan_api_key: str = ""

    # Enhanced tracker pipeline settings
    trade_poll_interval_sec: int = 2
    spot_record_interval_sec: int = 2
    book_snapshot_interval_sec: int = 5
    settlement_retry_max: int = 5
    settlement_retry_backoff_sec: float = 2.0
    settlement_give_up_sec: float = 14400.0  # 4 hours (hourly markets take 2-3h to resolve)
    clob_book_poll_url: str = "https://clob.polymarket.com/book"


@dataclass(frozen=True)
class BotConfig:
    # Polymarket CLOB
    polymarket_host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""

    # Binance
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"

    # Assets
    assets: tuple = ("BTC", "ETH", "SOL", "XRP")

    # Ladder parameters — 15m default (whale-calibrated: 0x8dxd tracker 36h, 119 windows)
    ladder_rungs: int = 31
    ladder_spacing: float = 0.01
    ladder_width: float = 0.70
    ladder_size_skew: float = 8.9
    max_pair_cost: float = 0.54   # 95th-pct winning avg_price = 0.541
    position_size_fraction: float = 0.05

    # Ladder parameters — 5m overrides (whale-calibrated: 324 windows)
    ladder_rungs_5m: int = 23
    ladder_spacing_5m: float = 0.01
    ladder_width_5m: float = 0.55
    ladder_size_skew_5m: float = 4.5
    max_pair_cost_5m: float = 0.56  # 95th-pct winning avg_price = 0.557
    position_size_fraction_5m: float = 0.021

    # Shared ladder / risk parameters
    reprice_threshold: float = 0.03
    max_imbalance_ratio: float = 0.60
    imbalance_timeout_sec: int = 30
    # Heartbeat
    heartbeat_interval_sec: float = 5.0
    heartbeat_max_failures: int = 2

    # Tick size cache
    tick_size_ttl_sec: float = 60.0

    # Batch orders (Polymarket allows up to 50; 15 is conservative default)
    batch_order_size: int = 15

    # Redemption
    redemption_retry_max: int = 10
    redemption_retry_backoff_sec: float = 2.0

    # Settlement (bot-side)
    bot_settlement_give_up_sec: float = 14400.0

    # Risk limits
    max_concurrent_positions: int = 8
    max_daily_drawdown_pct: float = 0.05
    no_trade_final_sec: int = 60

    # Polling
    poll_interval_ms: int = 500
    market_discovery_interval_sec: int = 60

    # Logging
    log_level: str = "INFO"

    # Safety
    dry_run: bool = True

    # Mock client tuning
    mock_base_fill_rate: float = 0.03
    web_port: int = 8080
    start_paused: bool = False

    # Data layer config (new for infrastructure rebuild)
    binance_fallback_interval_sec: float = 2.0
    clob_midpoint_poll_sec: float = 2.0
    market_ws_ping_sec: float = 10.0
    book_stale_sec: float = 30.0
    coingecko_ids: tuple = ("bitcoin", "ethereum", "solana", "ripple")
    bankroll: float = 1000.0  # Default paper bankroll; overridable via env

    def get_ladder_params(self, timeframe_sec: int, current_bankroll: float | None = None) -> LadderParams:
        """Return ladder parameters tuned for the given timeframe.

        Auto-scales position_size_fraction and rung count based on current_bankroll.
        Falls back to self.bankroll if current_bankroll is not provided (backward compat).
        """
        import math
        bankroll = max(current_bankroll if current_bankroll is not None else self.bankroll, 50)
        auto_fraction = max(0.02, min(0.30, 25.0 / bankroll))
        auto_rungs = max(8, min(60, int(12 * math.log10(bankroll))))

        if timeframe_sec <= 300:
            return LadderParams(
                rungs=min(auto_rungs, self.ladder_rungs_5m),
                spacing=self.ladder_spacing_5m,
                width=self.ladder_width_5m,
                size_skew=self.ladder_size_skew_5m,
                max_pair_cost=self.max_pair_cost_5m,
                position_size_fraction=auto_fraction * 0.33,
            )
        return LadderParams(
            rungs=min(auto_rungs, self.ladder_rungs),
            spacing=self.ladder_spacing,
            width=self.ladder_width,
            size_skew=self.ladder_size_skew,
            max_pair_cost=self.max_pair_cost,
            position_size_fraction=auto_fraction,
        )


def load_bot_config() -> BotConfig:
    load_dotenv()

    # Filter assets based on TRADE_* env vars (default: all enabled)
    all_assets = ("BTC", "ETH", "SOL", "XRP")
    assets = tuple(
        a for a in all_assets
        if os.getenv(f"TRADE_{a}", "true").lower() in ("true", "1", "yes")
    )
    if not assets:
        assets = all_assets  # fallback: don't run with zero assets

    return BotConfig(
        assets=assets,
        polymarket_host=os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com"),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        private_key=os.getenv("PRIVATE_KEY", ""),
        api_key=os.getenv("API_KEY", ""),
        api_secret=os.getenv("API_SECRET", ""),
        api_passphrase=os.getenv("API_PASSPHRASE", ""),
        binance_ws_url=os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"),
        ladder_rungs=int(os.getenv("LADDER_RUNGS", "31")),
        ladder_spacing=float(os.getenv("LADDER_SPACING", "0.01")),
        ladder_width=float(os.getenv("LADDER_WIDTH", "0.70")),
        ladder_size_skew=float(os.getenv("LADDER_SIZE_SKEW", "8.9")),
        max_pair_cost=float(os.getenv("MAX_PAIR_COST", "0.54")),
        position_size_fraction=float(os.getenv("POSITION_SIZE_FRACTION", "0.05")),
        ladder_rungs_5m=int(os.getenv("LADDER_RUNGS_5M", "23")),
        ladder_spacing_5m=float(os.getenv("LADDER_SPACING_5M", "0.01")),
        ladder_width_5m=float(os.getenv("LADDER_WIDTH_5M", "0.55")),
        ladder_size_skew_5m=float(os.getenv("LADDER_SIZE_SKEW_5M", "4.5")),
        max_pair_cost_5m=float(os.getenv("MAX_PAIR_COST_5M", "0.56")),
        position_size_fraction_5m=float(os.getenv("POSITION_SIZE_FRACTION_5M", "0.021")),
        reprice_threshold=float(os.getenv("REPRICE_THRESHOLD", "0.03")),
        max_imbalance_ratio=float(os.getenv("MAX_IMBALANCE_RATIO", "0.60")),
        imbalance_timeout_sec=int(os.getenv("IMBALANCE_TIMEOUT_SEC", "30")),
        heartbeat_interval_sec=float(os.getenv("HEARTBEAT_INTERVAL_SEC", "5.0")),
        heartbeat_max_failures=int(os.getenv("HEARTBEAT_MAX_FAILURES", "2")),
        tick_size_ttl_sec=float(os.getenv("TICK_SIZE_TTL_SEC", "60.0")),
        batch_order_size=int(os.getenv("BATCH_ORDER_SIZE", "15")),
        redemption_retry_max=int(os.getenv("REDEMPTION_RETRY_MAX", "10")),
        redemption_retry_backoff_sec=float(os.getenv("REDEMPTION_RETRY_BACKOFF_SEC", "2.0")),
        bot_settlement_give_up_sec=float(os.getenv("BOT_SETTLEMENT_GIVE_UP_SEC", "14400.0")),
        max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "8")),
        max_daily_drawdown_pct=float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "0.05")),
        no_trade_final_sec=int(os.getenv("NO_TRADE_FINAL_SEC", "60")),
        poll_interval_ms=int(os.getenv("BOT_POLL_INTERVAL_MS", "500")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        dry_run=os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        mock_base_fill_rate=float(os.getenv("MOCK_BASE_FILL_RATE", "0.03")),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        start_paused=os.getenv("START_PAUSED", "false").lower() in ("true", "1", "yes"),
        binance_fallback_interval_sec=float(os.getenv("BINANCE_FALLBACK_INTERVAL_SEC", "2.0")),
        clob_midpoint_poll_sec=float(os.getenv("CLOB_MIDPOINT_POLL_SEC", "2.0")),
        market_ws_ping_sec=float(os.getenv("MARKET_WS_PING_SEC", "10.0")),
        book_stale_sec=float(os.getenv("BOOK_STALE_SEC", "30.0")),
        bankroll=float(os.getenv("BANKROLL", "1000.0")),
    )


def load_config() -> TrackerConfig:
    load_dotenv()

    return TrackerConfig(
        polymarket_host=os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com"),
        polymarket_data_api=os.getenv("POLYMARKET_DATA_API", "https://data-api.polymarket.com"),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        tracked_wallet=os.getenv("TRACKED_WALLET", "0x63ce342161250d705dc0b16df89036c8e5f9ba9a"),
        binance_ws_url=os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"),
        poll_interval_sec=int(os.getenv("POLL_INTERVAL_SEC", "5")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        polygonscan_api_key=os.getenv("POLYGONSCAN_API_KEY", ""),
        trade_poll_interval_sec=int(os.getenv("TRADE_POLL_INTERVAL_SEC", "2")),
        spot_record_interval_sec=int(os.getenv("SPOT_RECORD_INTERVAL_SEC", "2")),
        book_snapshot_interval_sec=int(os.getenv("BOOK_SNAPSHOT_INTERVAL_SEC", "5")),
        settlement_retry_max=int(os.getenv("SETTLEMENT_RETRY_MAX", "5")),
        settlement_retry_backoff_sec=float(os.getenv("SETTLEMENT_RETRY_BACKOFF_SEC", "2.0")),
        settlement_give_up_sec=float(os.getenv("SETTLEMENT_GIVE_UP_SEC", "14400.0")),
        clob_book_poll_url=os.getenv("CLOB_BOOK_POLL_URL", "https://clob.polymarket.com/book"),
    )

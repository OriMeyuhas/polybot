import os
from dataclasses import dataclass, field
from dotenv import load_dotenv


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

    # Ladder parameters (from 0x8dxd whale analysis)
    ladder_rungs: int = 16
    ladder_spacing: float = 0.01
    ladder_width: float = 0.15
    ladder_size_skew: float = 2.0
    reprice_threshold: float = 0.03
    max_imbalance_ratio: float = 0.60
    imbalance_timeout_sec: int = 30

    # Position sizing & risk
    max_pair_cost: float = 0.985
    position_size_fraction: float = 0.10
    early_exit_profit_pct: float = 0.50
    stop_loss_reversal: float = 0.001

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
    mock_base_fill_rate: float = 0.15


def load_bot_config() -> BotConfig:
    load_dotenv()
    return BotConfig(
        polymarket_host=os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com"),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        private_key=os.getenv("PRIVATE_KEY", ""),
        api_key=os.getenv("API_KEY", ""),
        api_secret=os.getenv("API_SECRET", ""),
        api_passphrase=os.getenv("API_PASSPHRASE", ""),
        binance_ws_url=os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"),
        ladder_rungs=int(os.getenv("LADDER_RUNGS", "16")),
        ladder_spacing=float(os.getenv("LADDER_SPACING", "0.01")),
        ladder_width=float(os.getenv("LADDER_WIDTH", "0.15")),
        ladder_size_skew=float(os.getenv("LADDER_SIZE_SKEW", "2.0")),
        reprice_threshold=float(os.getenv("REPRICE_THRESHOLD", "0.02")),
        max_imbalance_ratio=float(os.getenv("MAX_IMBALANCE_RATIO", "0.60")),
        imbalance_timeout_sec=int(os.getenv("IMBALANCE_TIMEOUT_SEC", "30")),
        max_pair_cost=float(os.getenv("MAX_PAIR_COST", "0.985")),
        position_size_fraction=float(os.getenv("POSITION_SIZE_FRACTION", "0.10")),
        early_exit_profit_pct=float(os.getenv("EARLY_EXIT_PROFIT_PCT", "0.50")),
        stop_loss_reversal=float(os.getenv("STOP_LOSS_REVERSAL", "0.001")),
        max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "8")),
        max_daily_drawdown_pct=float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "0.05")),
        no_trade_final_sec=int(os.getenv("NO_TRADE_FINAL_SEC", "60")),
        poll_interval_ms=int(os.getenv("BOT_POLL_INTERVAL_MS", "500")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        dry_run=os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        mock_base_fill_rate=float(os.getenv("MOCK_BASE_FILL_RATE", "0.15")),
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
        clob_book_poll_url=os.getenv("CLOB_BOOK_POLL_URL", "https://clob.polymarket.com/book"),
    )

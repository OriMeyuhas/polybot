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

    # Strategy thresholds (from spec Section 8.2)
    min_spread_edge: float = 0.025
    min_directional_move: float = 0.002
    max_pair_cost: float = 0.985
    max_directional_price: float = 0.93
    min_directional_price: float = 0.07
    window_min_elapsed_sec: int = 480
    spread_min_elapsed_pct: float = 0.10  # enter spreads after 10% of window elapsed
    position_size_fraction: float = 0.10
    stop_loss_reversal: float = 0.001
    early_exit_profit_pct: float = 0.50  # exit spread when one side worth 50%+ more than cost

    # Risk limits
    max_concurrent_positions: int = 8
    max_capital_per_window_pct: float = 0.15
    max_daily_drawdown_pct: float = 0.05
    no_trade_final_sec: int = 60
    spread_fill_timeout_sec: int = 30
    max_book_depth_take_pct: float = 0.50

    # Polling
    poll_interval_ms: int = 500
    market_discovery_interval_sec: int = 60

    # Logging
    log_level: str = "INFO"

    # Safety
    dry_run: bool = True  # Default to dry run — must explicitly disable


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
        min_spread_edge=float(os.getenv("MIN_SPREAD_EDGE", "0.025")),
        min_directional_move=float(os.getenv("MIN_DIRECTIONAL_MOVE", "0.002")),
        max_pair_cost=float(os.getenv("MAX_PAIR_COST", "0.985")),
        max_directional_price=float(os.getenv("MAX_DIRECTIONAL_PRICE", "0.93")),
        min_directional_price=float(os.getenv("MIN_DIRECTIONAL_PRICE", "0.07")),
        window_min_elapsed_sec=int(os.getenv("WINDOW_MIN_ELAPSED_SEC", "480")),
        spread_min_elapsed_pct=float(os.getenv("SPREAD_MIN_ELAPSED_PCT", "0.10")),
        position_size_fraction=float(os.getenv("POSITION_SIZE_FRACTION", "0.10")),
        stop_loss_reversal=float(os.getenv("STOP_LOSS_REVERSAL", "0.001")),
        early_exit_profit_pct=float(os.getenv("EARLY_EXIT_PROFIT_PCT", "0.50")),
        max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "8")),
        max_capital_per_window_pct=float(os.getenv("MAX_CAPITAL_PER_WINDOW_PCT", "0.15")),
        max_daily_drawdown_pct=float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "0.05")),
        no_trade_final_sec=int(os.getenv("NO_TRADE_FINAL_SEC", "60")),
        spread_fill_timeout_sec=int(os.getenv("SPREAD_FILL_TIMEOUT_SEC", "30")),
        max_book_depth_take_pct=float(os.getenv("MAX_BOOK_DEPTH_TAKE_PCT", "0.50")),
        poll_interval_ms=int(os.getenv("BOT_POLL_INTERVAL_MS", "500")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        dry_run=os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
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
    )

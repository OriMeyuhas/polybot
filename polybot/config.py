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

    # Ladder parameters — 15m default (calibrated from 0x8dxd tracker 22.5h data)
    ladder_rungs: int = 36
    ladder_spacing: float = 0.02
    ladder_width: float = 0.70
    ladder_size_skew: float = 4.0
    max_pair_cost: float = 0.95   # data: >0.95 loses money (-$600/trade at 0.97-1.0)
    position_size_fraction: float = 0.10

    # Ladder parameters — 5m overrides (tighter spread capture profile)
    ladder_rungs_5m: int = 27
    ladder_spacing_5m: float = 0.02
    ladder_width_5m: float = 0.52
    ladder_size_skew_5m: float = 2.0
    max_pair_cost_5m: float = 0.95  # data: 0.92-0.97 still profitable (+$53/trade)
    position_size_fraction_5m: float = 0.033

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
    mock_base_fill_rate: float = 0.15
    web_port: int = 8080

    def get_ladder_params(self, timeframe_sec: int) -> LadderParams:
        """Return ladder parameters tuned for the given timeframe."""
        if timeframe_sec <= 300:  # 5m or shorter
            return LadderParams(
                rungs=self.ladder_rungs_5m,
                spacing=self.ladder_spacing_5m,
                width=self.ladder_width_5m,
                size_skew=self.ladder_size_skew_5m,
                max_pair_cost=self.max_pair_cost_5m,
                position_size_fraction=self.position_size_fraction_5m,
            )
        # 15m and longer use default params
        return LadderParams(
            rungs=self.ladder_rungs,
            spacing=self.ladder_spacing,
            width=self.ladder_width,
            size_skew=self.ladder_size_skew,
            max_pair_cost=self.max_pair_cost,
            position_size_fraction=self.position_size_fraction,
        )


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
        ladder_rungs=int(os.getenv("LADDER_RUNGS", "36")),
        ladder_spacing=float(os.getenv("LADDER_SPACING", "0.02")),
        ladder_width=float(os.getenv("LADDER_WIDTH", "0.70")),
        ladder_size_skew=float(os.getenv("LADDER_SIZE_SKEW", "4.0")),
        max_pair_cost=float(os.getenv("MAX_PAIR_COST", "0.995")),
        position_size_fraction=float(os.getenv("POSITION_SIZE_FRACTION", "0.10")),
        ladder_rungs_5m=int(os.getenv("LADDER_RUNGS_5M", "27")),
        ladder_spacing_5m=float(os.getenv("LADDER_SPACING_5M", "0.02")),
        ladder_width_5m=float(os.getenv("LADDER_WIDTH_5M", "0.52")),
        ladder_size_skew_5m=float(os.getenv("LADDER_SIZE_SKEW_5M", "2.0")),
        max_pair_cost_5m=float(os.getenv("MAX_PAIR_COST_5M", "0.92")),
        position_size_fraction_5m=float(os.getenv("POSITION_SIZE_FRACTION_5M", "0.033")),
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
        mock_base_fill_rate=float(os.getenv("MOCK_BASE_FILL_RATE", "0.15")),
        web_port=int(os.getenv("WEB_PORT", "8080")),
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

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


class TradingRules(NamedTuple):
    """Bankroll-adaptive trading rules."""
    assets: tuple
    timeframes: tuple       # allowed timeframe_sec values (300, 900, 3600)
    max_concurrent: int
    position_fraction: float  # base fraction per window


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

    # Ladder parameters — 15m (whale-calibrated: 109K trades, 897 markets, 830 settlements)
    ladder_rungs: int = 31
    ladder_spacing: float = 0.01
    ladder_width: float = 0.41
    ladder_size_skew: float = 1.0
    max_pair_cost: float = 0.93   # combined UP+DN VWAP ceiling (whale data: >0.92 loses money)
    position_size_fraction: float = 0.05

    # Ladder parameters — 5m overrides
    ladder_rungs_5m: int = 22
    ladder_spacing_5m: float = 0.01
    ladder_width_5m: float = 0.29
    ladder_size_skew_5m: float = 1.0
    max_pair_cost_5m: float = 0.93
    position_size_fraction_5m: float = 0.021

    # Ladder parameters — 1h overrides
    ladder_rungs_1h: int = 22
    ladder_spacing_1h: float = 0.01
    ladder_width_1h: float = 0.42
    ladder_size_skew_1h: float = 1.0
    max_pair_cost_1h: float = 0.93
    position_size_fraction_1h: float = 0.03

    # Shared ladder / risk parameters
    reprice_threshold: float = 0.05
    max_imbalance_ratio: float = 0.60
    imbalance_timeout_sec: int = 120
    # Heartbeat
    heartbeat_interval_sec: float = 5.0
    heartbeat_max_failures: int = 2
    heartbeat_recovery_threshold: int = 3

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
    market_discovery_interval_sec: int = 15
    balance_poll_sec: float = 60.0

    # Logging
    log_level: str = "INFO"

    # Timeframe toggles
    trade_5m: bool = True
    trade_15m: bool = True
    trade_1h: bool = True

    # Safety
    dry_run: bool = True

    # Mock client tuning
    mock_base_fill_rate: float = 0.03
    # Polymarket maker fee rate (crypto up/down markets: 1.56%)
    maker_fee_rate: float = 0.0156
    web_port: int = 8080
    start_paused: bool = False

    # Data layer config (new for infrastructure rebuild)
    binance_fallback_interval_sec: float = 2.0
    clob_midpoint_poll_sec: float = 2.0
    market_ws_ping_sec: float = 10.0
    book_stale_sec: float = 30.0
    price_stale_sec: float = 30.0
    price_snap_stale_sec: float = 15.0
    consecutive_loss_halt: int = 5
    max_capital_at_risk_pct: float = 0.40
    coingecko_ids: tuple = ("bitcoin", "ethereum", "solana", "ripple")
    bankroll: float = 1000.0  # Default paper bankroll; overridable via env

    # Pair recovery parameters
    boost_elapsed_pct: float = 0.20       # Phase D: min fraction of window elapsed before boost
    force_buy_elapsed_pct: float = 0.70   # Phase B: min fraction of window elapsed before force-buy
    force_buy_max_pair_cost: float = 0.93 # Phase B: pair cost ceiling for forced buy
    imbalance_min_heavy_fills: int = 3    # Min fully filled orders on heavy side before imbalance fires

    def get_ladder_params(self, timeframe_sec: int, current_bankroll: float | None = None) -> LadderParams:
        """Return ladder parameters tuned for the given timeframe.

        Uses get_trading_rules() for bankroll-adaptive position sizing.
        """
        import math
        bankroll = max(current_bankroll if current_bankroll is not None else self.bankroll, 50)
        rules = get_trading_rules(self.assets, bankroll)
        base_fraction = rules.position_fraction
        auto_rungs = max(8, min(60, int(12 * math.log10(bankroll))))

        if timeframe_sec <= 300:  # 5m
            return LadderParams(
                rungs=min(auto_rungs, self.ladder_rungs_5m),
                spacing=self.ladder_spacing_5m,
                width=self.ladder_width_5m,
                size_skew=self.ladder_size_skew_5m,
                max_pair_cost=self.max_pair_cost_5m,
                position_size_fraction=base_fraction * 0.33,
            )
        if timeframe_sec <= 900:  # 15m
            return LadderParams(
                rungs=min(auto_rungs, self.ladder_rungs),
                spacing=self.ladder_spacing,
                width=self.ladder_width,
                size_skew=self.ladder_size_skew,
                max_pair_cost=self.max_pair_cost,
                position_size_fraction=base_fraction,
            )
        # 1h+ — whale data: 1h is most profitable per market ($31.71 avg), give 1.5x budget
        return LadderParams(
            rungs=min(auto_rungs, self.ladder_rungs_1h),
            spacing=self.ladder_spacing_1h,
            width=self.ladder_width_1h,
            size_skew=self.ladder_size_skew_1h,
            max_pair_cost=self.max_pair_cost_1h,
            position_size_fraction=base_fraction * 1.5,
        )


def validate_live_config(cfg: BotConfig) -> list[str]:
    """Validate config bounds for live trading. Returns list of error messages."""
    errors = []
    if cfg.position_size_fraction > 0.30:
        errors.append(f"position_size_fraction={cfg.position_size_fraction} exceeds 0.30 safety limit")
    if cfg.max_daily_drawdown_pct > 0.20:
        errors.append(f"max_daily_drawdown_pct={cfg.max_daily_drawdown_pct} exceeds 0.20 safety limit")
    if cfg.max_concurrent_positions > 20:
        errors.append(f"max_concurrent_positions={cfg.max_concurrent_positions} exceeds 20")
    if cfg.bankroll < 10:
        errors.append(f"bankroll=${cfg.bankroll} below $10 minimum")
    if cfg.batch_order_size > 50:
        errors.append(f"batch_order_size={cfg.batch_order_size} exceeds Polymarket limit of 50")
    if cfg.maker_fee_rate < 0.0:
        errors.append(f"maker_fee_rate={cfg.maker_fee_rate} is negative — fee rates cannot be negative")
    if cfg.maker_fee_rate > 0.10:
        errors.append(f"maker_fee_rate={cfg.maker_fee_rate} exceeds 0.10 sanity bound — check Polymarket fee schedule")
    # Pair recovery validation
    if not (0.80 < cfg.force_buy_max_pair_cost < 0.99):
        errors.append(f"force_buy_max_pair_cost={cfg.force_buy_max_pair_cost} must be in (0.80, 0.99)")
    if cfg.boost_elapsed_pct >= cfg.force_buy_elapsed_pct:
        errors.append(f"boost_elapsed_pct={cfg.boost_elapsed_pct} must be < force_buy_elapsed_pct={cfg.force_buy_elapsed_pct}")
    if cfg.force_buy_elapsed_pct >= 0.95:
        errors.append(f"force_buy_elapsed_pct={cfg.force_buy_elapsed_pct} must be < 0.95")
    return errors


ASSET_PRIORITY = ("BTC", "ETH", "SOL", "XRP")


def get_trading_rules(enabled_assets: tuple[str, ...], bankroll: float) -> TradingRules:
    """Bankroll-adaptive trading rules.

    Tiers:
      Micro  (< $200):   1 asset, 5m only, 2 concurrent, 15% fraction
      Small  ($200-500):  1 asset, 5m+15m, 3 concurrent, 10% fraction
      Medium ($500-2000): 2 assets, all TFs, 5 concurrent, 6% fraction
      Standard ($2000+):  all assets, all TFs, 8 concurrent, 2-5% fraction
    """
    sorted_assets = sorted(
        enabled_assets,
        key=lambda a: ASSET_PRIORITY.index(a) if a in ASSET_PRIORITY else 99,
    )

    if bankroll < 200:  # Micro
        # 15m windows instead of 5m: 3x more budget per window ($15 vs $5),
        # more rungs (8-13 vs 1-4), and more time for both sides to fill.
        # 5m windows at $100 produce 1-rung ladders with no edge.
        return TradingRules(
            assets=tuple(sorted_assets[:1]),
            timeframes=(900,),
            max_concurrent=2,
            position_fraction=0.15,
        )
    if bankroll < 400:  # Small
        return TradingRules(
            assets=tuple(sorted_assets[:1]),
            timeframes=(900,),  # no 5m — too short for two-sided fills
            max_concurrent=3,
            position_fraction=0.10,
        )
    if bankroll < 2000:  # Medium
        return TradingRules(
            assets=tuple(sorted_assets[:2]),
            timeframes=(900, 3600),  # no 5m — structurally unprofitable at this bankroll
            max_concurrent=4,
            position_fraction=0.10,
        )
    # Standard ($2000+)
    return TradingRules(
        assets=tuple(sorted_assets),
        timeframes=(300, 900, 3600),
        max_concurrent=8,
        position_fraction=max(0.02, min(0.05, 25.0 / bankroll)),
    )


def effective_assets(enabled_assets: tuple[str, ...], bankroll: float) -> tuple[str, ...]:
    """Convenience wrapper — returns just the asset list from trading rules."""
    return get_trading_rules(enabled_assets, bankroll).assets


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
        ladder_width=float(os.getenv("LADDER_WIDTH", "0.41")),
        ladder_size_skew=float(os.getenv("LADDER_SIZE_SKEW", "1.0")),
        max_pair_cost=float(os.getenv("MAX_PAIR_COST", "0.90")),
        position_size_fraction=float(os.getenv("POSITION_SIZE_FRACTION", "0.05")),
        ladder_rungs_5m=int(os.getenv("LADDER_RUNGS_5M", "23")),
        ladder_spacing_5m=float(os.getenv("LADDER_SPACING_5M", "0.01")),
        ladder_width_5m=float(os.getenv("LADDER_WIDTH_5M", "0.29")),
        ladder_size_skew_5m=float(os.getenv("LADDER_SIZE_SKEW_5M", "1.0")),
        max_pair_cost_5m=float(os.getenv("MAX_PAIR_COST_5M", "0.90")),
        position_size_fraction_5m=float(os.getenv("POSITION_SIZE_FRACTION_5M", "0.021")),
        ladder_rungs_1h=int(os.getenv("LADDER_RUNGS_1H", "22")),
        ladder_spacing_1h=float(os.getenv("LADDER_SPACING_1H", "0.01")),
        ladder_width_1h=float(os.getenv("LADDER_WIDTH_1H", "0.42")),
        ladder_size_skew_1h=float(os.getenv("LADDER_SIZE_SKEW_1H", "1.0")),
        max_pair_cost_1h=float(os.getenv("MAX_PAIR_COST_1H", "0.90")),
        position_size_fraction_1h=float(os.getenv("POSITION_SIZE_FRACTION_1H", "0.03")),
        reprice_threshold=float(os.getenv("REPRICE_THRESHOLD", "0.05")),
        max_imbalance_ratio=float(os.getenv("MAX_IMBALANCE_RATIO", "0.60")),
        imbalance_timeout_sec=int(os.getenv("IMBALANCE_TIMEOUT_SEC", "120")),
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
        balance_poll_sec=float(os.getenv("BALANCE_POLL_SEC", "60.0")),
        log_level=os.getenv("LOG_LEVEL", "ERROR"),
        dry_run=os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        mock_base_fill_rate=float(os.getenv("MOCK_BASE_FILL_RATE", "0.03")),
        maker_fee_rate=float(os.getenv("MAKER_FEE_RATE", "0.0156")),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        start_paused=os.getenv("START_PAUSED", "false").lower() in ("true", "1", "yes"),
        binance_fallback_interval_sec=float(os.getenv("BINANCE_FALLBACK_INTERVAL_SEC", "2.0")),
        clob_midpoint_poll_sec=float(os.getenv("CLOB_MIDPOINT_POLL_SEC", "2.0")),
        market_ws_ping_sec=float(os.getenv("MARKET_WS_PING_SEC", "10.0")),
        book_stale_sec=float(os.getenv("BOOK_STALE_SEC", "30.0")),
        bankroll=float(os.getenv("BANKROLL", "1000.0")),
        trade_5m=os.getenv("TRADE_5M", "true").lower() in ("true", "1", "yes"),
        trade_15m=os.getenv("TRADE_15M", "true").lower() in ("true", "1", "yes"),
        trade_1h=os.getenv("TRADE_1H", "true").lower() in ("true", "1", "yes"),
        boost_elapsed_pct=float(os.getenv("BOOST_ELAPSED_PCT", "0.20")),
        force_buy_elapsed_pct=float(os.getenv("FORCE_BUY_ELAPSED_PCT", "0.70")),
        force_buy_max_pair_cost=float(os.getenv("FORCE_BUY_MAX_PAIR_COST", "0.93")),
        imbalance_min_heavy_fills=int(os.getenv("IMBALANCE_MIN_HEAVY_FILLS", "3")),
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
        log_level=os.getenv("LOG_LEVEL", "ERROR"),
        polygonscan_api_key=os.getenv("POLYGONSCAN_API_KEY", ""),
        trade_poll_interval_sec=int(os.getenv("TRADE_POLL_INTERVAL_SEC", "2")),
        spot_record_interval_sec=int(os.getenv("SPOT_RECORD_INTERVAL_SEC", "2")),
        book_snapshot_interval_sec=int(os.getenv("BOOK_SNAPSHOT_INTERVAL_SEC", "5")),
        settlement_retry_max=int(os.getenv("SETTLEMENT_RETRY_MAX", "5")),
        settlement_retry_backoff_sec=float(os.getenv("SETTLEMENT_RETRY_BACKOFF_SEC", "2.0")),
        settlement_give_up_sec=float(os.getenv("SETTLEMENT_GIVE_UP_SEC", "14400.0")),
        clob_book_poll_url=os.getenv("CLOB_BOOK_POLL_URL", "https://clob.polymarket.com/book"),
    )

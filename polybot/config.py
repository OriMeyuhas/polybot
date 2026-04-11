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

    # Ladder parameters — 15m (balanced: wide enough for margin, tight enough for fills)
    ladder_rungs: int = 15
    ladder_spacing: float = 0.01
    ladder_width: float = 0.15
    ladder_size_skew: float = 2.0   # 2x weight on expensive rungs (fill first on both sides)
    max_pair_cost: float = 0.93     # need >7c margin per pair to absorb excess shares
    position_size_fraction: float = 0.05

    # Ladder parameters — 5m overrides
    ladder_rungs_5m: int = 8
    ladder_spacing_5m: float = 0.01
    ladder_width_5m: float = 0.08
    ladder_size_skew_5m: float = 2.0
    max_pair_cost_5m: float = 0.93
    position_size_fraction_5m: float = 0.021

    # Ladder parameters — 1h overrides (wider for margin, more time to fill)
    ladder_rungs_1h: int = 20
    ladder_spacing_1h: float = 0.01
    ladder_width_1h: float = 0.20
    ladder_size_skew_1h: float = 2.0
    max_pair_cost_1h: float = 0.96
    position_size_fraction_1h: float = 0.03

    # Shared ladder / risk parameters
    reprice_threshold: float = 0.05  # reprice when book moves 5c (less churn = better queue position)
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
    poll_interval_ms: int = 200  # 200ms tick — competitive minimum for adverse selection defense
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
    # Polymarket maker fee rate — makers pay 0% (only takers pay fees)
    maker_fee_rate: float = 0.0
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
    force_buy_max_pair_cost: float = 0.83 # Phase B: pair cost ceiling for forced buy
    imbalance_min_heavy_fills: int = 1    # Min fully filled orders on heavy side before imbalance fires

    # Fair value model — binary option pricing for intelligent budget skewing and exits
    fair_value_enabled: bool = True
    vol_window_sec: int = 300            # rolling window for vol estimation (seconds)
    vol_fallback_annual: float = 0.50    # fallback annual vol when not enough data
    vol_min_samples: int = 30            # min 1-sec bars before trusting vol estimate
    skew_phase_pct: float = 0.30         # start skewing budget at 30% elapsed
    directional_phase_pct: float = 0.70  # enter directional mode at 70% elapsed
    certainty_exit_threshold: float = 0.30    # sell losing side when certainty < 30% (conservative)
    certainty_hold_threshold: float = 0.95    # hold to settlement when certainty >= 95%
    certainty_directional_threshold: float = 0.92  # buy winning side when >= 92% (near-certain only)
    directional_max_ask: float = 0.75    # max price for directional buy (need real discount)
    max_budget_skew: float = 0.80        # max fraction of budget on one side

    # Exit capability — sell losing one-sided positions mid-window (whale exits 12.8% of trades)
    exit_enabled: bool = True
    exit_elapsed_pct: float = 0.55       # min elapsed fraction before considering exit
    exit_min_loss_ratio: float = 3.0     # heavy/light ratio to qualify as "losing"
    exit_target_price: float = 0.35      # target sell price (whale avg exit = $0.35)
    exit_min_price: float = 0.15         # won't sell below this (too much slippage)

    # Reactive pairing — chase pair completion when one side fills
    reactive_pairing_enabled: bool = True
    reactive_chase_width: float = 0.10   # width of chase ladder (tight, near midpoint)
    reactive_chase_budget_pct: float = 0.50  # fraction of remaining budget for chase

    # Inventory-aware quoting — bias unfilled side closer to midpoint
    inventory_skew_enabled: bool = True
    inventory_skew_max: float = 0.60     # max fraction of budget for light side (vs 0.50 default)

    # Spot-awareness parameters
    spot_delta_reduce_threshold: float = 0.0015  # 0.15% — reduce losing side budget
    spot_delta_skip_threshold: float = 0.005     # 0.50% — skip losing side entirely
    spot_gate_force_buy_threshold: float = 0.003 # 0.30% — block force-buy when spot against
    spot_loss_cap_multiplier: float = 0.50       # tighten loss cap when spot confirms loss

    # Directional post hard cap — prevents runaway adverse selection on FV-gated
    # one-sided ladders. When FV gate or spot skip forces 100% of budget onto one
    # side, cap that budget at this absolute dollar amount regardless of bankroll.
    # Sized from 2026-04-11 outlier analysis: $20 optimum on 49-stl session.
    # Proposal #53.
    directional_budget_cap: float = 20.0

    # FV directional gate — when enabled, skips the losing side at cert >= 80%.
    # Disabled 2026-04-11: 500ms-delay removal killed info-arb edge, FV calibration
    # broken at 80-89% (33% win rate, worse than coin flip). Re-enable only after
    # proper model calibration against historical data. See:
    # researcher_2026-04-11_fv-gate-kill-decision.md
    # NOTE: fv_gate_certainty_threshold (hardcoded 0.80 in ladder_manager.py) is
    # inert when fv_gate_enabled=False — the threshold check is never reached.
    fv_gate_enabled: bool = False

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
        # 1h+ — our data confirms: 1h has best balance rate and biggest profits, give 2x budget
        return LadderParams(
            rungs=min(auto_rungs, self.ladder_rungs_1h),
            spacing=self.ladder_spacing_1h,
            width=self.ladder_width_1h,
            size_skew=self.ladder_size_skew_1h,
            max_pair_cost=self.max_pair_cost_1h,
            position_size_fraction=base_fraction * 2.0,
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
    # Fair value validation
    if cfg.skew_phase_pct >= cfg.directional_phase_pct:
        errors.append(
            f"skew_phase_pct={cfg.skew_phase_pct} must be < directional_phase_pct={cfg.directional_phase_pct}"
        )
    if cfg.certainty_exit_threshold >= cfg.certainty_hold_threshold:
        errors.append(
            f"certainty_exit_threshold={cfg.certainty_exit_threshold} must be < "
            f"certainty_hold_threshold={cfg.certainty_hold_threshold}"
        )
    if not (0.5 <= cfg.max_budget_skew <= 0.95):
        errors.append(f"max_budget_skew={cfg.max_budget_skew} must be in [0.50, 0.95]")
    # Spot-awareness validation
    if cfg.spot_delta_reduce_threshold <= 0:
        errors.append(f"spot_delta_reduce_threshold={cfg.spot_delta_reduce_threshold} must be > 0")
    if cfg.spot_delta_reduce_threshold >= cfg.spot_delta_skip_threshold:
        errors.append(
            f"spot_delta_reduce_threshold={cfg.spot_delta_reduce_threshold} must be < "
            f"spot_delta_skip_threshold={cfg.spot_delta_skip_threshold}"
        )
    if not (0 < cfg.spot_loss_cap_multiplier <= 1.0):
        errors.append(f"spot_loss_cap_multiplier={cfg.spot_loss_cap_multiplier} must be in (0, 1.0]")
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


def filter_rules_by_config(rules: TradingRules, cfg: "BotConfig") -> TradingRules:
    """Filter trading rules timeframes by the trade_5m/trade_15m/trade_1h config flags."""
    allowed = set()
    if cfg.trade_5m:
        allowed.add(300)
    if cfg.trade_15m:
        allowed.add(900)
    if cfg.trade_1h:
        allowed.add(3600)
    filtered = tuple(t for t in rules.timeframes if t in allowed)
    if not filtered:
        filtered = (900,)  # fallback to 15m if everything disabled
    return TradingRules(
        assets=rules.assets,
        timeframes=filtered,
        max_concurrent=rules.max_concurrent,
        position_fraction=rules.position_fraction,
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
        ladder_rungs=int(os.getenv("LADDER_RUNGS", "15")),
        ladder_spacing=float(os.getenv("LADDER_SPACING", "0.01")),
        ladder_width=float(os.getenv("LADDER_WIDTH", "0.15")),
        ladder_size_skew=float(os.getenv("LADDER_SIZE_SKEW", "2.0")),
        max_pair_cost=float(os.getenv("MAX_PAIR_COST", "0.93")),
        position_size_fraction=float(os.getenv("POSITION_SIZE_FRACTION", "0.05")),
        ladder_rungs_5m=int(os.getenv("LADDER_RUNGS_5M", "23")),
        ladder_spacing_5m=float(os.getenv("LADDER_SPACING_5M", "0.01")),
        ladder_width_5m=float(os.getenv("LADDER_WIDTH_5M", "0.29")),
        ladder_size_skew_5m=float(os.getenv("LADDER_SIZE_SKEW_5M", "2.0")),
        max_pair_cost_5m=float(os.getenv("MAX_PAIR_COST_5M", "0.93")),
        position_size_fraction_5m=float(os.getenv("POSITION_SIZE_FRACTION_5M", "0.021")),
        ladder_rungs_1h=int(os.getenv("LADDER_RUNGS_1H", "20")),
        ladder_spacing_1h=float(os.getenv("LADDER_SPACING_1H", "0.01")),
        ladder_width_1h=float(os.getenv("LADDER_WIDTH_1H", "0.20")),
        ladder_size_skew_1h=float(os.getenv("LADDER_SIZE_SKEW_1H", "2.0")),
        max_pair_cost_1h=float(os.getenv("MAX_PAIR_COST_1H", "0.96")),
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
        poll_interval_ms=int(os.getenv("BOT_POLL_INTERVAL_MS", "200")),
        balance_poll_sec=float(os.getenv("BALANCE_POLL_SEC", "60.0")),
        log_level=os.getenv("LOG_LEVEL", "ERROR"),
        dry_run=os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        mock_base_fill_rate=float(os.getenv("MOCK_BASE_FILL_RATE", "0.03")),
        maker_fee_rate=float(os.getenv("MAKER_FEE_RATE", "0.0")),
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
        force_buy_max_pair_cost=float(os.getenv("FORCE_BUY_MAX_PAIR_COST", "0.83")),
        imbalance_min_heavy_fills=int(os.getenv("IMBALANCE_MIN_HEAVY_FILLS", "1")),
        fair_value_enabled=os.getenv("FAIR_VALUE_ENABLED", "true").lower() in ("true", "1", "yes"),
        vol_window_sec=int(os.getenv("VOL_WINDOW_SEC", "300")),
        vol_fallback_annual=float(os.getenv("VOL_FALLBACK_ANNUAL", "0.50")),
        vol_min_samples=int(os.getenv("VOL_MIN_SAMPLES", "30")),
        skew_phase_pct=float(os.getenv("SKEW_PHASE_PCT", "0.30")),
        directional_phase_pct=float(os.getenv("DIRECTIONAL_PHASE_PCT", "0.70")),
        certainty_exit_threshold=float(os.getenv("CERTAINTY_EXIT_THRESHOLD", "0.30")),
        certainty_hold_threshold=float(os.getenv("CERTAINTY_HOLD_THRESHOLD", "0.95")),
        certainty_directional_threshold=float(os.getenv("CERTAINTY_DIRECTIONAL_THRESHOLD", "0.92")),
        directional_max_ask=float(os.getenv("DIRECTIONAL_MAX_ASK", "0.75")),
        max_budget_skew=float(os.getenv("MAX_BUDGET_SKEW", "0.80")),
        exit_enabled=os.getenv("EXIT_ENABLED", "true").lower() in ("true", "1", "yes"),
        exit_elapsed_pct=float(os.getenv("EXIT_ELAPSED_PCT", "0.55")),
        exit_min_loss_ratio=float(os.getenv("EXIT_MIN_LOSS_RATIO", "3.0")),
        exit_target_price=float(os.getenv("EXIT_TARGET_PRICE", "0.35")),
        exit_min_price=float(os.getenv("EXIT_MIN_PRICE", "0.15")),
        reactive_pairing_enabled=os.getenv("REACTIVE_PAIRING_ENABLED", "true").lower() in ("true", "1", "yes"),
        reactive_chase_width=float(os.getenv("REACTIVE_CHASE_WIDTH", "0.10")),
        reactive_chase_budget_pct=float(os.getenv("REACTIVE_CHASE_BUDGET_PCT", "0.50")),
        inventory_skew_enabled=os.getenv("INVENTORY_SKEW_ENABLED", "true").lower() in ("true", "1", "yes"),
        inventory_skew_max=float(os.getenv("INVENTORY_SKEW_MAX", "0.60")),
        spot_delta_reduce_threshold=float(os.getenv("SPOT_DELTA_REDUCE_THRESHOLD", "0.0015")),
        spot_delta_skip_threshold=float(os.getenv("SPOT_DELTA_SKIP_THRESHOLD", "0.005")),
        spot_gate_force_buy_threshold=float(os.getenv("SPOT_GATE_FORCE_BUY_THRESHOLD", "0.003")),
        spot_loss_cap_multiplier=float(os.getenv("SPOT_LOSS_CAP_MULTIPLIER", "0.50")),
        directional_budget_cap=float(os.getenv("DIRECTIONAL_BUDGET_CAP", "20.0")),
        fv_gate_enabled=os.getenv("FV_GATE_ENABLED", "false").lower() in ("true", "1", "yes"),
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

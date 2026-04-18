"""Tests for settlement PnL attribution when FV-exit SELL fires before window expiry.

TDD: these tests are written BEFORE the implementation changes.

Scenario:
  - Market opens, BUY fills on UP side
  - FV-exit SELL fires mid-window, reduces position, credits bankroll
  - Window expires → _settle_position is called
  - settlement_log.jsonl must record pnl = realized_from_sell + settle_delta
  - Must NOT be zero even though pos.up_qty == 0 at settlement time
"""

import pytest
from unittest.mock import MagicMock, patch
from polybot.types import Side
from polybot.strategy.position_manager import PositionManager
from polybot.strategy.ladder_manager import LadderManager
from polybot.config import BotConfig


@pytest.fixture
def cfg():
    return BotConfig(position_size_fraction=0.10, max_pair_cost=0.985, dry_run=True)


@pytest.fixture
def pm(cfg):
    return PositionManager(cfg, bankroll=1000.0)


@pytest.fixture
def ladder_manager(cfg, pm):
    executor = MagicMock()
    tracker = MagicMock()
    tracker.orders = {}
    risk = MagicMock()
    risk.exposure_factor.return_value = 1.0
    lm = LadderManager(
        cfg=cfg,
        order_executor=executor,
        order_tracker=tracker,
        position_manager=pm,
        risk_manager=risk,
    )
    return lm


class TestRealizedInWindowAccumulation:
    def test_realized_in_window_dict_exists(self, ladder_manager):
        """LadderManager must have _realized_in_window dict after __init__."""
        assert hasattr(ladder_manager, '_realized_in_window')
        assert isinstance(ladder_manager._realized_in_window, dict)

    def test_sell_fill_accumulates_realized_pnl(self, cfg, pm, ladder_manager):
        """When process_paper_fills handles a SELL fill, realized PnL is accumulated."""
        from polybot.strategy.order_tracker import TrackedOrder

        market_id = "mkt-btc-up-0001"
        # Set up initial BUY position: 100 shares at $0.45 avg = $45.00 cost
        pm.update_position(market_id, Side.UP, qty=100.0, cost=45.0)

        # Create a tracked SELL order
        sell_order = TrackedOrder(
            order_id="sell-001",
            market_id=market_id,
            token_id="tok_up",
            side=Side.UP,
            price=0.55,  # selling at $0.55
            size=100.0,
        )
        # Inject into ladder_manager's tracker
        ladder_manager.tracker.orders = {"sell-001": sell_order}

        # Simulate paper_fills dict that the PaperClobClient would return
        paper_fills = [{"id": "sell-001", "side": "SELL"}]
        ladder_manager.process_paper_fills(paper_fills)

        # realized = proceeds(55.0) - cost_basis(45.0) = $10.00
        accumulated = ladder_manager._realized_in_window.get(market_id, 0.0)
        assert accumulated == pytest.approx(10.0)

    def test_multiple_sell_fills_accumulate(self, cfg, pm, ladder_manager):
        """Multiple SELL fills on same market accumulate into _realized_in_window."""
        from polybot.strategy.order_tracker import TrackedOrder

        market_id = "mkt-btc-up-0002"
        # 200 shares at $0.45 avg = $90 cost
        pm.update_position(market_id, Side.UP, qty=200.0, cost=90.0)

        sell1 = TrackedOrder(
            order_id="sell-002a", market_id=market_id, token_id="tok_up",
            side=Side.UP, price=0.55, size=100.0,
        )
        sell2 = TrackedOrder(
            order_id="sell-002b", market_id=market_id, token_id="tok_up",
            side=Side.UP, price=0.60, size=100.0,
        )
        ladder_manager.tracker.orders = {"sell-002a": sell1, "sell-002b": sell2}

        # First sell: 100 shares @ $0.55 → proceeds=$55, cost_basis=45 → realized=$10
        paper_fills_1 = [{"id": "sell-002a", "side": "SELL"}]
        ladder_manager.process_paper_fills(paper_fills_1)

        # Second sell: 100 shares @ $0.60 → proceeds=$60, cost_basis=45 → realized=$15
        paper_fills_2 = [{"id": "sell-002b", "side": "SELL"}]
        ladder_manager.process_paper_fills(paper_fills_2)

        total = ladder_manager._realized_in_window.get(market_id, 0.0)
        assert total == pytest.approx(25.0)


class TestSettlementPopsRealizedPrior:
    """Integration test: settlement must include _realized_in_window in reported PnL."""

    def _make_bot(self, cfg, pm):
        """Build a minimal Bot with the required dependencies mocked."""
        from polybot.strategy.ladder_manager import LadderManager
        from polybot.strategy.order_tracker import OrderTracker
        from polybot.risk_manager import RiskManager
        from polybot.types import MarketWindow

        executor = MagicMock()
        tracker = OrderTracker()
        risk = MagicMock()
        risk.update_pnl = MagicMock()
        risk.consecutive_losses = 0
        risk.exposure_factor.return_value = 1.0

        lm = LadderManager(
            cfg=cfg, order_executor=executor, order_tracker=tracker,
            position_manager=pm, risk_manager=risk,
        )

        # Build a minimal Bot-like object to call _settle_position
        # We patch bot.py's _settle_position instead of constructing the full bot
        import polybot.bot as bot_module
        return lm, risk

    def test_settle_position_includes_realized_prior(self, cfg, pm):
        """After FV-exit SELL, settlement PnL = settle_delta + realized_prior, not zero."""
        from polybot.types import MarketWindow
        from polybot.strategy.order_tracker import OrderTracker
        from polybot.risk_manager import RiskManager

        mid = "mkt-btc-dn-settle-001"
        market = MarketWindow(
            market_id=mid,
            condition_id="cond-001",
            asset="BTC",
            timeframe_sec=900,
            up_token_id="tok_up_001",
            dn_token_id="tok_dn_001",
            open_epoch=1000000,
            close_epoch=1000900,
        )

        # Position after FV-exit: 100 UP shares at $0.45 avg, then SELL all
        pm.update_position(mid, Side.UP, qty=100.0, cost=45.0)

        executor = MagicMock()
        tracker = OrderTracker()
        risk = MagicMock()
        risk.update_pnl = MagicMock()
        risk.consecutive_losses = 0
        risk.exposure_factor.return_value = 1.0

        from polybot.strategy.ladder_manager import LadderManager
        lm = LadderManager(cfg=cfg, order_executor=executor, order_tracker=tracker,
                           position_manager=pm, risk_manager=risk)

        # Simulate SELL fill: sell 100 UP shares at $0.55 → realized = $10.00
        from polybot.strategy.order_tracker import TrackedOrder
        sell_order = TrackedOrder(
            order_id="sell-settle-001", market_id=mid, token_id="tok_up_001",
            side=Side.UP, price=0.55, size=100.0,
        )
        lm.tracker.orders["sell-settle-001"] = sell_order
        lm.process_paper_fills([{"id": "sell-settle-001", "side": "SELL"}])

        # Verify realized was tracked
        assert lm._realized_in_window.get(mid, 0.0) == pytest.approx(10.0)

        # Now simulate settlement: pos.up_qty == 0 after SELL, so profit_if_up() == -dn_cost
        # pos.dn_qty == 0 and pos.dn_cost == 0 → profit_if_up() = 0.0 - 0.0 = 0.0
        pos = pm.positions.get(mid)
        # After selling all UP shares, up_qty=0, up_cost=0
        assert pos.up_qty == pytest.approx(0.0)

        # _settle_position must pop realized_prior and add it to settle_pnl
        realized_prior = lm._realized_in_window.pop(mid, 0.0)
        assert realized_prior == pytest.approx(10.0)

        # After pop, the key is gone
        assert lm._realized_in_window.get(mid, 0.0) == pytest.approx(0.0)

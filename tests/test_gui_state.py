from decimal import Decimal
from polybot.web.state import GuiStateHolder


def test_initial_state_has_all_spec_fields():
    """Verify all fields from spec's Web API Contract are present."""
    state = GuiStateHolder()
    data = state.get()
    assert data["mode"] == "dry_run"
    assert data["running"] is False
    assert data["heartbeat_healthy"] is True
    assert data["cancel_only_mode"] is False
    assert data["total_pnl"] == 0.0
    assert data["realized_pnl"] == 0.0
    assert data["unrealized_pnl"] == 0.0
    assert data["trade_count"] == 0
    assert data["position_count"] == 0
    assert data["pairs_completed"] == 0
    assert data["avg_pair_cost"] == 0.0
    assert data["imbalance_ratio"] == 0.0
    assert data["runtime_sec"] == 0
    assert data["markets_active"] == 0
    assert data["win_rate"] == 0.0
    assert data["prices"] == {}
    assert data["binance_prices"] == {}
    assert data["spots"] == {}
    assert data["active_markets"] == []
    assert data["activity_feed"] == []
    assert data["trades"] == []
    assert data["pending_settlements"] == []
    assert data["wallet"] is None


def test_update():
    state = GuiStateHolder()
    state.update(running=True, total_pnl=Decimal("42.50"))
    data = state.get()
    assert data["running"] is True
    assert data["total_pnl"] == Decimal("42.50")


def test_serialization_converts_decimals():
    state = GuiStateHolder()
    state.update(total_pnl=Decimal("42.50"))
    serialized = state.serialize()
    assert isinstance(serialized["total_pnl"], float)
    assert serialized["total_pnl"] == 42.50

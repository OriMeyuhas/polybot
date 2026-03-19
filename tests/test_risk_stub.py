from polybot.strategy.risk_stub import RiskStub


def test_is_halted_always_false():
    stub = RiskStub()
    assert stub.is_halted() is False


def test_can_open_position_always_true():
    stub = RiskStub()
    assert stub.can_open_position(0) is True
    assert stub.can_open_position(100) is True


def test_can_trade_in_window_always_true():
    stub = RiskStub()
    assert stub.can_trade_in_window(None, 0) is True


def test_update_pnl_noop():
    stub = RiskStub()
    stub.update_pnl(100.0)  # Should not raise


def test_reset_daily_noop():
    stub = RiskStub()
    stub.reset_daily()  # Should not raise

from decimal import Decimal
from polybot.data.clob_midpoints import ClobMidpointPoller


def test_register_tokens():
    poller = ClobMidpointPoller()
    poller.register_tokens(["token_a", "token_b"])
    assert "token_a" in poller._token_ids
    assert "token_b" in poller._token_ids


def test_remove_tokens():
    poller = ClobMidpointPoller()
    poller.register_tokens(["token_a", "token_b"])
    poller.remove_tokens(["token_a"])
    assert "token_a" not in poller._token_ids
    assert "token_b" in poller._token_ids


def test_get_mid_none_before_poll():
    poller = ClobMidpointPoller()
    assert poller.get_mid("token_a") is None


def test_get_mid_after_manual_set():
    poller = ClobMidpointPoller()
    poller._midpoints["token_a"] = Decimal("0.55")
    assert poller.get_mid("token_a") == Decimal("0.55")

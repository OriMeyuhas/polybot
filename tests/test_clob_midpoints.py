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


def test_set_tokens_replaces_set():
    """set_tokens replaces the full set: A removed, B retained, C added."""
    poller = ClobMidpointPoller()
    poller.register_tokens(["token_a", "token_b"])
    poller._midpoints["token_a"] = Decimal("0.50")
    poller._midpoints["token_b"] = Decimal("0.60")

    poller.set_tokens(["token_b", "token_c"])

    assert "token_a" not in poller._token_ids
    assert "token_a" not in poller._midpoints
    assert "token_b" in poller._token_ids
    assert poller._midpoints["token_b"] == Decimal("0.60")
    assert "token_c" in poller._token_ids


def test_set_tokens_prunes_midpoints():
    """set_tokens([]) removes all midpoints."""
    poller = ClobMidpointPoller()
    poller._midpoints["token_a"] = Decimal("0.50")
    poller._token_ids.add("token_a")

    poller.set_tokens([])

    assert len(poller._midpoints) == 0
    assert len(poller._token_ids) == 0

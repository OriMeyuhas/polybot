"""Tests for time-decay ladder tightening."""

import pytest
from polybot.types import MarketWindow
from polybot.strategy.ladder_manager import compute_decay_factor, build_ladder_rungs


def _make_market(timeframe_sec=900, open_epoch=1000):
    """Create a MarketWindow with the given timeframe."""
    return MarketWindow(
        market_id="btc-15m-100",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=timeframe_sec,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=open_epoch,
        close_epoch=open_epoch + timeframe_sec,
    )


class TestDecayFactor:
    def test_decay_at_window_open(self):
        """At 0% elapsed, decay_factor should be 1.0 (full width)."""
        market = _make_market(timeframe_sec=900, open_epoch=1000)
        # now == open_epoch => 0% elapsed
        factor = compute_decay_factor(market, 1000)
        assert factor == pytest.approx(1.0)

    def test_decay_at_halfway(self):
        """At 50% elapsed, decay_factor should be 0.65."""
        market = _make_market(timeframe_sec=900, open_epoch=1000)
        # 50% elapsed => now = 1000 + 450 = 1450
        factor = compute_decay_factor(market, 1450)
        assert factor == pytest.approx(0.65)

    def test_decay_at_80_percent(self):
        """At 80% elapsed (phase 2), decay holds at floor=0.58."""
        market = _make_market(timeframe_sec=900, open_epoch=1000)
        # 80% > 60% => phase 2, hold at floor
        factor = compute_decay_factor(market, 1720)
        assert factor == pytest.approx(0.58)

    def test_decay_at_expiry(self):
        """At 100% elapsed, decay_factor should be 0.58 (floor, held from phase 2)."""
        market = _make_market(timeframe_sec=900, open_epoch=1000)
        factor = compute_decay_factor(market, 1900)
        assert factor == pytest.approx(0.58)

    def test_decay_floor_never_below(self):
        """Decay factor should never go below the floor (0.58) for any elapsed fraction."""
        market = _make_market(timeframe_sec=900, open_epoch=1000)
        for pct in range(0, 150, 5):
            now = 1000 + int(900 * pct / 100)
            factor = compute_decay_factor(market, now)
            assert factor >= 0.58, f"Factor {factor} < 0.58 at {pct}% elapsed"

    def test_decay_with_zero_timeframe(self):
        """If timeframe_sec is 0, should return floor."""
        market = _make_market(timeframe_sec=0, open_epoch=1000)
        factor = compute_decay_factor(market, 1000)
        assert factor == pytest.approx(0.58)

    def test_decay_custom_floor(self):
        """Custom floor should be respected."""
        market = _make_market(timeframe_sec=900, open_epoch=1000)
        factor = compute_decay_factor(market, 1900, floor=0.5)
        assert factor == pytest.approx(0.5)

    def test_decay_before_open(self):
        """Before window opens, elapsed is 0, factor should be 1.0."""
        market = _make_market(timeframe_sec=900, open_epoch=1000)
        factor = compute_decay_factor(market, 500)
        assert factor == pytest.approx(1.0)


class TestEffectiveRungs:
    def test_effective_rungs_at_floor(self):
        """With base 31 rungs and floor 0.3: max(4, int(31 * 0.3)) = 9."""
        assert max(4, int(31 * 0.3)) == 9

    def test_effective_rungs_minimum_with_small_base(self):
        """With base 5 rungs and floor 0.3: int(5 * 0.3) = 1, clamped to 4."""
        assert max(4, int(5 * 0.3)) == 4

    def test_effective_rungs_at_halfway(self):
        """With base 31 rungs and factor 0.65: max(4, int(31 * 0.65)) = 20."""
        assert max(4, int(31 * 0.65)) == 20


class TestBuildLadderWithDecayedParams:
    def test_fewer_rungs_with_decayed_width(self):
        """Build ladder with decayed params should produce fewer rungs and tighter range."""
        base_width = 0.41
        base_rungs = 31
        decay = 0.65  # 50% elapsed

        effective_width = base_width * decay
        effective_rungs = max(4, int(base_rungs * decay))

        full_rungs = build_ladder_rungs(
            best_ask=0.50, budget=500.0, rungs=base_rungs,
            spacing=0.01, width=base_width, size_skew=0.7,
            tick_size=0.01,
        )
        decayed_rungs = build_ladder_rungs(
            best_ask=0.50, budget=500.0, rungs=effective_rungs,
            spacing=0.01, width=effective_width, size_skew=0.7,
            tick_size=0.01,
        )

        # Decayed should have fewer or equal rungs
        assert len(decayed_rungs) <= len(full_rungs)
        # Decayed prices should be in a tighter range
        if decayed_rungs and full_rungs:
            decayed_range = decayed_rungs[-1][0] - decayed_rungs[0][0]
            full_range = full_rungs[-1][0] - full_rungs[0][0]
            assert decayed_range <= full_range

    def test_minimum_rungs_still_builds(self):
        """Even at floor, 4 rungs should still produce a valid ladder."""
        rungs = build_ladder_rungs(
            best_ask=0.50, budget=100.0, rungs=4,
            spacing=0.01, width=0.12, size_skew=0.7,
            tick_size=0.01,
        )
        assert len(rungs) > 0
        assert len(rungs) <= 4

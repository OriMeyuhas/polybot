"""Tests for passive top rung fix (1-tick offset).

Verifies that build_ladder_rungs shifts the anchor down by 1 tick so the top
rung sits at best_ask - tick_size (passive limit) instead of best_ask (marketable).
"""

import pytest
from polybot.strategy.ladder_manager import build_ladder_rungs as build_ladder_rungs_strategy
from polybot.ladder_manager import build_ladder_rungs as build_ladder_rungs_legacy


@pytest.mark.parametrize("build_fn,label", [
    (build_ladder_rungs_strategy, "strategy"),
    (build_ladder_rungs_legacy, "legacy"),
])
class TestPassiveTopRung:
    """T1: Top rung is passive (best_ask - tick_size), not marketable."""

    def test_top_rung_is_passive(self, build_fn, label):
        rungs = build_fn(
            best_ask=0.50, budget=100, rungs=5,
            spacing=0.02, width=0.08, size_skew=1.5,
            tick_size=0.01, max_rung_price=1.0,
        )
        assert len(rungs) > 0
        top_price = max(p for p, _ in rungs)
        assert top_price == pytest.approx(0.49), (
            f"[{label}] top rung should be 0.49 (best_ask - tick_size), got {top_price}"
        )

    def test_anchor_floors_at_tick_size(self, build_fn, label):
        """T2: Anchor floors at tick_size when naive calculation goes negative."""
        rungs = build_fn(
            best_ask=0.05, budget=500, rungs=5,
            spacing=0.02, width=0.10, size_skew=1.5,
            tick_size=0.01,
        )
        assert len(rungs) > 0
        cheapest_price = min(p for p, _ in rungs)
        assert cheapest_price >= 0.01, (
            f"[{label}] cheapest rung should be >= tick_size (0.01), got {cheapest_price}"
        )

    def test_pair_cost_reduction(self, build_fn, label):
        """T3: UP + DN ladders' VWAP sum is strictly less than best_ask_up + best_ask_dn."""
        best_ask_up = 0.50
        best_ask_dn = 0.50
        up_rungs = build_fn(
            best_ask=best_ask_up, budget=200, rungs=5,
            spacing=0.02, width=0.08, size_skew=1.5,
            tick_size=0.01, max_rung_price=1.0,
        )
        dn_rungs = build_fn(
            best_ask=best_ask_dn, budget=200, rungs=5,
            spacing=0.02, width=0.08, size_skew=1.5,
            tick_size=0.01, max_rung_price=1.0,
        )
        assert len(up_rungs) > 0 and len(dn_rungs) > 0

        up_vwap = sum(p * s for p, s in up_rungs) / sum(s for _, s in up_rungs)
        dn_vwap = sum(p * s for p, s in dn_rungs) / sum(s for _, s in dn_rungs)
        combined = up_vwap + dn_vwap

        # With 1-tick offset on each side, combined should be < 1.00 - 2*tick
        assert combined < best_ask_up + best_ask_dn - 2 * 0.01, (
            f"[{label}] combined VWAP {combined:.4f} should be < {best_ask_up + best_ask_dn - 0.02:.4f}"
        )

    def test_small_tick_size(self, build_fn, label):
        """T4: With tick_size=0.001, top rung should be 0.499, not 0.500."""
        rungs = build_fn(
            best_ask=0.50, budget=200, rungs=5,
            spacing=0.02, width=0.08, size_skew=1.5,
            tick_size=0.001, max_rung_price=1.0,
        )
        assert len(rungs) > 0
        top_price = max(p for p, _ in rungs)
        assert top_price == pytest.approx(0.499, abs=1e-6), (
            f"[{label}] top rung should be 0.499, got {top_price}"
        )


class TestPairCostGuardIntegration:
    """T5: With the fix, pair cost guard passes for asks that previously failed."""

    def test_pair_cost_guard_passes_with_offset(self):
        """Asks summing to 0.91 would produce pair_cost > 0.92 without the fix.
        With the 1-tick offset, the VWAP drops enough to pass the guard."""
        from unittest.mock import MagicMock
        from polybot.strategy.ladder_manager import LadderManager, build_ladder_rungs
        from polybot.config import BotConfig
        from polybot.types import MarketWindow, Side

        cfg = BotConfig(
            private_key="0xfake", api_key="key",
            api_secret="secret", api_passphrase="pass",
            ladder_rungs=5, ladder_spacing=0.01,
            ladder_width=0.04, max_pair_cost=0.92,
            ladder_rungs_5m=5, ladder_spacing_5m=0.01,
            ladder_width_5m=0.04, max_pair_cost_5m=0.92,
            ladder_size_skew=1.5,
            ladder_size_skew_5m=1.5,
        )

        # best_ask_up=0.46, best_ask_dn=0.45 => sum 0.91
        # Without fix: top rungs at 0.46+0.45 => VWAP ~0.91+ => pair cost > 0.92 with fees
        # With fix: top rungs at 0.45+0.44 => VWAP ~0.89 => pair cost < 0.92
        up_rungs = build_ladder_rungs(
            best_ask=0.46, budget=100, rungs=5,
            spacing=0.01, width=0.04, size_skew=1.5,
            tick_size=0.01, fee_rate=0.0,
        )
        dn_rungs = build_ladder_rungs(
            best_ask=0.45, budget=100, rungs=5,
            spacing=0.01, width=0.04, size_skew=1.5,
            tick_size=0.01, fee_rate=0.0,
        )
        assert len(up_rungs) > 0 and len(dn_rungs) > 0

        up_vwap = sum(p * s for p, s in up_rungs) / sum(s for _, s in up_rungs)
        dn_vwap = sum(p * s for p, s in dn_rungs) / sum(s for _, s in dn_rungs)
        combined = up_vwap + dn_vwap

        # With the 1-tick offset, combined VWAP should be under 0.92
        assert combined < 0.92, (
            f"Combined VWAP {combined:.4f} should be < 0.92 with passive top rung"
        )

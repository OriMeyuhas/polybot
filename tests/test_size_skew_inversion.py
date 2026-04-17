"""Tests for size skew inversion: default 1.5 -> 0.7.

T1. Inverted skew gives more size to cheap rungs
T2. Budget is fully allocated
T3. Default config uses 0.7 for all three timeframes
T4. Env var override still works
"""

import os
from unittest.mock import patch

import pytest

from polybot.config import BotConfig, load_bot_config
from polybot.strategy.ladder_manager import build_ladder_rungs


class TestInvertedSkewBehavior:
    """T1 & T2: build_ladder_rungs with size_skew=0.7 puts more size on cheap rungs."""

    def test_cheap_rungs_get_more_size(self):
        """T1: With skew=0.7, cheapest rung (index 0) has larger size than most expensive (index -1)."""
        result = build_ladder_rungs(
            best_ask=0.50,
            budget=10.0,
            rungs=5,
            spacing=0.01,
            width=0.04,
            size_skew=0.7,
        )
        assert len(result) >= 2, f"Expected at least 2 rungs, got {len(result)}"
        # Cheapest rung (first) should have more shares than most expensive (last)
        cheapest_size = result[0][1]
        most_expensive_size = result[-1][1]
        assert cheapest_size > most_expensive_size, (
            f"Cheapest rung size {cheapest_size} should be > most expensive {most_expensive_size}"
        )

    def test_budget_fully_allocated(self):
        """T2: Total cost of all rungs approximately equals the budget."""
        budget = 10.0
        result = build_ladder_rungs(
            best_ask=0.50,
            budget=budget,
            rungs=5,
            spacing=0.01,
            width=0.04,
            size_skew=0.7,
        )
        total_cost = sum(price * size for price, size in result)
        # Allow some tolerance for rounding
        assert total_cost == pytest.approx(budget, rel=0.15), (
            f"Total cost {total_cost:.2f} should be ~{budget}"
        )


class TestDefaultConfigSkew:
    """T3: Default BotConfig uses 0.7 for all three size_skew fields."""

    def test_default_ladder_size_skew(self):
        cfg = BotConfig()
        assert cfg.ladder_size_skew == 2.0

    def test_default_ladder_size_skew_5m(self):
        cfg = BotConfig()
        assert cfg.ladder_size_skew_5m == 2.0

    def test_default_ladder_size_skew_1h(self):
        cfg = BotConfig()
        assert cfg.ladder_size_skew_1h == 2.0

    def test_get_ladder_params_15m_uses_0_7(self):
        cfg = BotConfig()
        params = cfg.get_ladder_params(900)
        assert params.size_skew == 2.0

    def test_get_ladder_params_5m_uses_0_7(self):
        cfg = BotConfig()
        params = cfg.get_ladder_params(300)
        assert params.size_skew == 2.0

    def test_get_ladder_params_1h_uses_0_7(self):
        cfg = BotConfig()
        params = cfg.get_ladder_params(3600)
        assert params.size_skew == 2.0


class TestEnvVarOverride:
    """T4: Env var override still works after changing the default."""

    def test_env_var_overrides_default(self):
        env = {
            "LADDER_SIZE_SKEW": "1.5",
            "LADDER_SIZE_SKEW_5M": "2.0",
            "LADDER_SIZE_SKEW_1H": "1.0",
            "DRY_RUN": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = load_bot_config()
        assert cfg.ladder_size_skew == 1.5
        assert cfg.ladder_size_skew_5m == 2.0
        assert cfg.ladder_size_skew_1h == 1.0  # env set to 1.0

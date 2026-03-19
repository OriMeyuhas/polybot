"""Tests for the shared settlement resolution module."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from polybot.settlement import (
    resolve_via_clob,
    resolve_via_gamma,
    fetch_condition_id,
)


def _make_response(json_data, status_code=200):
    """Create a fake httpx.Response with the given JSON body."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://fake"),
    )
    return resp


# =========================================================================
# resolve_via_clob
# =========================================================================


class TestResolveViaClob:
    @pytest.mark.asyncio
    async def test_resolved_market(self):
        """CLOB returns a resolved market with a winning token."""
        data = {
            "resolved": True,
            "tokens": [
                {"outcome": "Up", "winner": True},
                {"outcome": "Down", "winner": False},
            ],
        }
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _make_response(data)

        result = await resolve_via_clob(client, "https://clob.example.com", "0xabc123")

        assert result is not None
        assert result["outcome"] == "UP"
        assert result["settlement_price"] == 1.0
        client.get.assert_called_once_with(
            "https://clob.example.com/markets/0xabc123", timeout=10
        )

    @pytest.mark.asyncio
    async def test_unresolved_market(self):
        """CLOB returns an unresolved market — should return None."""
        data = {
            "resolved": False,
            "tokens": [
                {"outcome": "Up", "winner": False},
                {"outcome": "Down", "winner": False},
            ],
        }
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _make_response(data)

        result = await resolve_via_clob(client, "https://clob.example.com", "0xabc123")

        assert result is None

    @pytest.mark.asyncio
    async def test_resolved_via_winner_field(self):
        """CLOB returns resolved with a top-level winner field (no token winners)."""
        data = {
            "resolved": True,
            "tokens": [],
            "winner": "Down",
        }
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _make_response(data)

        result = await resolve_via_clob(client, "https://clob.example.com", "0xabc123")

        assert result is not None
        assert result["outcome"] == "DOWN"


# =========================================================================
# resolve_via_gamma
# =========================================================================


class TestResolveViaGamma:
    @pytest.mark.asyncio
    async def test_resolved_with_outcome_prices(self):
        """Gamma returns a resolved market with outcomePrices."""
        data = [
            {
                "markets": [
                    {
                        "resolved": True,
                        "outcomes": json.dumps(["Up", "Down"]),
                        "outcomePrices": json.dumps(["1", "0"]),
                    }
                ]
            }
        ]
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _make_response(data)

        result = await resolve_via_gamma(client, "btc-up-or-down-test")

        assert result is not None
        assert result["outcome"] == "UP"
        assert result["settlement_price"] == 1.0

    @pytest.mark.asyncio
    async def test_unresolved(self):
        """Gamma returns an unresolved market — should return None."""
        data = [
            {
                "markets": [
                    {
                        "resolved": False,
                        "outcomes": json.dumps(["Up", "Down"]),
                        "outcomePrices": json.dumps(["0.5", "0.5"]),
                    }
                ]
            }
        ]
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _make_response(data)

        result = await resolve_via_gamma(client, "btc-up-or-down-test")

        assert result is None


# =========================================================================
# fetch_condition_id
# =========================================================================


class TestFetchConditionId:
    @pytest.mark.asyncio
    async def test_success(self):
        """Gamma returns a conditionId for the given slug."""
        data = [
            {
                "markets": [
                    {"conditionId": "0xdeadbeef123"},
                ]
            }
        ]
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _make_response(data)

        result = await fetch_condition_id(client, "btc-up-or-down-test")

        assert result == "0xdeadbeef123"

    @pytest.mark.asyncio
    async def test_failure_returns_empty(self):
        """On HTTP error, returns empty string."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=httpx.Request("GET", "https://fake"),
            response=httpx.Response(404, request=httpx.Request("GET", "https://fake")),
        )

        result = await fetch_condition_id(client, "nonexistent-slug")

        assert result == ""

    @pytest.mark.asyncio
    async def test_no_markets_returns_empty(self):
        """Gamma returns events with no markets — returns empty string."""
        data = [{"markets": []}]
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _make_response(data)

        result = await fetch_condition_id(client, "empty-slug")

        assert result == ""

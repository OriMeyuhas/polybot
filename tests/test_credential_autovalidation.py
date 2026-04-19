"""Tests for the credential validation helper and the automatic validation
performed by POST /api/config.

Covers:
- _validate_credentials success / failure / timeout paths
- Rollback-on-failure: a rejected save must not overwrite existing .env creds
- Skip validation when no credential changed or when the full set isn't present
- handle_test_connection delegating to the shared helper
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import AioHTTPTestCase

import polybot.web.server as server_mod
from polybot.web.server import (
    _validate_credentials,
    _validate_credentials_sync,
    create_app,
)
from polybot.web.state import GuiStateHolder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_env_live(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DRY_RUN=false\nBANKROLL=1000\n", encoding="utf-8")
    monkeypatch.setattr(server_mod, "_ENV_FILE", env_file)
    return env_file


@pytest.fixture
def tmp_env_paper(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DRY_RUN=true\nBANKROLL=1000\n", encoding="utf-8")
    monkeypatch.setattr(server_mod, "_ENV_FILE", env_file)
    return env_file


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestValidateCredentialsSync:
    """The sync helper is the core — everything else delegates to it."""

    def test_success_returns_balance(self, monkeypatch):
        class _FakeClobClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_balance_allowance(self, params):
                return {"balance": "5000000"}

        import py_clob_client_v2.client as cc
        monkeypatch.setattr(cc, "ClobClient", _FakeClobClient)

        ok, balance, err = _validate_credentials_sync("0xkey", "ak", "as", "ap")
        assert ok is True
        assert balance == 5.0
        assert err is None

    def test_missing_balance_field_fails(self, monkeypatch):
        class _FakeClobClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_balance_allowance(self, params):
                return {}  # no balance field

        import py_clob_client_v2.client as cc
        monkeypatch.setattr(cc, "ClobClient", _FakeClobClient)

        ok, balance, err = _validate_credentials_sync("0xkey", "ak", "as", "ap")
        assert ok is False
        assert balance is None
        assert err and "no balance" in err.lower()

    def test_exception_reported_as_error(self, monkeypatch):
        class _FakeClobClient:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("bad key")

        import py_clob_client_v2.client as cc
        monkeypatch.setattr(cc, "ClobClient", _FakeClobClient)

        ok, balance, err = _validate_credentials_sync("0xkey", "ak", "as", "ap")
        assert ok is False
        assert err == "bad key"

    def test_secret_is_redacted_in_error(self, monkeypatch):
        """If the SDK echoes credentials in its exception we MUST redact them."""
        secret = "supersecret123"

        class _FakeClobClient:
            def __init__(self, *args, **kwargs):
                raise RuntimeError(f"unauthorized: creds were {secret}")

        import py_clob_client_v2.client as cc
        monkeypatch.setattr(cc, "ClobClient", _FakeClobClient)

        ok, balance, err = _validate_credentials_sync("0xkey", "ak", secret, "ap")
        assert ok is False
        assert secret not in (err or "")
        assert "<redacted>" in err


class TestValidateCredentialsAsync:
    @pytest.mark.asyncio
    async def test_async_success(self, monkeypatch):
        class _FakeClobClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_balance_allowance(self, params):
                return {"balance": "2500000"}

        import py_clob_client_v2.client as cc
        monkeypatch.setattr(cc, "ClobClient", _FakeClobClient)

        ok, balance, err = await _validate_credentials("0xkey", "ak", "as", "ap", timeout=5)
        assert ok is True
        assert balance == 2.5

    @pytest.mark.asyncio
    async def test_async_timeout_returns_error(self, monkeypatch):
        # Patch to_thread on the aliased asyncio inside server_mod so wait_for
        # sees a slow coroutine.
        async def slow_to_thread(fn, *args, **kwargs):
            await asyncio.sleep(5)
            return fn(*args, **kwargs)

        monkeypatch.setattr(server_mod.asyncio, "to_thread", slow_to_thread)

        ok, balance, err = await _validate_credentials("k", "k", "k", "k", timeout=0.05)
        assert ok is False
        assert balance is None
        assert err and "timed out" in err.lower()


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


class _BaseCase(AioHTTPTestCase):
    running_mode: str = "dry_run"

    async def get_application(self):
        state = GuiStateHolder()
        state.update(mode=self.running_mode)
        return create_app(
            state=state, start_fn=None, stop_fn=None, restart_fn=None
        )


class TestPostConfigRollback(_BaseCase):
    running_mode = "live"

    @pytest.fixture(autouse=True)
    def _wire(self, tmp_env_live, monkeypatch):
        # Seed existing valid creds so rollback has something to restore to.
        tmp_env_live.write_text(
            "DRY_RUN=false\n"
            "BANKROLL=1000\n"
            "PRIVATE_KEY=0xprev\n"
            "API_KEY=prev_ak\n"
            "API_SECRET=prev_as\n"
            "API_PASSPHRASE=prev_ap\n",
            encoding="utf-8",
        )
        self._tmp_env = tmp_env_live
        self._monkeypatch = monkeypatch
        yield

    async def test_invalid_creds_rejected_and_rolled_back(self):
        async def fake_validate(pk, ak, asec, ap, timeout=10.0):
            return False, None, "401 Unauthorized"

        self._monkeypatch.setattr(server_mod, "_validate_credentials", fake_validate)

        resp = await self.client.post("/api/config", json={
            "private_key": "0xbad",
            "api_key": "bad_ak",
            "api_secret": "bad_as",
            "api_passphrase": "bad_ap",
        })
        data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is False
        assert data["saved"] is False
        assert "401" in data["error"] or "invalid" in data["error"].lower()

        # .env must still contain the ORIGINAL creds.
        content = self._tmp_env.read_text(encoding="utf-8")
        assert "PRIVATE_KEY=0xprev" in content
        assert "API_KEY=prev_ak" in content
        assert "API_SECRET=prev_as" in content
        assert "API_PASSPHRASE=prev_ap" in content
        # and NOT the bad ones
        assert "0xbad" not in content
        assert "bad_ak" not in content

    async def test_valid_creds_persisted_and_balance_returned(self):
        async def fake_validate(pk, ak, asec, ap, timeout=10.0):
            return True, 42.50, None

        self._monkeypatch.setattr(server_mod, "_validate_credentials", fake_validate)

        resp = await self.client.post("/api/config", json={
            "private_key": "0xnew",
            "api_key": "new_ak",
            "api_secret": "new_as",
            "api_passphrase": "new_ap",
        })
        data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is True
        assert data["saved"] is True
        assert data["validated"] is True
        assert data["balance"] == 42.50

        content = self._tmp_env.read_text(encoding="utf-8")
        assert "PRIVATE_KEY=0xnew" in content
        assert "API_KEY=new_ak" in content


class TestPostConfigSkipsValidation(_BaseCase):
    running_mode = "dry_run"

    @pytest.fixture(autouse=True)
    def _wire(self, tmp_env_paper, monkeypatch):
        self._tmp_env = tmp_env_paper
        self._monkeypatch = monkeypatch
        self._validate_calls = []

        async def spy_validate(pk, ak, asec, ap, timeout=10.0):
            self._validate_calls.append((pk, ak, asec, ap))
            return True, 1.0, None

        monkeypatch.setattr(server_mod, "_validate_credentials", spy_validate)
        yield

    async def test_empty_body_does_not_validate(self):
        resp = await self.client.post("/api/config", json={})
        data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is True
        assert data["saved"] is False
        assert len(self._validate_calls) == 0

    async def test_partial_creds_not_validated(self):
        # Only submit 2 of 4. Merged .env still won't be complete.
        resp = await self.client.post("/api/config", json={
            "private_key": "0xone", "api_key": "two"
        })
        data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is True
        assert data["saved"] is True
        assert data.get("validated") is False
        assert len(self._validate_calls) == 0

    async def test_completing_set_triggers_validation(self):
        # Seed 3 existing creds, submit only the 4th — merged set is complete.
        self._tmp_env.write_text(
            "DRY_RUN=true\n"
            "PRIVATE_KEY=0xexisting\n"
            "API_KEY=existing_ak\n"
            "API_SECRET=existing_as\n",
            encoding="utf-8",
        )
        resp = await self.client.post("/api/config", json={
            "api_passphrase": "final_ap"
        })
        data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is True
        assert data["saved"] is True
        assert data["validated"] is True
        assert len(self._validate_calls) == 1
        # All four creds made it to the validator.
        assert self._validate_calls[0] == (
            "0xexisting", "existing_ak", "existing_as", "final_ap"
        )


class TestTestConnectionDelegates(_BaseCase):
    running_mode = "live"

    @pytest.fixture(autouse=True)
    def _wire(self, tmp_env_live, monkeypatch):
        tmp_env_live.write_text(
            "DRY_RUN=false\n"
            "PRIVATE_KEY=0xkey\n"
            "API_KEY=ak\n"
            "API_SECRET=as\n"
            "API_PASSPHRASE=ap\n",
            encoding="utf-8",
        )
        self._tmp_env = tmp_env_live
        self._monkeypatch = monkeypatch
        self._calls = []

        async def fake_validate(pk, ak, asec, ap, timeout=10.0):
            self._calls.append((pk, ak, asec, ap))
            return True, 123.45, None

        monkeypatch.setattr(server_mod, "_validate_credentials", fake_validate)
        yield

    async def test_test_connection_delegates(self):
        resp = await self.client.post("/api/test-connection")
        data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is True
        assert data["balance"] == 123.45
        assert self._calls == [("0xkey", "ak", "as", "ap")]

    async def test_test_connection_reports_missing(self):
        # Wipe creds.
        self._tmp_env.write_text("DRY_RUN=false\n", encoding="utf-8")
        resp = await self.client.post("/api/test-connection")
        data = await resp.json()
        assert data["ok"] is False
        assert "missing" in data["error"].lower()

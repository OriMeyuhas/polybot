"""Tests for POST /api/restart-reset — paper-only clean-slate restart.

Archives settlement + activity logs, reseeds .env BANKROLL from
DRY_RUN_BANKROLL, then delegates to the normal restart flow. Must refuse
when DRY_RUN=false so live history is never wiped.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import AioHTTPTestCase

import polybot.web.server as server_mod
from polybot.web.server import create_app, _archive_paper_logs
from polybot.web.state import GuiStateHolder


@pytest.fixture
def tmp_project(tmp_path, monkeypatch):
    """Set up an isolated project root with .env and data/ dir."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DRY_RUN=true\nBANKROLL=500\nDRY_RUN_BANKROLL=7500\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Seed some realistic cumulative paper logs.
    (data_dir / "settlement_log.jsonl").write_text(
        '{"ts":1,"pnl":1.0}\n{"ts":2,"pnl":-0.5}\n', encoding="utf-8"
    )
    (data_dir / "activity_log.jsonl").write_text(
        '{"ts":1,"event":"foo"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(server_mod, "_ENV_FILE", env_file)
    return tmp_path


class _BaseCase(AioHTTPTestCase):
    restart_fn = None

    async def get_application(self):
        state = GuiStateHolder()
        state.update(mode="dry_run")
        return create_app(
            state=state,
            start_fn=None,
            stop_fn=None,
            restart_fn=self.restart_fn,
        )


class TestRestartResetArchivesAndReseeds(_BaseCase):
    _restart_called: list = []

    @pytest.fixture(autouse=True)
    def _wire(self, tmp_project):
        type(self)._project = tmp_project
        type(self)._restart_called = []

        async def _fake_restart():
            type(self)._restart_called.append(True)

        type(self).restart_fn = staticmethod(_fake_restart)
        yield

    async def test_happy_path(self):
        resp = await self.client.post("/api/restart-reset")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        # Both logs should have been archived.
        archived = set(data["archived"])
        assert archived == {"settlement_log.jsonl", "activity_log.jsonl"}
        # Bankroll seed should come from DRY_RUN_BANKROLL=7500 in .env.
        assert data["bankroll_seed"] == 7500.0

        # Originals gone, .bak files present.
        data_dir: Path = type(self)._project / "data"
        assert not (data_dir / "settlement_log.jsonl").exists()
        assert not (data_dir / "activity_log.jsonl").exists()
        bak_files = list(data_dir.glob("*.bak"))
        assert len(bak_files) == 2

        # .env BANKROLL was rewritten to the seed value.
        env_content = (type(self)._project / ".env").read_text(encoding="utf-8")
        assert "BANKROLL=7500" in env_content
        # DRY_RUN must stay true — we never flip mode in the reset path.
        assert "DRY_RUN=true" in env_content

        # restart_fn fires in a background task.
        await asyncio.sleep(0.4)
        assert type(self)._restart_called == [True]


class TestRestartResetRefusesLive(_BaseCase):
    @pytest.fixture(autouse=True)
    def _wire(self, tmp_project):
        # Flip .env to live mode before the test runs.
        (tmp_project / ".env").write_text(
            "DRY_RUN=false\nBANKROLL=500\nDRY_RUN_BANKROLL=7500\n",
            encoding="utf-8",
        )
        type(self)._project = tmp_project

        async def _fake_restart():
            raise AssertionError("restart_fn must NOT fire in live mode")

        type(self).restart_fn = staticmethod(_fake_restart)
        yield

    async def test_refuses_when_dry_run_is_false(self):
        resp = await self.client.post("/api/restart-reset")
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False
        assert "live" in data["error"].lower()

        # Originals must NOT be archived.
        data_dir: Path = type(self)._project / "data"
        assert (data_dir / "settlement_log.jsonl").exists()
        assert (data_dir / "activity_log.jsonl").exists()


class TestRestartResetUnwired(_BaseCase):
    @pytest.fixture(autouse=True)
    def _wire(self, tmp_project):
        type(self).restart_fn = None
        yield

    async def test_returns_400_when_restart_fn_unwired(self):
        resp = await self.client.post("/api/restart-reset")
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False


class TestRestartResetMissingDryRunBankroll(_BaseCase):
    """Default seed is 10000 when DRY_RUN_BANKROLL is absent from .env."""

    _restart_called: list = []

    @pytest.fixture(autouse=True)
    def _wire(self, tmp_project):
        (tmp_project / ".env").write_text(
            "DRY_RUN=true\nBANKROLL=500\n",  # no DRY_RUN_BANKROLL
            encoding="utf-8",
        )
        type(self)._restart_called = []

        async def _fake_restart():
            type(self)._restart_called.append(True)

        type(self).restart_fn = staticmethod(_fake_restart)
        yield

    async def test_default_seed_is_10000(self):
        resp = await self.client.post("/api/restart-reset")
        assert resp.status == 200
        data = await resp.json()
        assert data["bankroll_seed"] == 10000.0


class TestArchiveHelperSkipsMissingFiles:
    """_archive_paper_logs must not fail on a missing file — returns only the
    ones that existed. Ensures a fresh install (no logs yet) still restarts."""

    def test_missing_files_skipped(self, tmp_path):
        result = _archive_paper_logs(tmp_path, now_ts=123)
        assert result == []

    def test_only_existing_files_archived(self, tmp_path):
        (tmp_path / "settlement_log.jsonl").write_text("{}\n", encoding="utf-8")
        # activity_log.jsonl missing
        result = _archive_paper_logs(tmp_path, now_ts=123)
        assert result == ["settlement_log.jsonl"]
        assert (tmp_path / "settlement_log.jsonl.123.bak").exists()

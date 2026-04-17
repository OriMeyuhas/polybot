"""Tests for mode-restart UX: /api/settings, /api/config, /api/restart.

These endpoints deliberately require a bot restart to apply mode changes
(paper <-> live) or newly-saved credentials. The HTTP response must tell
the frontend when a restart is needed so it can prompt the user.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import AioHTTPTestCase

import polybot.web.server as server_mod
from polybot.web.server import create_app, _configured_mode, _env_dry_run
from polybot.web.state import GuiStateHolder


def _write_env(tmp_env: Path, content: str) -> None:
    tmp_env.write_text(content, encoding="utf-8")


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Point the server module's _ENV_FILE at a tmp file for the test."""
    env_file = tmp_path / ".env"
    env_file.write_text("DRY_RUN=true\nBANKROLL=1000\n", encoding="utf-8")
    monkeypatch.setattr(server_mod, "_ENV_FILE", env_file)
    return env_file


class _ModeTestCase(AioHTTPTestCase):
    """Base class: exposes helpers for test subclasses that need a real app."""

    # Subclasses must set these before get_application() is invoked.
    running_mode: str = "dry_run"
    env_file: Path | None = None
    restart_fn = None

    async def get_application(self):
        state = GuiStateHolder()
        state.update(mode=self.running_mode)
        app = create_app(
            state=state,
            start_fn=None,
            stop_fn=None,
            restart_fn=self.restart_fn,
        )
        return app


class TestConfiguredModeHelpers:
    """Pure helpers — no aiohttp needed."""

    def test_env_dry_run_true_when_true(self, tmp_env):
        tmp_env.write_text("DRY_RUN=true\n", encoding="utf-8")
        assert _env_dry_run() is True
        assert _configured_mode() == "dry_run"

    def test_env_dry_run_false_when_false(self, tmp_env):
        tmp_env.write_text("DRY_RUN=false\n", encoding="utf-8")
        assert _env_dry_run() is False
        assert _configured_mode() == "live"

    def test_env_dry_run_defaults_true_when_missing(self, tmp_env):
        tmp_env.write_text("BANKROLL=500\n", encoding="utf-8")
        assert _env_dry_run() is True
        assert _configured_mode() == "dry_run"


class TestPostSettingsRestartSignal(_ModeTestCase):
    """POST /api/settings must report restart_required iff .env mode diverges
    from the actually-running bot mode."""

    running_mode = "dry_run"

    @pytest.fixture(autouse=True)
    def _wire_env(self, tmp_env):
        type(self).env_file = tmp_env
        yield

    async def test_saving_live_mode_while_running_paper_signals_restart(self):
        resp = await self.client.post("/api/settings", json={"dry_run": False})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["restart_required"] is True
        # .env was updated
        assert _env_dry_run() is False

    async def test_saving_paper_mode_while_running_paper_no_restart(self):
        resp = await self.client.post("/api/settings", json={"dry_run": True})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["restart_required"] is False


class TestPostConfigRestartSignal(_ModeTestCase):
    running_mode = "dry_run"

    @pytest.fixture(autouse=True)
    def _wire_env(self, tmp_env, monkeypatch):
        type(self).env_file = tmp_env
        # /api/config now auto-validates credentials when all 4 are present.
        # Stub the validator so this test (about restart-signal semantics)
        # doesn't need network access.
        async def _fake_validate(pk, ak, asec, ap, timeout=10.0):
            return True, 99.99, None
        monkeypatch.setattr(server_mod, "_validate_credentials", _fake_validate)
        yield

    async def test_saving_credentials_signals_restart(self):
        resp = await self.client.post(
            "/api/config",
            json={
                "private_key": "0xabc",
                "api_key": "k",
                "api_secret": "s",
                "api_passphrase": "p",
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["restart_required"] is True
        assert data["saved"] is True

    async def test_empty_post_no_restart_when_modes_match(self):
        # .env is dry_run=true, bot running as dry_run, no creds posted.
        resp = await self.client.post("/api/config", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["restart_required"] is False
        assert data["saved"] is False


class TestConfiguredModeInState(_ModeTestCase):
    running_mode = "dry_run"

    @pytest.fixture(autouse=True)
    def _wire_env(self, tmp_env):
        type(self).env_file = tmp_env
        yield

    async def test_state_exposes_configured_mode_matching(self):
        resp = await self.client.get("/api/state")
        data = await resp.json()
        assert data["mode"] == "dry_run"
        assert data["configured_mode"] == "dry_run"

    async def test_state_exposes_configured_mode_mismatch(self):
        # Flip .env to live while bot is still dry_run.
        type(self).env_file.write_text("DRY_RUN=false\n", encoding="utf-8")
        resp = await self.client.get("/api/state")
        data = await resp.json()
        assert data["mode"] == "dry_run"
        assert data["configured_mode"] == "live"


class TestRestartEndpoint(_ModeTestCase):
    running_mode = "dry_run"
    _restart_called: list = []

    @pytest.fixture(autouse=True)
    def _wire(self, tmp_env):
        type(self).env_file = tmp_env
        type(self)._restart_called = []

        async def _fake_restart():
            type(self)._restart_called.append(True)

        # Wrap in staticmethod so accessing through self doesn't bind it.
        type(self).restart_fn = staticmethod(_fake_restart)
        yield

    async def test_restart_returns_ok_and_invokes_restart_fn(self):
        resp = await self.client.post("/api/restart")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["status"] == "restarting"
        # The handler schedules restart_fn in a background task; give it a moment.
        await asyncio.sleep(0.4)
        assert type(self)._restart_called == [True]


class TestRestartEndpointUnwired(_ModeTestCase):
    running_mode = "dry_run"

    @pytest.fixture(autouse=True)
    def _wire(self, tmp_env):
        type(self).env_file = tmp_env
        type(self).restart_fn = None
        yield

    async def test_restart_without_wiring_returns_400(self):
        resp = await self.client.post("/api/restart")
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False


class TestRestartProcessOrdering:
    """Verify _restart_bot_process stops, spawns, then exits — in that order."""

    @pytest.mark.asyncio
    async def test_stop_then_spawn_then_exit(self):
        from run_bot import _restart_bot_process

        events: list[str] = []

        async def fake_stop():
            events.append("stop")

        def fake_popen(argv, cwd=None, close_fds=None, env=None):
            events.append("spawn")
            # Validate the args we promise in the docstring.
            assert argv[0].endswith(("python", "python.exe")) or "python" in argv[0].lower()
            assert close_fds is False
            # Restart must tell the child to skip the interactive CONFIRM prompt,
            # otherwise a live-mode restart hangs on stdin.
            assert env is not None and env.get("POLYBOT_SKIP_CONFIRM") == "1"
            return MagicMock()

        def fake_exit(status):
            events.append(f"exit:{status}")
            # Raise to break out, since real os._exit would terminate.
            raise SystemExit(status)

        async def fake_sleep(_secs):
            events.append("sleep")

        with pytest.raises(SystemExit):
            await _restart_bot_process(
                fake_stop,
                argv=["python", "run_bot.py"],
                _popen=fake_popen,
                _exit=fake_exit,
                _sleep=fake_sleep,
            )

        assert events == ["stop", "spawn", "sleep", "exit:0"]

    @pytest.mark.asyncio
    async def test_stop_exception_does_not_block_spawn(self):
        from run_bot import _restart_bot_process

        events: list[str] = []

        async def failing_stop():
            events.append("stop")
            raise RuntimeError("stop failed")

        def fake_popen(argv, cwd=None, close_fds=None, env=None):
            events.append("spawn")
            return MagicMock()

        def fake_exit(status):
            events.append(f"exit:{status}")
            raise SystemExit(status)

        async def fake_sleep(_secs):
            pass

        with pytest.raises(SystemExit):
            await _restart_bot_process(
                failing_stop,
                argv=["python", "run_bot.py"],
                _popen=fake_popen,
                _exit=fake_exit,
                _sleep=fake_sleep,
            )

        # Must still spawn + exit even if stop raised — otherwise a wedged
        # shutdown would leave the old process wedged instead of relaunching.
        assert events == ["stop", "spawn", "exit:0"]

    @pytest.mark.asyncio
    async def test_popen_failure_skips_exit(self):
        """If spawn fails, do NOT exit — we need the old process alive so the
        user can fix whatever prevented the relaunch."""
        from run_bot import _restart_bot_process

        events: list[str] = []

        async def fake_stop():
            events.append("stop")

        def failing_popen(argv, cwd=None, close_fds=None, env=None):
            events.append("spawn-attempt")
            raise OSError("disk full")

        def fake_exit(status):
            events.append(f"exit:{status}")  # must not be called

        async def fake_sleep(_secs):
            events.append("sleep")

        await _restart_bot_process(
            fake_stop,
            argv=["python", "run_bot.py"],
            _popen=failing_popen,
            _exit=fake_exit,
            _sleep=fake_sleep,
        )

        assert events == ["stop", "spawn-attempt"]

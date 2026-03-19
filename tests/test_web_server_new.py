import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from polybot.web.server import create_app
from polybot.web.state import GuiStateHolder


class TestWebServer(AioHTTPTestCase):
    async def get_application(self):
        state = GuiStateHolder()
        return create_app(state=state, start_fn=None, stop_fn=None)

    @unittest_run_loop
    async def test_status_endpoint(self):
        resp = await self.client.request("GET", "/api/state")
        assert resp.status == 200
        data = await resp.json()
        assert "mode" in data
        assert "running" in data

    @unittest_run_loop
    async def test_static_files(self):
        resp = await self.client.request("GET", "/")
        assert resp.status == 200

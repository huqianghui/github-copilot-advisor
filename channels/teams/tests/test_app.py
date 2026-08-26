# channels/teams/tests/test_app.py
import teams_adapter.app as app_module
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from microsoft_agents.hosting.core.authorization.agent_auth_configuration import (
    AgentAuthConfiguration,
)
from teams_adapter.app import create_app


def _anon_config() -> AgentAuthConfiguration:
    # 匿名允许 → JWT 装饰器无 Authorization 头也放行(本地/emulator 同款路径)。
    return AgentAuthConfiguration(
        client_id="test", tenant_id="test", anonymous_allowed=True,
    )


async def test_healthz_ok_without_auth():
    app = create_app(object(), object(), _anon_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert await resp.text() == "ok"


async def test_messages_routes_to_start_agent_process(monkeypatch):
    seen = {}

    async def fake_start(request, agent_application, adapter):
        seen["agent_app"] = agent_application
        seen["adapter"] = adapter
        return web.Response(status=201)

    monkeypatch.setattr(app_module, "start_agent_process", fake_start)

    sentinel_app, sentinel_adapter = object(), object()
    app = create_app(sentinel_app, sentinel_adapter, _anon_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/messages", json={"type": "message"})
        assert resp.status == 201
    assert seen["agent_app"] is sentinel_app
    assert seen["adapter"] is sentinel_adapter

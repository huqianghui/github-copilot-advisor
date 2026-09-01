# channels/teams/tests/test_app.py
import logging

import teams_adapter.app as app_module
import teams_adapter.__main__ as main_module
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import AgentApplication, MemoryStorage, TurnState
from microsoft_agents.hosting.core.authorization.agent_auth_configuration import (
    AgentAuthConfiguration,
)
from teams_adapter.app import create_app


def _anon_config() -> AgentAuthConfiguration:
    # 匿名允许 → JWT 装饰器无 Authorization 头也放行(本地/emulator 同款路径)。
    return AgentAuthConfiguration(
        client_id="test", tenant_id="test", anonymous_allowed=True,
    )


def test_build_agent_app_uses_explicit_authorization(monkeypatch):
    config = load_configuration_from_env({
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED": "True",
    })
    monkeypatch.setattr(
        main_module, "load_configuration_from_env", lambda _environ: config)
    monkeypatch.setattr(
        main_module, "build_advisor", lambda channel_name: object())

    agent_app, adapter, agent_configuration = main_module.build_agent_app()

    assert isinstance(agent_app, AgentApplication)
    assert isinstance(adapter, CloudAdapter)
    assert (
        agent_app.connection_manager.get_default_connection_configuration()
        is agent_configuration
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


async def test_valid_teams_activity_reaches_agent_through_real_adapter():
    config = load_configuration_from_env({
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED": "True",
    })
    connection_manager = MsalConnectionManager(**config)
    adapter = CloudAdapter(connection_manager=connection_manager)
    agent_app = AgentApplication[TurnState](
        storage=MemoryStorage(),
        adapter=adapter,
        connection_manager=connection_manager,
    )
    seen = []

    @agent_app.activity("message")
    async def on_message(context, _state):
        seen.append(context.activity.id)

    app = create_app(
        agent_app,
        adapter,
        connection_manager.get_default_connection_configuration(),
    )
    activity = {
        "type": "message",
        "id": "activity-1",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "conversation": {
            "id": "19:test",
            "conversationType": "channel",
        },
        "recipient": {"id": "28:bot"},
        "from": {"id": "29:user"},
        "text": "hello",
    }

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/messages", json=activity)

    assert resp.status == 202
    assert seen == ["activity-1"]


async def test_sdk_rejection_logs_reason_without_activity_body(
        monkeypatch, caplog):
    async def fake_start(_request, _agent_application, _adapter):
        return web.json_response(
            {"error": "Activity must have type and conversation.id"},
            status=400,
        )

    monkeypatch.setattr(app_module, "start_agent_process", fake_start)
    app = create_app(object(), object(), _anon_config())

    with caplog.at_level(logging.WARNING, logger="teams_adapter.app"):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/messages",
                json={"type": "message", "text": "sensitive message"},
            )

    assert resp.status == 400
    assert "Activity must have type and conversation.id" in caplog.text
    assert "sensitive message" not in caplog.text

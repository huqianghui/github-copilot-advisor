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


def test_agent_app_wires_image_downloader(monkeypatch):
    """装配断言:AgentApplication 必须拿到 TeamsImageDownloader。

    stub 的形状是实测出来的:AgentApplication 会读 authorization.connection_manager
    (agent_application.py:186),authorization 为 None 时又会要求显式
    connection_manager,所以两者都必须是有属性的对象,不能用裸 object()。
    """
    import teams_adapter.__main__ as entry
    from teams_adapter.downloader import TeamsImageDownloader

    class StubConnectionManager:
        def get_default_connection_configuration(self):
            return {}

    stub_cm = StubConnectionManager()

    class StubAuthorization:
        connection_manager = stub_cm

    monkeypatch.setattr(entry, "load_configuration_from_env", lambda env: {})
    monkeypatch.setattr(entry, "MsalConnectionManager", lambda **_: stub_cm)
    monkeypatch.setattr(entry, "CloudAdapter", lambda **_: None)
    monkeypatch.setattr(entry, "Authorization",
                        lambda *a, **k: StubAuthorization())
    monkeypatch.setattr(entry, "build_advisor", lambda channel_name: object())

    agent_app, _, _ = entry.build_agent_app()
    downloaders = agent_app._options.file_downloaders
    assert len(downloaders) == 1
    assert isinstance(downloaders[0], TeamsImageDownloader)
    # 光有 isinstance 判别不出装配是否真的通电:TeamsImageDownloader(None)
    # 同样满足上面两条断言,但运行期 _access_token 会 AttributeError,被
    # download_files 的 except Exception 吞掉,每张图静默丢弃 —— 变异实测存活。
    # 所以必须钉住它拿到的正是本次构造出的 connection_manager。
    assert downloaders[0]._connection_manager is stub_cm


def test_openai_logger_pinned_to_info():
    """OPENAI_LOG=debug 会 dump 含图片 base64 的请求体,必须在启动时钉死。"""
    import logging

    import teams_adapter.__main__ as entry

    logging.getLogger("openai").setLevel(logging.DEBUG)   # 模拟环境变量效果
    entry._configure_logging()
    assert logging.getLogger("openai").level == logging.INFO


def test_app_log_level_applies_even_when_root_already_has_a_handler():
    """OPENAI_LOG 一设,openai 就在**导入期**通过 _basic_config() 给 root 装了
    handler。CPython 的 logging.basicConfig 里,level 的赋值在
    `if len(root.handlers) == 0` 分支内 —— root 已有 handler 时它提前返回,
    level 根本没应用(实测:root 停在 WARNING)。

    后果很讽刺:运维为排查图片问题去开 OPENAI_LOG,会**同时**丢掉
    teams_adapter.bot 的逐消息遥测和 advisor_agent.core 的 advisor_event
    审计行 —— 带 image_count 的正是后者,正是他要看的东西。

    上面的 test_openai_logger_pinned_to_info 抓不到这个:它用直接 setLevel
    模拟环境变量,绕过了"装 handler"这个副作用,而副作用才是致病的那一半。
    所以这里必须真的把 root 置成"已有 handler"的状态再调 —— 假协作者太简单
    正是这条缺陷躲过一整轮测试的原因。

    断言落在业务 logger 的 isEnabledFor 上,而不只是 root.level:
    前者才是"这条日志到底出不出得来"。
    """
    import logging

    import teams_adapter.__main__ as entry

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        # 模拟 openai 导入期 _basic_config() 的效果:root 上已有 handler,
        # 且 level 还是默认的 WARNING
        root.handlers = [logging.StreamHandler()]
        root.setLevel(logging.WARNING)

        entry._configure_logging()

        assert root.level == logging.INFO
        assert logging.getLogger("teams_adapter.bot").isEnabledFor(logging.INFO)
        assert logging.getLogger("advisor_agent.core").isEnabledFor(logging.INFO)
    finally:
        root.handlers, root.level = saved_handlers, saved_level


def test_main_activates_the_openai_logger_pin(monkeypatch):
    """防线必须真的被启动路径激活 —— 只证明 _configure_logging 有效还不够。
    断言的是结果(logger 级别),不是"某个函数被调用过"。"""
    import logging

    import teams_adapter.__main__ as entry

    monkeypatch.setattr(entry, "build_agent_app", lambda: (None, None, None))
    monkeypatch.setattr(entry, "create_app", lambda *a: None)
    monkeypatch.setattr(entry.web, "run_app", lambda *a, **k: None)

    logging.getLogger("openai").setLevel(logging.DEBUG)   # 模拟 OPENAI_LOG=debug
    entry.main()
    assert logging.getLogger("openai").level == logging.INFO


def test_configure_logging_is_idempotent():
    """原先无条件 addHandler,每调一次就多挂一个 handler、日志多打一行。
    main() 只跑一次时无害,但测试会反复调用它 —— 断言的是 handler 数量
    不随调用次数增长,而不是"某个分支被走到"。"""
    import logging

    import teams_adapter.__main__ as entry

    ms = logging.getLogger("microsoft_agents")
    saved = list(ms.handlers)
    try:
        ms.handlers = []
        for _ in range(3):
            entry._configure_logging()
        assert len(ms.handlers) == 1
    finally:
        ms.handlers = saved

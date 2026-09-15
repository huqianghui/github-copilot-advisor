# channels/teams/src/teams_adapter/__main__.py
"""启动 Teams adapter:完整 composition root。
需 CONNECTIONS__* 认证变量与 advisor agent 的全部环境变量。"""
import logging
from os import environ

from aiohttp import web
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    MemoryStorage,
    TurnState,
)

from advisor_agent.factory import build_advisor
from advisor_shared.logging import TelemetryFormatter, configure_logging
from advisor_shared.telemetry import step
from teams_adapter.app import create_app
from teams_adapter.bot import register_handlers
from teams_adapter.downloader import TeamsImageDownloader


def _configure_logging() -> None:
    configure_logging()
    ms = logging.getLogger("microsoft_agents")
    # 幂等:原先无条件 addHandler,每调一次就多挂一个 handler、日志多打一行。
    # main() 只跑一次时无害,但测试会反复调用它,重复挂载是真实的。
    if not ms.handlers:
        handler = logging.StreamHandler()
        ms.addHandler(handler)
    for handler in ms.handlers:
        if type(handler) is logging.StreamHandler:
            handler.setFormatter(TelemetryFormatter(
                environ.get("ADVISOR_LOG_FORMAT", "json").lower()))
    ms.setLevel(logging.INFO)
    ms.propagate = False


def build_agent_app():
    config = load_configuration_from_env(environ)
    connection_manager = MsalConnectionManager(**config)
    adapter = CloudAdapter(connection_manager=connection_manager)
    storage = MemoryStorage()
    authorization = Authorization(storage, connection_manager, **config)
    agent_app = AgentApplication[TurnState](
        storage=storage,
        adapter=adapter,
        authorization=authorization,
        start_typing_timer=False,       # behavior-preserving:handler 手动发 typing
        remove_recipient_mention=False,  # behavior-preserving:extract.strip_mentions 唯一剥离来源
        file_downloaders=[TeamsImageDownloader(connection_manager)],
        **config,
    )
    core = build_advisor(channel_name="teams")
    register_handlers(agent_app, core)
    agent_configuration = connection_manager.get_default_connection_configuration()
    return agent_app, adapter, agent_configuration


def main() -> None:
    _configure_logging()
    with step("app.startup"):
        agent_app, adapter, agent_configuration = build_agent_app()
        app = create_app(agent_app, adapter, agent_configuration)
    web.run_app(app, port=int(environ.get("PORT", 3978)))


if __name__ == "__main__":
    main()

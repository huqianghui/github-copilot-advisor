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
from teams_adapter.app import create_app
from teams_adapter.bot import register_handlers


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO)
    ms = logging.getLogger("microsoft_agents")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    ms.addHandler(handler)
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
        **config,
    )
    core = build_advisor(channel_name="teams")
    register_handlers(agent_app, core)
    agent_configuration = connection_manager.get_default_connection_configuration()
    return agent_app, adapter, agent_configuration


def main() -> None:
    _configure_logging()
    agent_app, adapter, agent_configuration = build_agent_app()
    app = create_app(agent_app, adapter, agent_configuration)
    web.run_app(app, port=int(environ.get("PORT", 3978)))


if __name__ == "__main__":
    main()

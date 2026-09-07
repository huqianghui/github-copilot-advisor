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
from teams_adapter.downloader import TeamsImageDownloader


def _configure_logging() -> None:
    # basicConfig 只保证"root 上有 handler",不保证 level 被应用:CPython 里
    # level 的赋值在 `if len(root.handlers) == 0` 分支内,root 已有 handler 时
    # 它提前返回。而 OPENAI_LOG 一旦设置,openai 就在导入期通过 _basic_config()
    # 装了一个 root handler —— 于是这行在最需要它的场景里是空操作(实测:
    # root 停在 WARNING)。后果很讽刺:运维为排查图片问题去开 OPENAI_LOG,
    # 会同时丢掉 bot 的逐消息遥测和 core 的 advisor_event 审计行(带
    # image_count 的正是后者)。所以紧跟一行无条件的 setLevel 兜底。
    # 不用 basicConfig(force=True):force 会 removeHandler + close 掉已有的
    # root handler,那是在处置别人(宿主/APM/openai)装的东西,越权且不可逆;
    # 显式 setLevel 只声明本应用要的阈值,不动 handler 拓扑。
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().setLevel(logging.INFO)
    # OPENAI_LOG=debug 会把请求体整个 dump,含图片 base64。此处在 openai
    # 导入期 setup_logging() 之后覆盖,确保生产环境改环境变量也打不开。
    logging.getLogger("openai").setLevel(logging.INFO)
    ms = logging.getLogger("microsoft_agents")
    # 幂等:原先无条件 addHandler,每调一次就多挂一个 handler、日志多打一行。
    # main() 只跑一次时无害,但测试会反复调用它,重复挂载是真实的。
    if not ms.handlers:
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
        file_downloaders=[TeamsImageDownloader(connection_manager)],
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

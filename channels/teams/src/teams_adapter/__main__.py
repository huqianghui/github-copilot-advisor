# channels/teams/src/teams_adapter/__main__.py
"""启动:python -m teams_adapter(需 TEAMS_APP_ID/TEAMS_APP_PASSWORD 及计划 2 全部环境变量)。"""
import logging
import os

from aiohttp import web
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)

from advisor_agent.factory import build_advisor
from teams_adapter.app import create_app
from teams_adapter.bot import AdvisorBot


class _Config:
    APP_ID = os.environ.get("TEAMS_APP_ID", "")
    APP_PASSWORD = os.environ.get("TEAMS_APP_PASSWORD", "")
    APP_TYPE = "MultiTenant"
    APP_TENANTID = ""


def main():
    logging.basicConfig(level=logging.INFO)
    adapter = CloudAdapter(ConfigurationBotFrameworkAuthentication(_Config()))
    core = build_advisor(channel_name="teams")
    bot = AdvisorBot(core, bot_id=f"28:{_Config.APP_ID}")
    app = create_app(bot, adapter)
    web.run_app(app, port=int(os.environ.get("PORT", 3978)))


if __name__ == "__main__":
    main()

# channels/teams/src/teams_adapter/__main__.py
"""启动 Teams adapter,需 Bot 身份和 advisor agent 的全部环境变量。"""
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
from teams_adapter.config import TeamsBotConfig


def main():
    logging.basicConfig(level=logging.INFO)
    config = TeamsBotConfig()
    adapter = CloudAdapter(ConfigurationBotFrameworkAuthentication(config))
    core = build_advisor(channel_name="teams")
    bot = AdvisorBot(core, bot_id=f"28:{config.APP_ID}")
    app = create_app(bot, adapter)
    web.run_app(app, port=int(os.environ.get("PORT", 3978)))


if __name__ == "__main__":
    main()

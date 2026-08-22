# channels/teams/src/teams_adapter/app.py
"""aiohttp 入口:Bot Framework 消息端点 + 健康检查。"""
from aiohttp import web


def create_app(bot, adapter) -> web.Application:
    async def messages(request: web.Request) -> web.Response:
        return await adapter.process(request, bot)

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    return app

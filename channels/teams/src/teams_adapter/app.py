# channels/teams/src/teams_adapter/app.py
"""aiohttp 入口:Agents SDK 消息端点 + 公开健康检查。
认证只保护 /api/messages;/healthz 保持匿名(behavior-preserving)。"""
from aiohttp import web
from microsoft_agents.hosting.aiohttp import (
    jwt_authorization_decorator,
    start_agent_process,
)


def create_app(agent_app, adapter, agent_configuration) -> web.Application:
    @jwt_authorization_decorator          # 仅此路由校验 Azure Bot JWT
    async def messages(request: web.Request):
        return await start_agent_process(
            request,
            request.app["agent_app"],
            request.app["adapter"],
        )

    async def healthz(_: web.Request) -> web.Response:   # 公开,无 JWT
        return web.Response(text="ok")

    app = web.Application()
    # jwt_authorization_decorator 从 app["agent_configuration"] 读认证配置(SDK 契约,精确键名)。
    app["agent_configuration"] = agent_configuration
    app["agent_app"] = agent_app
    app["adapter"] = adapter
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    return app

# channels/teams/src/teams_adapter/app.py
"""aiohttp 入口:Agents SDK 消息端点 + 公开健康检查。
认证只保护 /api/messages;/healthz 保持匿名(behavior-preserving)。"""
import logging

from aiohttp import web
from microsoft_agents.hosting.aiohttp import (
    jwt_authorization_decorator,
    start_agent_process,
)
from advisor_shared.telemetry import step, trace_scope

logger = logging.getLogger(__name__)


def create_app(agent_app, adapter, agent_configuration) -> web.Application:
    @web.middleware
    async def trace_request(request: web.Request, handler):
        if request.path != "/api/messages":
            return await handler(request)
        with trace_scope(new=True), step("teams.http") as timing:
            try:
                response = await handler(request)
            except web.HTTPException as error:
                timing.attributes["http_status"] = error.status
                raise
            timing.attributes["http_status"] = response.status
            if response.status >= 400:
                timing.status = "error" if response.status >= 500 else "degraded"
            return response

    @jwt_authorization_decorator          # 仅此路由校验 Azure Bot JWT
    async def messages(request: web.Request):
        response = await start_agent_process(
            request,
            request.app["agent_app"],
            request.app["adapter"],
        )
        if response is not None and response.status >= 400:
            logger.warning(
                "Agents SDK rejected request status=%s content_type=%s "
                "content_length=%s response=%s",
                response.status,
                request.content_type,
                request.content_length,
                response.text,
            )
        return response

    async def healthz(_: web.Request) -> web.Response:   # 公开,无 JWT
        return web.Response(text="ok")

    app = web.Application(middlewares=[trace_request])
    # jwt_authorization_decorator 从 app["agent_configuration"] 读认证配置(SDK 契约,精确键名)。
    app["agent_configuration"] = agent_configuration
    app["agent_app"] = agent_app
    app["adapter"] = adapter
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    return app

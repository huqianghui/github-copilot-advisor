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


def create_cloud_app(ingress, worker_factory, *, runtime=None) -> web.Application:
    """Supervise shared queue/maintenance tasks for the lifetime of the ACA app."""
    from teams_adapter.cloud.observability import host_invocation

    if runtime is None:
        from teams_adapter.cloud.runtime import get_runtime
        runtime = get_runtime()
    @web.middleware
    async def observe_http(request, handler):
        probe = (request.method in {"GET", "HEAD"}
                 and request.path in {"/healthz", "/readyz"})
        with host_invocation("http", host_kind="aca", workload=not probe) as observation:
            try:
                response = await handler(request)
            except web.HTTPException as error:
                observation.http_status(error.status)
                raise
            observation.http_status(response.status)
            return response

    app = web.Application(middlewares=[observe_http])

    async def lifecycle(_app):
        try:
            await runtime.start(worker_factory=worker_factory)
            yield
        finally:
            await runtime.aclose()

    async def messages(request):
        result = await ingress.handle_http(
            request.method, request.headers, await request.read())
        if result.body is None:
            return web.Response(status=result.status, headers=result.headers)
        return web.json_response(result.body, status=result.status, headers=result.headers)

    async def healthz(_request):
        return web.Response(text="ok" if runtime.healthy else "unhealthy",
                            status=200 if runtime.healthy else 503)

    async def readyz(_request):
        ready = runtime.healthy and runtime.ready
        return web.Response(text="ready" if ready else "not ready", status=200 if ready else 503)

    async def benchmark(request, operation):
        target = runtime.benchmark_ingress
        if target is None or not target.settings.enabled:
            return web.Response(status=404)
        handler = {"runs": target.handle_runs, "messages": target.handle_messages}[operation]
        result = await handler(request.method, request.headers, await request.read())
        if result.body is None:
            return web.Response(status=result.status, headers=result.headers)
        return web.json_response(result.body, status=result.status, headers=result.headers)

    async def benchmark_runs(request):
        return await benchmark(request, "runs")

    async def benchmark_messages(request):
        return await benchmark(request, "messages")

    app.cleanup_ctx.append(lifecycle)
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_post("/bench/runs", benchmark_runs)
    app.router.add_post("/bench/messages", benchmark_messages)
    return app

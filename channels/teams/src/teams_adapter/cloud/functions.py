"""Native Functions adapters; the generic runtime never imports azure.functions."""
import json
import logging

import azure.functions as func

from advisor_shared.cloud import QueueWake
from teams_adapter.cloud.observability import current_host_telemetry, host_invocation

logger = logging.getLogger(__name__)


def get_ingress():
    from teams_adapter.cloud.runtime import get_ingress as get
    return get()


def get_runtime():
    from teams_adapter.cloud.runtime import get_runtime as get
    return get()


def get_benchmark_ingress():
    from teams_adapter.cloud.runtime import get_benchmark_ingress as get
    return get()


async def get_worker():
    return await get_runtime().get_worker()


def http_invocation(*, workload=True):
    """Observe every HTTP route; only explicit health probes exclude workload."""
    return host_invocation("http", host_kind="functions", workload=workload)


def current_http_telemetry():
    """Read-only provider for an ingress running inside http_invocation()."""
    return current_host_telemetry()


class FunctionRequestAdapter:
    def __init__(self, request: func.HttpRequest, claims=None):
        self.request, self.claims = request, claims

    @property
    def method(self):
        return self.request.method

    @property
    def headers(self):
        return self.request.headers

    async def json(self):
        return self.request.get_json()

    def get_claims_identity(self):
        return self.claims

    def get_path_param(self, name):
        return (self.request.route_params or {}).get(name, "")


async def handle_function_request(req: func.HttpRequest) -> func.HttpResponse:
    with http_invocation() as observation:
        request = FunctionRequestAdapter(req)
        try:
            result = await get_ingress().handle_http(
                request.method, request.headers, req.get_body())
        except Exception as error:
            logger.error("function_http_setup_failed error_type=%s", type(error).__name__)
            observation.http_status(503)
            return func.HttpResponse(status_code=503)
        observation.http_status(result.status)
        return func.HttpResponse(
            body=json.dumps(result.body) if result.body is not None else None,
            status_code=result.status, headers=result.headers,
            mimetype="application/json" if result.body is not None else None)


async def handle_function_health(req: func.HttpRequest) -> func.HttpResponse:
    with http_invocation(workload=False) as observation:
        observation.http_status(200)
        return func.HttpResponse("ok", status_code=200)


async def handle_benchmark_request(req: func.HttpRequest, *, operation: str) -> func.HttpResponse:
    """Use the shared target protocol and its distinct opt-in authentication."""
    with http_invocation() as observation:
        try:
            ingress = get_benchmark_ingress()
            handler = {"runs": ingress.handle_runs, "messages": ingress.handle_messages}[operation]
            result = await handler(req.method, req.headers, req.get_body())
        except Exception as error:
            logger.error("function_benchmark_setup_failed error_type=%s", type(error).__name__)
            observation.http_status(503)
            return func.HttpResponse(status_code=503)
        observation.http_status(result.status)
        return func.HttpResponse(
            body=json.dumps(result.body) if result.body is not None else None,
            status_code=result.status, headers=result.headers,
            mimetype="application/json" if result.body is not None else None)


async def handle_queue_message(body: bytes) -> None:
    """Functions owns receipts; completion is returned only after safe work/reschedule."""
    with host_invocation("worker", host_kind="functions"):
        try:
            wake = QueueWake.model_validate_json(body)
            worker = await get_worker()
            result = await worker.process_session(wake.partition_key, wake.trace_id)
            if result == "busy":
                await get_runtime().dispatcher.notify(
                    wake.partition_key, wake.trace_id, delay_seconds=5)
            elif result not in {"drained", "rescheduled"}:
                raise RuntimeError("unknown worker completion state")
        except Exception as error:
            logger.error("function_queue_failed error_type=%s", type(error).__name__)
            raise RuntimeError("queue_processing_failed") from None


async def handle_maintenance() -> None:
    with host_invocation("maintenance", host_kind="functions"):
        try:
            await get_runtime().run_maintenance_once()
        except Exception as error:
            logger.error("function_maintenance_failed error_type=%s", type(error).__name__)
            raise RuntimeError("maintenance_failed") from None

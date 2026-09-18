import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import azure.functions as func
import pytest

from advisor_shared.cloud import QueueWake
from test_ingress import auth_case, ingress_case, signing_key, ingress, signed_token
from test_executor import payload_for


async def test_native_http_request_runs_verified_ingress(ingress_case, monkeypatch):
    from teams_adapter.cloud import functions
    monkeypatch.setattr(functions, "get_ingress", lambda: ingress(ingress_case))
    request = func.HttpRequest(
        method="POST", url="https://localhost/api/messages",
        headers={"Authorization": f"Bearer {signed_token(ingress_case.auth.key)}"},
        body=json.dumps(payload_for().activity).encode())
    response = await functions.handle_function_request(request)
    assert isinstance(response, func.HttpResponse)
    assert response.status_code == 202
    assert json.loads(response.get_body())["job_id"] in ingress_case.jobs.accepted


async def test_native_http_adapter_preserves_headers_json_claims_and_route_parameters():
    from microsoft_agents.hosting.core.authorization import ClaimsIdentity
    from teams_adapter.cloud.functions import FunctionRequestAdapter

    request = func.HttpRequest(
        method="POST", url="https://localhost/api/messages", body=b'{"type":"invoke"}',
        headers={"Content-Type": "application/json"}, route_params={"operation": "test"})
    claims = ClaimsIdentity({"aud": "app"})
    adapter = FunctionRequestAdapter(request, claims)
    assert adapter.method == "POST"
    assert adapter.headers["Content-Type"] == "application/json"
    assert await adapter.json() == {"type": "invoke"}
    assert adapter.get_claims_identity() is claims
    assert adapter.get_path_param("operation") == "test"
    assert adapter.get_path_param("absent") == ""


async def test_function_setup_failure_returns_503_not_accepted(monkeypatch, caplog):
    from teams_adapter.cloud import functions
    def unavailable():
        raise RuntimeError("private-client-configuration")
    monkeypatch.setattr(functions, "get_ingress", unavailable)
    response = await functions.handle_function_request(func.HttpRequest(
        method="POST", url="https://localhost/api/messages", body=b"{}"))
    assert response.status_code == 503
    assert "private-client-configuration" not in caplog.text


@pytest.mark.parametrize("result", ["drained", "rescheduled", "busy"])
async def test_function_queue_awaits_worker_and_reliably_reschedules_busy(
        monkeypatch, result):
    from teams_adapter.cloud import functions
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    wake = QueueWake(partition_key="partition", trace_id="wake-trace")

    class Worker:
        async def process_session(self, partition, trace):
            assert (partition, trace) == ("partition", "wake-trace")
            entered.set()
            await release.wait()
            return result

    async def get_worker():
        return Worker()

    async def notify(partition, trace, *, delay_seconds):
        calls.append((partition, trace, delay_seconds))

    monkeypatch.setattr(functions, "get_worker", get_worker)
    monkeypatch.setattr(functions, "get_runtime", lambda: SimpleNamespace(
        dispatcher=SimpleNamespace(notify=notify)))
    task = asyncio.create_task(functions.handle_queue_message(wake.model_dump_json().encode()))
    await entered.wait()
    assert not task.done()
    release.set()
    await task
    assert calls == ([("partition", "wake-trace", 5)] if result == "busy" else [])


async def test_function_worker_failure_propagates_for_host_retry(monkeypatch):
    from teams_adapter.cloud import functions
    class Worker:
        async def process_session(self, *args):
            raise TimeoutError("private-storage-url")
    async def get_worker():
        return Worker()
    monkeypatch.setattr(functions, "get_worker", get_worker)
    with pytest.raises(RuntimeError, match="queue_processing_failed"):
        await functions.handle_queue_message(
            QueueWake(partition_key="p", trace_id="trace").model_dump_json().encode())


def test_native_bindings_routes_encoding_and_maintenance_schedule(monkeypatch):
    monkeypatch.setenv("ADVISOR_MAINTENANCE_SCHEDULE", "0 */5 * * * *")
    path = Path(__file__).parents[4] / "deploy" / "functions" / "function_app.py"
    spec = importlib.util.spec_from_file_location("native_function_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    functions = {f.get_function_name(): json.loads(f.get_function_json())
                 for f in module.app.get_functions()}
    bindings = [binding for fn in functions.values() for binding in fn["bindings"]]
    routes = {b["route"] for b in bindings if b["type"] == "httpTrigger"}
    assert routes == {"api/messages", "healthz", "bench/runs", "bench/messages"}
    queue = next(b for b in bindings if b["type"] == "queueTrigger")
    assert queue["queueName"] == "%ADVISOR_QUEUE_NAME%"
    assert queue["connection"] == "AdvisorQueue"
    timer = next(b for b in bindings if b["type"] == "timerTrigger")
    assert timer["schedule"] == "%ADVISOR_MAINTENANCE_SCHEDULE%"
    assert not timer["runOnStartup"] and timer["useMonitor"]
    host = json.loads(path.with_name("host.json").read_text())
    assert host["extensions"]["http"]["routePrefix"] == ""
    assert host["extensions"]["queues"]["messageEncoding"] == "none"

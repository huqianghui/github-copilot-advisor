import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess
import sys

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import azure.functions as func
import pytest

from advisor_shared.benchmark import BenchmarkReply
from test_aca_lifecycle import CONFIG, runtime_case
from test_benchmark_target import CASES, HEADERS, KEY, message, registration, _storage_contract
from test_ingress import auth_case, signing_key, signed_token
from test_executor import APP_ID, payload_for


@asynccontextmanager
async def receiver():
    replies = []
    received = asyncio.Event()

    async def reply(request):
        assert request.headers["X-Benchmark-Key"] == KEY
        body = BenchmarkReply.model_validate_json(await request.read())
        replies.append(body)
        if body.event == "final":
            received.set()
        return web.json_response({
            "disposition": "typing" if body.event == "typing" else "completed"})

    app = web.Application()
    app.router.add_post("/bench/replies", reply)
    async with TestServer(app) as server:
        yield str(server.make_url("/bench/replies")), replies, received


def build_target_runtime(monkeypatch, callback_url, *, capture=None, **changes):
    from azure.storage.blob.aio import ContainerClient
    from azure.storage.queue.aio import QueueClient
    from teams_adapter.cloud.runtime import build_runtime

    contract = _storage_contract()
    blobs = {}
    queues = {}

    def container(url, name, **kwargs):
        transport = blobs.setdefault(name, contract.ContractTransport())
        return ContainerClient(
            "https://memory.blob.core.windows.net", name,
            transport=transport, retry_total=0, **kwargs)

    def queue(url, name, **kwargs):
        transport = queues.setdefault(name, contract.QueueTransport())
        return QueueClient(
            "https://memory.queue.core.windows.net", name,
            transport=transport, retry_total=0, **kwargs)

    monkeypatch.setattr("azure.storage.blob.aio.ContainerClient", container)
    monkeypatch.setattr("azure.storage.queue.aio.QueueClient", queue)
    env = {
        **CONFIG,
        "BENCHMARK_ENABLED": "true", "BENCHMARK_TARGET_ID": "target-local",
        "BENCHMARK_CASES_PATH": str(CASES), "BENCHMARK_AUTH_MODE": "key",
        "BENCHMARK_API_KEY": KEY, "BENCHMARK_RECEIVER_URL": callback_url,
        **changes,
    }
    runtime = build_runtime(env, benchmark_allow_loopback=True)
    if capture is not None:
        capture.update(blobs=blobs, queues=queues)
    return runtime


async def test_functions_benchmark_is_disabled_without_building_runtime(monkeypatch):
    from teams_adapter.cloud import functions, runtime

    monkeypatch.delenv("BENCHMARK_ENABLED", raising=False)

    def forbidden():
        pytest.fail("disabled benchmark initialized runtime or Agent")

    monkeypatch.setattr(runtime, "get_runtime", forbidden)
    for operation in ("runs", "messages"):
        response = await functions.handle_benchmark_request(func.HttpRequest(
            method="POST", url=f"http://localhost/bench/{operation}", body=b"{}"),
            operation=operation)
        assert response.status_code == 404


async def test_aca_disabled_routes_are_present_and_do_not_use_bot_auth(runtime_case):
    from teams_adapter.app import create_cloud_app

    class EmptyQueue:
        async def receive(self):
            if False:
                yield

    runtime_case.runtime.queue = EmptyQueue()
    app = create_cloud_app(
        runtime_case.runtime.ingress, runtime_case.runtime.get_worker,
        runtime=runtime_case.runtime)
    registered = {resource.canonical for resource in app.router.resources()}
    assert {"/bench/runs", "/bench/messages"} <= registered
    async with TestClient(TestServer(app)) as client:
        for route in ("runs", "messages"):
            response = await client.post(f"/bench/{route}", json={})
            assert response.status == 404
    assert runtime_case.state.builds == 0


async def test_aca_routes_use_canonical_protocol_real_worker_and_isolated_receiver(monkeypatch):
    from teams_adapter.app import create_cloud_app
    from teams_adapter.cloud.observability import HostObservations
    from teams_adapter.cloud import observability

    monkeypatch.setattr(observability, "_observations", HostObservations(
        environment={"CONTAINER_APP_REPLICA_NAME": "actual-test-replica"}))

    def forbidden_model(*args, **kwargs):
        pytest.fail("synthetic benchmark initialized a model")

    monkeypatch.setattr("advisor_agent.factory.build_components", forbidden_model)
    async with receiver() as (url, replies, received):
        runtime = build_target_runtime(monkeypatch, url)
        app = create_cloud_app(runtime.ingress, runtime.get_worker, runtime=runtime)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/healthz")).status == 200
            assert (await client.post("/api/messages", headers=HEADERS, json={})).status == 401
            assert (await client.post("/bench/runs", json=registration())).status == 401
            response = await client.post("/bench/runs", headers=HEADERS, json=registration())
            assert response.status == 201
            response = await client.post("/bench/messages", headers=HEADERS, json=message())
            assert response.status == 202
            ack = await response.json()
            await asyncio.wait_for(received.wait(), 8)
            final = next(reply for reply in replies if reply.event == "final")
            assert final.job_id == ack["job_id"]
            assert final.run_id == "run-1"
            assert "synthetic" in final.reply.text
            assert final.telemetry.boots
            assert all(not boot.first_request for boot in final.telemetry.boots)
            assert final.telemetry.model_rounds is None
            worker = await runtime.get_worker()
            callback_session = worker.executor.benchmark.sink.session._session
            assert callback_session is not None and not callback_session.closed
        assert callback_session.closed
        assert runtime.queue._closed
        assert runtime.ingress.jobs.gateway._closed


async def test_functions_routes_use_target_auth_and_store_same_wire_ack(monkeypatch):
    from teams_adapter.cloud import functions

    async with receiver() as (url, _, _):
        runtime = build_target_runtime(monkeypatch, url)
        monkeypatch.setattr(functions, "get_benchmark_ingress", lambda: runtime.benchmark_ingress)
        try:
            for operation, body, expected in (
                ("runs", registration(), 201), ("messages", message(), 202),
            ):
                response = await functions.handle_benchmark_request(func.HttpRequest(
                    method="POST", url=f"http://localhost/bench/{operation}",
                    headers=HEADERS, body=json.dumps(body).encode()), operation=operation)
                assert response.status_code == expected
            ack = json.loads(response.get_body())
            stored = await runtime.ingress.jobs.load_payload(ack["job_id"])
            assert stored.target == "benchmark"
            assert stored.run_id == "run-1" and stored.request_id == "request-1"
        finally:
            await runtime.aclose()


async def test_functions_authenticated_admission_queue_worker_and_real_callback(monkeypatch):
    from teams_adapter.cloud import functions, observability
    from teams_adapter.cloud.observability import HostObservations

    monkeypatch.setattr(observability, "_observations", HostObservations(
        environment={"WEBSITE_INSTANCE_ID": "observed-functions-instance"}))
    capture = {}
    async with receiver() as (url, replies, received):
        runtime = build_target_runtime(monkeypatch, url, capture=capture)
        monkeypatch.setattr(functions, "get_benchmark_ingress", lambda: runtime.benchmark_ingress)
        monkeypatch.setattr(functions, "get_runtime", lambda: runtime)
        try:
            await functions.handle_function_health(func.HttpRequest(
                method="GET", url="http://localhost/healthz", body=b""))
            unauthenticated = await functions.handle_benchmark_request(func.HttpRequest(
                method="POST", url="http://localhost/bench/runs",
                body=json.dumps(registration()).encode()), operation="runs")
            assert unauthenticated.status_code == 401
            for operation, body, status in (
                ("runs", registration(), 201), ("messages", message(), 202),
            ):
                response = await functions.handle_benchmark_request(func.HttpRequest(
                    method="POST", url=f"http://localhost/bench/{operation}", headers=HEADERS,
                    body=json.dumps(body).encode()), operation=operation)
                assert response.status_code == status
            ack = json.loads(response.get_body())
            queue_transport = capture["queues"]["advisor-incoming"]
            published = next(iter(queue_transport.messages.values()))["body"].encode("utf-8")
            await functions.handle_queue_message(published)
            assert received.is_set()
            final = next(item for item in replies if item.event == "final")
            assert final.job_id == ack["job_id"]
            assert final.outcome == "success"
            assert {boot.group for boot in final.telemetry.boots} == {"http", "worker"}
            assert all(not boot.first_request for boot in final.telemetry.boots)
            assert (await runtime.ingress.jobs.read(ack["job_id"])).state == "completed"
            assert not any(operation in {"ack", "update"}
                           for operation, _ in queue_transport.calls)
            worker = await runtime.get_worker()
            callback_session = worker.executor.benchmark.sink.session._session
            assert callback_session is not None and not callback_session.closed
        finally:
            await runtime.aclose()
        assert callback_session.closed
        assert runtime.ingress.jobs.gateway._closed


async def test_benchmark_key_does_not_replace_normal_bot_jwt_auth(monkeypatch, auth_case):
    from teams_adapter.cloud import functions

    async with receiver() as (url, _, _):
        runtime = build_target_runtime(monkeypatch, url, **{
            "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": APP_ID})
        monkeypatch.setattr(functions, "get_ingress", lambda: runtime.ingress)
        try:
            body = json.dumps(payload_for().activity).encode()
            for headers, status in (
                (HEADERS, 401),
                ({"Authorization": f"Bearer {signed_token(auth_case.key)}"}, 202),
            ):
                response = await functions.handle_function_request(func.HttpRequest(
                    method="POST", url="http://localhost/api/messages",
                    headers=headers, body=body))
                assert response.status_code == status
            stored = await runtime.ingress.jobs.load_payload(json.loads(response.get_body())["job_id"])
            assert stored.target == "teams"
        finally:
            await runtime.aclose()


async def test_enabled_routes_require_explicit_approved_dataset_and_receiver(monkeypatch):
    from teams_adapter.cloud.runtime import build_runtime

    with pytest.raises(ValueError):
        build_runtime({**CONFIG, "BENCHMARK_ENABLED": "true"})


async def test_host_dispatch_does_not_relabel_or_accept_unregistered_benchmark_jobs(monkeypatch):
    from advisor_shared.delivery import DeliveryFailure
    from test_executor import benchmark_payload

    async with receiver() as (url, _, _):
        runtime = build_target_runtime(monkeypatch, url)
        try:
            worker = await runtime.get_worker()
            payload = benchmark_payload()
            original = payload.model_dump()
            with pytest.raises(DeliveryFailure):
                await worker.executor.validate(payload)
            assert payload.model_dump() == original
        finally:
            await runtime.aclose()


async def test_real_agent_benchmark_initializes_shared_components_only_on_prepare(monkeypatch):
    from advisor_agent.factory import AdvisorComponents, AzureAdvisor
    from test_executor import ModelBoundary

    backend = ModelBoundary()
    owner = AzureAdvisor(
        backend, channel_name="teams", azure_clients=(), credential_owner=None)
    builds = []

    def build_components(channel_name, *, credentials):
        builds.append(channel_name)
        return AdvisorComponents(owner)

    monkeypatch.setattr("advisor_agent.factory.build_components", build_components)
    async with receiver() as (url, replies, _):
        runtime = build_target_runtime(monkeypatch, url)
        try:
            worker = await runtime.get_worker()
            assert builds == []
            target = runtime.benchmark_ingress
            assert (await target.handle_runs(
                "POST", HEADERS, registration(scenario="real_agent"))).status == 201
            for number in (1, 2):
                submitted = await target.handle_messages("POST", HEADERS, message(
                    request_id=f"real-{number}", scenario="real_agent"))
                payload = await runtime.ingress.jobs.load_payload(submitted.body["job_id"])
                await worker.process_session(payload.partition_key, payload.trace_id)
            assert builds == ["teams"]
            assert len(backend.calls) == 2
            assert len([reply for reply in replies if reply.event == "final"]) == 2
        finally:
            await runtime.aclose()
    assert owner._azure_closed


def test_enabled_synthetic_worker_composition_does_not_import_model_factory():
    env = {
        **CONFIG, "BENCHMARK_ENABLED": "true", "BENCHMARK_TARGET_ID": "target-local",
        "BENCHMARK_CASES_PATH": str(CASES), "BENCHMARK_AUTH_MODE": "key",
        "BENCHMARK_API_KEY": KEY, "BENCHMARK_RECEIVER_URL": "https://receiver.example/bench/replies",
    }
    code = f"""
import asyncio, json, sys
from teams_adapter.cloud.runtime import build_runtime
async def check():
    runtime = build_runtime({env!r})
    try:
        await runtime.get_worker()
        print(json.dumps(sorted(sys.modules)))
    finally:
        await runtime.aclose()
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).parents[4],
        capture_output=True, text=True, timeout=120, check=True)
    modules = json.loads(result.stdout)
    assert "advisor_agent.factory" not in modules
    assert "openai" not in modules
    assert "agent_framework" not in modules


@pytest.mark.parametrize("storage_mode", ["key", "entra"])
async def test_entra_callback_uses_receiver_audience_and_owned_explicit_identity(
        monkeypatch, storage_mode):
    identities = []

    class Credential:
        closes = 0

        async def get_token(self, *args, **kwargs):
            pytest.fail("constructing ingress/worker must not acquire a callback token")

        async def close(self):
            self.closes += 1

    def credential(source, client_id):
        value = Credential()
        identities.append(value)
        return value

    monkeypatch.setattr("advisor_shared.azure_auth.make_entra_credential", credential)
    receiver_audience = "00000000-0000-0000-0000-000000000006"
    runtime = build_target_runtime(
        monkeypatch, "https://receiver.example/bench/replies",
        ADVISOR_STORAGE_AUTH_MODE=storage_mode, BENCHMARK_AUTH_MODE="entra",
        BENCHMARK_TENANT_ID="00000000-0000-0000-0000-000000000001",
        BENCHMARK_API_CLIENT_ID="00000000-0000-0000-0000-000000000002",
        BENCHMARK_ALLOWED_CLIENT_IDS="00000000-0000-0000-0000-000000000003",
        BENCHMARK_ALLOWED_PRINCIPAL_IDS="00000000-0000-0000-0000-000000000004",
        BENCHMARK_RECEIVER_AUDIENCE=receiver_audience)
    try:
        worker = await runtime.get_worker()
        assert len(identities) == 1
        sink = worker.executor.benchmark.sink
        assert sink.auth_mode == "entra"
        assert sink.scope == f"api://{receiver_audience}/.default"
        assert sink._credential is identities[0]
        assert sink._key is None
    finally:
        await runtime.aclose()
    assert identities[0].closes == 1


@pytest.mark.parametrize("url", [
    "", "https://api.botframework.com/bench/replies",
    "https://receiver.example/not-the-protocol", "https://receiver.example/bench/replies?token=x",
])
def test_enabled_host_rejects_missing_or_unsafe_receiver_configuration(monkeypatch, url):
    with pytest.raises(ValueError):
        build_target_runtime(monkeypatch, url)

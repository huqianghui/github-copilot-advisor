"""Target contracts: real Azure SDK memory wire and local observer HTTP."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from aiohttp import ClientSession, web
from azure.storage.blob.aio import ContainerClient
from azure.storage.queue.aio import QueueClient
import pytest

from advisor_agent.durable.blob import BlobGateway
from advisor_agent.durable.history import BlobHistoryStore
from advisor_agent.durable.jobs import InboxStore, JobStore
from advisor_agent.durable.queue import OutboxDispatcher, SessionQueue
from advisor_agent.durable.worker import SessionWorker
from advisor_shared.benchmark import dataset_digest


ROOT = Path(__file__).resolve().parents[4]
CASES = ROOT / "deploy" / "bench" / "cases.json"
KEY = "local-test-benchmark-key-32-characters"
HEADERS = {"X-Benchmark-Key": KEY}
BOT = "benchmark-bot"


def _storage_contract():
    name = "_benchmark_storage_contract"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "agent" / "tests" / "cloud" / "conftest.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@pytest.fixture
async def target():
    from teams_adapter.cloud.benchmark import (
        BenchmarkAuth, BenchmarkIngress, BenchmarkSettings,
    )

    contract = _storage_contract()
    transport, queue_transport = contract.ContractTransport(), contract.QueueTransport()
    container = ContainerClient(
        "https://memory.invalid", "contract", credential=None,
        transport=transport, retry_total=0, api_version="2023-11-03")
    queue_client = QueueClient(
        "https://memory.queue.core.windows.net", "contract", credential="bG9jYWw=",
        transport=queue_transport, retry_total=0, api_version="2023-11-03")
    gateway, queue = BlobGateway(container), SessionQueue(queue_client)
    jobs = JobStore(gateway, bot_id=BOT, inbox=InboxStore(gateway))
    dispatcher = OutboxDispatcher(jobs.inbox, queue)
    settings = BenchmarkSettings(
        enabled=True, target_id="target-local", cases_path=str(CASES),
        max_requests=20, max_concurrency=4, max_run_seconds=3600)
    auth = BenchmarkAuth(mode="key", key=KEY)
    ingress = BenchmarkIngress(jobs, dispatcher, settings=settings, auth=auth)
    value = SimpleNamespace(
        settings=settings, ingress=ingress, auth=auth, jobs=jobs, gateway=gateway,
        dispatcher=dispatcher, queue=queue, transport=transport,
        queue_transport=queue_transport, history=BlobHistoryStore(gateway))
    try:
        yield value
    finally:
        await queue.aclose()
        await gateway.aclose()


def registration(**changes):
    body = {
        "run_id": "run-1", "profile": "warm", "case_ids": ["kb-01", "kb-02"],
        "seed": 11, "scenario": "synthetic", "history_mode": "empty",
        "max_requests": 4, "max_concurrency": 2,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "dataset_sha256": dataset_digest(json.loads(CASES.read_text(encoding="utf-8"))),
    }
    return body | changes


def message(**changes):
    return {
        "run_id": "run-1", "request_id": "request-1", "case_id": "kb-01",
        "virtual_user": 0, "scenario": "synthetic",
    } | changes


async def register(target, body=None):
    return await target.ingress.handle_runs("POST", HEADERS, body or registration())


async def submit(target, body=None):
    return await target.ingress.handle_messages("POST", HEADERS, body or message())


async def test_disabled_benchmark_returns_404_without_configuration():
    from teams_adapter.cloud.benchmark import BenchmarkIngress, BenchmarkSettings

    ingress = BenchmarkIngress(None, None, settings=BenchmarkSettings())
    assert (await ingress.handle_runs("POST", {}, {})).status == 404
    assert (await ingress.handle_messages("POST", {}, {})).status == 404


async def test_registration_is_bound_to_principal_and_content(target):
    from teams_adapter.cloud.benchmark import BenchmarkAuth

    body = registration()
    first = await register(target, body)
    assert first.status == 201
    assert first.body == {
        "run_id": "run-1", "target_id": "target-local",
        "dataset_sha256": body["dataset_sha256"],
    }
    assert (await register(target, body)).status == 200
    assert (await register(target, body | {"seed": 12})).status == 409
    target.ingress.auth = BenchmarkAuth(mode="key", key=KEY + "-another-driver")
    other = {"X-Benchmark-Key": KEY + "-another-driver"}
    assert (await target.ingress.handle_runs("POST", other, body)).status == 403
    assert (await target.ingress.handle_messages("POST", other, message())).status == 403


@pytest.mark.parametrize("changes", [
    {"case_ids": ["unknown"]}, {"dataset_sha256": "0" * 64},
    {"expires_at": "2000-01-01T00:00:00Z"},
    {"max_requests": 21}, {"max_concurrency": 5},
    {"scenario": "production"}, {"callback_url": "https://teams.microsoft.com"},
])
async def test_unapproved_registration_never_admits(target, changes):
    assert (await register(target, registration(**changes))).status == 400
    assert not target.queue_transport.messages
    assert not any(name.startswith("payload/") for name in target.transport.blobs)


@pytest.mark.parametrize("headers", [
    {}, {"X-Benchmark-Key": "wrong"}, {"Authorization": "Bearer bot-token"},
    {"X-Benchmark-Key": KEY, "x-benchmark-key": KEY},
])
async def test_malformed_or_missing_auth_is_not_admission(target, headers):
    result = await target.ingress.handle_runs("POST", headers, registration())
    assert result.status == 401
    assert not target.transport.blobs


async def test_idempotent_ack_requires_durable_job_and_notification(target):
    assert (await register(target)).status == 201
    target.queue_transport.failures.append(("send", 503, "ServerBusy"))
    assert (await submit(target)).status == 503
    jobs = [name for name in target.transport.blobs if name.startswith("jobs/")]
    assert len(jobs) == 1
    result = await submit(target)
    assert result.status == 202
    assert result.body["request_id"] == "request-1"
    assert len(target.queue_transport.messages) == 1
    original = await target.jobs.load_payload(result.body["job_id"])
    assert original.target == "benchmark"
    assert original.run_id == "run-1"
    assert original.activity["serviceUrl"] == "https://benchmark.invalid"
    assert (await submit(target, message(case_id="kb-02"))).status == 409
    assert (await submit(target, message(virtual_user=1))).status == 409
    assert (await submit(target)).body == result.body


@pytest.mark.parametrize("changes", [
    {"case_id": "unknown"}, {"case_id": "web-01"}, {"virtual_user": 2},
    {"scenario": "real_agent"}, {"user_id": "real-user"},
    {"callback_url": "https://smba.trafficmanager.net"},
])
async def test_messages_cannot_change_workload_or_real_identity(target, changes):
    assert (await register(target)).status == 201
    assert (await submit(target, message(**changes))).status == 400
    assert not target.queue_transport.messages


async def test_independent_users_isolate_and_same_user_contends(target):
    assert (await register(target)).status == 201
    one, two = await asyncio.gather(
        submit(target), submit(target, message(request_id="request-2", virtual_user=1)))
    a = await target.jobs.load_payload(one.body["job_id"])
    b = await target.jobs.load_payload(two.body["job_id"])
    assert a.partition_key != b.partition_key
    assert a.activity["from"] != b.activity["from"]
    assert a.activity["conversation"] != b.activity["conversation"]
    assert (await register(target, registration(run_id="same", history_mode="same_user"))).status == 201
    one = await submit(target, message(run_id="same"))
    two = await submit(target, message(run_id="same", request_id="request-2"))
    a = await target.jobs.load_payload(one.body["job_id"])
    b = await target.jobs.load_payload(two.body["job_id"])
    assert a.partition_key == b.partition_key
    assert len(await target.jobs.pending(a.partition_key)) == 2
    assert (await submit(target, message(run_id="same", virtual_user=1))).status == 400


async def test_missing_approved_cases_has_no_example_fallback(target):
    from dataclasses import replace
    from teams_adapter.cloud.benchmark import BenchmarkIngress

    with pytest.raises(ValueError, match="dataset"):
        BenchmarkIngress(target.jobs, target.dispatcher, auth=target.auth,
                         settings=replace(target.settings, cases_path="missing-approved-cases.json"))


def test_target_dataset_uses_common_utf8_only_contract(tmp_path):
    from teams_adapter.cloud.benchmark import ApprovedDataset

    path = tmp_path / "benchmark-cases.json"
    path.write_bytes(CASES.read_text(encoding="utf-8").encode("utf-16"))
    with pytest.raises(ValueError, match="approved benchmark dataset"):
        ApprovedDataset.load(path)


def test_target_dataset_projection_preserves_shared_content_and_digest(tmp_path):
    from advisor_shared.benchmark import load_dataset
    from teams_adapter.cloud.benchmark import ApprovedDataset

    path = tmp_path / "benchmark-cases.json"
    path.write_bytes(CASES.read_bytes())
    shared = load_dataset(path)
    loaded = ApprovedDataset.load(path)
    assert loaded.digest == dataset_digest(shared)
    assert loaded.cases == {item["case_id"]: item["question"] for item in shared["cases"]}
    assert loaded.seeded_history == shared["seeded_history"]


@asynccontextmanager
async def observer(status=200):
    received = []

    async def receive(request):
        assert request.headers.get("X-Benchmark-Key") == KEY
        body = await request.json()
        received.append(body)
        disposition = "typing" if body["event"] == "typing" else "completed"
        return web.json_response({"disposition": disposition}, status=status)

    app = web.Application()
    app.router.add_post("/bench/replies", receive)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession(trust_env=False) as session:
            yield f"http://127.0.0.1:{port}/bench/replies", session, received
    finally:
        await runner.cleanup()


def make_executor(target, session, url, *, backend=None, host_telemetry=None):
    from advisor_agent.factory import AdvisorComponents, AzureAdvisor
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink, BenchmarkTurnExecutor

    class NoTokens:
        def get_token_provider_from_activity(self, *args):
            raise AssertionError("benchmark attempted a real Teams token operation")

    components = None
    if backend is not None:
        components = AdvisorComponents(AzureAdvisor(
            backend, channel_name="teams", azure_clients=(), credential_owner=None))
    sink = BenchmarkReplySink(
        session, url, auth_mode="key", key=KEY, allow_loopback=True)
    return BenchmarkTurnExecutor(
        target.ingress.runs, sink, components=components,
        connection_manager=NoTokens(), bot_app_id=BOT, host_telemetry=host_telemetry)


async def test_synthetic_uses_real_worker_checkpoint_and_local_sdk_http(target):
    from time import perf_counter
    from advisor_shared.benchmark import BenchmarkReply

    assert (await register(target, registration(max_requests=1))).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer() as (url, session, received):
        executor = make_executor(target, session, url)
        await executor.validate(payload)
        started = perf_counter()
        prepared = await executor.prepare(payload, [])
        assert perf_counter() - started >= 0.99
        assert not [item for item in received if item["event"] == "final"]
        # Serialization simulates a worker restart between prepare and send.
        from advisor_shared.cloud import PreparedTurn
        saved = PreparedTurn.model_validate_json(prepared.model_dump_json())
        restarted = make_executor(target, session, url)
        await restarted.send(payload, saved)
        final = BenchmarkReply.model_validate(
            next(item for item in received if item["event"] == "final"))
        assert final.outcome == "success"
        assert "synthetic" in final.reply.text
        assert "1000" in final.reply.text
        assert not final.telemetry.dependencies
        assert any(stage.name == "synthetic" and stage.duration_ms >= 990
                   for stage in final.telemetry.stages)
        assert not session.closed


async def test_budget_resumes_after_failed_receipt_and_does_not_grow_run_state(target):
    assert (await register(target, registration(max_requests=3, max_concurrency=1))).status == 201
    target.transport.failures.append(("create", 503, "ServerBusy"))
    assert (await submit(target)).status == 503
    run = await target.ingress.runs.load("run-1")
    assert run.used == 1 and len(run.active) == 1
    assert (await submit(target, message(request_id="blocked"))).status == 429
    async with observer() as (url, session, received):
        worker = SessionWorker(
            target.jobs, target.history, target.dispatcher, make_executor(target, session, url))
        for number in range(1, 4):
            request = message(request_id=f"request-{number}")
            result = await submit(target, request)
            assert result.status == 202
            payload = await target.jobs.load_payload(result.body["job_id"])
            assert await worker.process_session(payload.partition_key, payload.trace_id) == "drained"
            assert (await target.jobs.read(payload.job_id)).state == "completed"
            assert (await submit(target, request)).body == result.body
            assert len((await target.ingress.runs.load("run-1")).active) <= 1
        assert (await submit(target, message(request_id="over-budget"))).status == 429
        run = await target.ingress.runs.load("run-1")
        assert run.used == 3
        assert len([item for item in received if item["event"] == "final"]) == 3
    assert not any("teams.microsoft" in call.url for _, call in target.transport.calls)


async def test_real_pipeline_seeds_exactly_once_and_send_never_regenerates(target):
    class Backend:
        def __init__(self):
            self.histories = []

        async def run(self, user_text, history, images=None):
            self.histories.append(history)
            assert images is None
            return "The complete rendered answer with [a link](https://example.invalid/doc)."

    backend = Backend()
    assert (await register(target, registration(
        history_mode="seeded20", scenario="real_agent"))).status == 201
    result = await submit(target, message(scenario="real_agent"))
    payload = await target.jobs.load_payload(result.body["job_id"])
    async with observer() as (url, session, received):
        executor = make_executor(target, session, url, backend=backend)
        first = await executor.prepare(payload, [])
        # A preparation retry reuses the bootstrap, not twenty additional turns.
        second = await executor.prepare(payload, [])
        expected = json.loads(CASES.read_text(encoding="utf-8"))["seeded_history"]
        assert backend.histories == [expected, expected]
        assert len([name for name in target.transport.blobs
                    if name.startswith("benchmark/seeds/")]) == 1
        assert not [item for item in received if item["event"] == "final"]
        await executor.send(payload, second)
        await executor.send(payload, first)
        assert len(backend.histories) == 2
        finals = [item for item in received if item["event"] == "final"]
        assert len(finals) == 2
        assert finals[0]["reply"]["text"].endswith("(https://example.invalid/doc).")
        assert any(stage["name"] == "history_setup" for stage in finals[0]["telemetry"]["stages"])
        assert not session.closed


async def test_tampered_synthetic_payload_fails_before_private_history_or_network(target):
    from advisor_shared.delivery import DeliveryFailure

    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer() as (url, session, received):
        executor = make_executor(target, session, url)
        for change in (
            {"target": "teams"}, {"partition_key": "real-user-partition"},
            {"activity": payload.activity | {"serviceUrl": "https://smba.trafficmanager.net"}},
            {"activity": payload.activity | {"attachments": [
                {"contentType": "image/png", "contentUrl": "https://teams.microsoft.com/private"}]}},
        ):
            with pytest.raises(DeliveryFailure) as error:
                await executor.validate(payload.model_copy(update=change))
            assert error.value.definitely_not_sent
            assert not error.value.retryable
            assert str(error.value) == "invalid_payload"
        assert received == []


@pytest.mark.parametrize("status,definite,retryable", [(429, True, True), (401, True, False), (503, False, False)])
async def test_callback_failures_follow_shared_worker_delivery_contract(target, status, definite, retryable):
    from advisor_shared.cloud import PreparedTurn
    from advisor_shared.delivery import DeliveryFailure
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink

    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer(status) as (url, session, received):
        sink = BenchmarkReplySink(session, url, auth_mode="key", key=KEY, allow_loopback=True)
        prepared = PreparedTurn(reply={"type": "message", "text": "private-answer"}, history=[], outcome="success")
        with pytest.raises(DeliveryFailure) as error:
            await sink.send(payload, prepared)
        assert error.value.definitely_not_sent is definite
        assert error.value.retryable is retryable
        assert "private-answer" not in str(error.value)


async def test_ignored_no_message_reports_final_without_inventing_answer(target):
    from advisor_shared.cloud import PreparedTurn
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink

    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer() as (url, session, received):
        sink = BenchmarkReplySink(session, url, auth_mode="key", key=KEY, allow_loopback=True)
        await sink.send(payload, PreparedTurn(reply=None, history=[], outcome="ignored"))
        assert received[0]["outcome"] == "ignored"
        assert received[0].get("reply") is None


async def test_local_http_admission_preserves_fixed_identity_and_auth(target):
    async def runs(request):
        result = await target.ingress.handle_runs(request.method, request.headers, await request.read())
        return web.json_response(result.body, status=result.status, headers=result.headers)

    async def messages(request):
        result = await target.ingress.handle_messages(request.method, request.headers, await request.read())
        return web.json_response(result.body, status=result.status, headers=result.headers)

    app = web.Application()
    app.router.add_post("/bench/runs", runs)
    app.router.add_post("/bench/messages", messages)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession(trust_env=False) as session:
            async with session.post(base + "/bench/runs", json=registration()) as response:
                assert response.status == 401
            async with session.post(base + "/bench/runs", json=registration(), headers=HEADERS) as response:
                assert response.status == 201
            async with session.post(base + "/bench/messages", json=message(), headers=HEADERS) as response:
                assert response.status == 202
                ack = await response.json()
            payload = await target.jobs.load_payload(ack["job_id"])
            assert payload.activity["from"]["id"].startswith("bench:")
            assert payload.activity["channelData"]["tenant"]["id"].startswith("bench:")
            assert payload.activity["conversation"]["id"] == "bench:run-1:request-1"
            async with session.post(
                base + "/bench/messages", data='{"run_id":"run-1","run_id":"other"}', headers=HEADERS,
            ) as response:
                assert response.status == 400
    finally:
        await runner.cleanup()


async def test_concurrent_reservations_cannot_exceed_run_limit(target):
    assert (await register(target, registration(max_requests=2, max_concurrency=2))).status == 201
    outcomes = await asyncio.gather(*(
        submit(target, message(request_id=f"race-{index}", virtual_user=index % 2))
        for index in range(12)
    ))
    assert sorted(result.status for result in outcomes) == [202, 202] + [429] * 10
    run = await target.ingress.runs.load("run-1")
    assert run.used == len(run.active) == 2
    assert len([name for name in target.transport.blobs if name.startswith("payload/")]) == 2


@pytest.fixture(scope="module")
def signer():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwt_auth(signer, *, outage=False):
    import jwt
    from advisor_shared.entra_api import EntraApiValidator
    from teams_adapter.cloud.benchmark import BenchmarkAuth

    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signer.public_key()))
    public.update(kid="benchmark-local", alg="RS256")

    async def key_provider(token):
        if outage:
            raise jwt.PyJWKClientConnectionError("private-upstream-error")
        return jwt.PyJWK.from_dict(public)

    return BenchmarkAuth(validator=EntraApiValidator(
        tenant_id="00000000-0000-0000-0000-000000000001",
        audience="00000000-0000-0000-0000-000000000002",
        allowed_client_ids=["00000000-0000-0000-0000-000000000003"],
        allowed_principal_ids=["00000000-0000-0000-0000-000000000004"],
        key_provider=key_provider, leeway_seconds=0))


def jwt_token(signer, **changes):
    import jwt
    from time import time

    now = int(time())
    claims = {
        "aud": "00000000-0000-0000-0000-000000000002",
        "iss": "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000001/v2.0",
        "tid": "00000000-0000-0000-0000-000000000001",
        "azp": "00000000-0000-0000-0000-000000000003",
        "oid": "00000000-0000-0000-0000-000000000004",
        "roles": ["Benchmark.Invoke"], "exp": now + 120, "iat": now - 1, "nbf": now - 1,
    }
    return jwt.encode(claims | changes, signer, algorithm="RS256", headers={"kid": "benchmark-local"})


@pytest.mark.parametrize("claims", [
    {"aud": "api://00000000-0000-0000-0000-000000000002"},
    {"aud": "https://api.botframework.com"}, {"roles": []}, {"exp": 1},
    {"iss": "https://api.botframework.com"},
    {"azp": "00000000-0000-0000-0000-000000000099"},
    {"oid": "00000000-0000-0000-0000-000000000099"},
])
async def test_real_entra_verifier_never_accepts_bot_scope_or_wrong_identity(target, signer, claims, caplog):
    target.ingress.auth = jwt_auth(signer)
    bearer = jwt_token(signer, **claims)
    response = await target.ingress.handle_runs(
        "POST", HEADERS | {"Authorization": f"Bearer {bearer}"}, registration())
    assert response.status == 401
    assert bearer not in caplog.text
    assert not target.transport.blobs


async def test_entra_outage_does_not_downgrade_to_supplied_key(target, signer, caplog):
    target.ingress.auth = jwt_auth(signer, outage=True)
    bearer = jwt_token(signer)
    assert (await target.ingress.handle_runs(
        "POST", HEADERS | {"Authorization": f"Bearer {bearer}"}, registration())).status == 503
    assert bearer not in caplog.text
    assert "private-upstream-error" not in caplog.text
    target.ingress.auth = jwt_auth(signer)
    assert (await target.ingress.handle_runs(
        "POST", {"Authorization": f"Bearer {bearer}"}, registration())).status == 201


async def test_callback_entra_uses_api_guid_default_scope_without_fallback(target):
    from azure.core.credentials import AccessToken
    from advisor_shared.cloud import PreparedTurn
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink

    class Credential:
        def __init__(self):
            self.scopes = []

        async def get_token(self, scope):
            self.scopes.append(scope)
            return AccessToken("local-observer-token", 9999999999)

    credential = Credential()
    received = []

    async def receive(request):
        assert request.headers["Authorization"] == "Bearer local-observer-token"
        assert "X-Benchmark-Key" not in request.headers
        received.append(await request.json())
        return web.json_response({"disposition": "completed"})

    app = web.Application()
    app.router.add_post("/bench/replies", receive)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        assert (await register(target)).status == 201
        ack = await submit(target)
        payload = await target.jobs.load_payload(ack.body["job_id"])
        async with ClientSession(trust_env=False) as session:
            sink = BenchmarkReplySink(
                session, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/bench/replies",
                credential=credential, api_app_id="00000000-0000-0000-0000-000000000002",
                allow_loopback=True)
            await sink.send(payload, PreparedTurn(reply=None, history=[], outcome="ignored"))
        assert credential.scopes == ["api://00000000-0000-0000-0000-000000000002/.default"]
        assert received[0]["outcome"] == "ignored"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("url", [
    "http://example.invalid/bench/replies", "https://user:password@example.invalid/bench/replies",
    "https://example.invalid/bench/replies?secret=1",
    "https://smba.trafficmanager.net/bench/replies",
    "https://api.botframework.com/bench/replies", "https://teams.microsoft.com/bench/replies",
])
async def test_callback_configuration_cannot_select_real_teams(url):
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink
    async with ClientSession(trust_env=False) as session:
        with pytest.raises(ValueError, match="callback"):
            BenchmarkReplySink(session, url, auth_mode="key", key=KEY, allow_loopback=True)


async def test_sdk_proactive_transport_delivers_complete_text_only_to_fixed_observer(target):
    from microsoft_agents.activity import Activity
    from teams_adapter.cloud.auth import ManagedCloudAdapter
    from teams_adapter.cloud.benchmark_connector import BenchmarkConnectorFactory, BenchmarkReplySink

    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer() as (url, session, received):
        sink = BenchmarkReplySink(session, url, auth_mode="key", key=KEY, allow_loopback=True)
        factory = BenchmarkConnectorFactory(sink, bot_app_id=BOT)
        adapter = ManagedCloudAdapter(channel_service_client_factory=factory)

        async def deliver(context):
            await context.send_activity(Activity(type="message", text="The complete reply."))

        with factory.bind(payload, phase="send", outcome="success"):
            await adapter.continue_conversation(BOT, Activity.model_validate(payload.activity), deliver)
        assert received[0]["reply"] == {
            "type": "message", "format": "markdown", "text": "The complete reply.",
        }
        assert received[0]["outcome"] == "success"
        assert received[0]["request_id"] == "request-1"
        assert not session.closed


async def test_complete_chunked_observer_ack_is_accepted(target):
    from advisor_shared.cloud import PreparedTurn
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink

    async def receive(request):
        await request.read()
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(b'{"disposition":')
        await asyncio.sleep(0.05)
        await response.write(b'"completed"}')
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/bench/replies", receive)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        assert (await register(target)).status == 201
        ack = await submit(target)
        payload = await target.jobs.load_payload(ack.body["job_id"])
        async with ClientSession(trust_env=False) as session:
            sink = BenchmarkReplySink(
                session, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/bench/replies",
                auth_mode="key", key=KEY, allow_loopback=True)
            receipt = await sink.send(payload, PreparedTurn(reply=None, history=[], outcome="ignored"))
            assert receipt == payload.job_id
    finally:
        await runner.cleanup()


async def test_synthetic_backend_is_async_fixed_wait_not_real_agent_fallback():
    from time import perf_counter
    from teams_adapter.cloud.benchmark_connector import _SyntheticBackend

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.05)
            ticks += 1

    started = perf_counter()
    answer, _ = await asyncio.gather(_SyntheticBackend().run("approved question", []), heartbeat())
    assert perf_counter() - started >= 0.99
    assert ticks == 5
    assert "synthetic" in answer and "no model invocation" in answer


async def test_expired_run_rejects_admission_without_releasing_a_budget_slot(target, monkeypatch):
    body = registration()
    assert (await register(target, body)).status == 201
    future = datetime.fromisoformat(body["expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(target.jobs, "now", lambda: future)
    assert (await submit(target)).status == 410
    run = await target.ingress.runs.load("run-1")
    assert run.used == 0
    assert not target.queue_transport.messages


def test_explicit_env_auth_does_not_infer_a_key_or_uri_audience():
    from teams_adapter.cloud.benchmark import BenchmarkAuth, BenchmarkSettings

    assert BenchmarkSettings.from_env({}).enabled is False
    with pytest.raises(ValueError):
        BenchmarkAuth.from_env({"BENCHMARK_API_KEY": KEY})
    with pytest.raises(ValueError):
        BenchmarkAuth.from_env({
            "BENCHMARK_API_APP_ID": "api://00000000-0000-0000-0000-000000000002"})
    assert BenchmarkAuth.from_env({
        "BENCHMARK_AUTH_MODE": "key", "BENCHMARK_API_KEY": KEY}).mode == "key"


async def test_no_visible_success_is_reported_unknown_not_fake_success(target):
    from advisor_shared.cloud import PreparedTurn
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink

    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer() as (url, session, received):
        sink = BenchmarkReplySink(session, url, auth_mode="key", key=KEY, allow_loopback=True)
        await sink.send(payload, PreparedTurn(reply=None, history=[], outcome="success"))
        assert received[0]["outcome"] == "unknown"
        assert received[0]["reply"] is None


async def test_canonical_deployment_auth_names_are_sufficient(signer):
    import jwt
    from teams_adapter.cloud.benchmark import BenchmarkAuth

    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signer.public_key()))
    public.update(kid="benchmark-local", alg="RS256")

    async def key_provider(token):
        return jwt.PyJWK.from_dict(public)

    env = {
        "BENCHMARK_TENANT_ID": "00000000-0000-0000-0000-000000000001",
        "BENCHMARK_API_CLIENT_ID": "00000000-0000-0000-0000-000000000002",
        "BENCHMARK_ALLOWED_CLIENT_IDS": "00000000-0000-0000-0000-000000000003",
        "BENCHMARK_ALLOWED_PRINCIPAL_IDS": "00000000-0000-0000-0000-000000000004",
    }
    auth = BenchmarkAuth.from_env(env, key_provider=key_provider)
    principal = await auth.verify({"Authorization": f"Bearer {jwt_token(signer)}"})
    assert principal.object_id == env["BENCHMARK_ALLOWED_PRINCIPAL_IDS"]
    with pytest.raises(ValueError, match="conflicting"):
        BenchmarkAuth.from_env(env | {
            "BENCHMARK_API_APP_ID": "00000000-0000-0000-0000-000000000099"})


async def test_ingress_storage_unavailable_has_safe_observable_reason(target, caplog):
    assert (await register(target)).status == 201
    target.transport.failures.append(("read", 503, "ServerBusy"))
    result = await submit(target)
    assert result.status == 503
    records = [record for record in caplog.records if record.name.endswith(".benchmark")]
    assert records
    assert records[-1].telemetry["reason"] == "admission_unavailable"
    assert records[-1].telemetry["error_type"] == "HttpResponseError"
    assert KEY not in caplog.text
    assert "injected contract failure" not in caplog.text
    assert (await submit(target)).status == 202


async def test_final_callback_can_submit_next_question_before_job_completed(target):
    from advisor_shared.cloud import PreparedTurn
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink

    assert (await register(target, registration(max_requests=3, max_concurrency=1))).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    observed = []

    async def receive(request):
        body = await request.json()
        assert body["event"] == "final"
        observed.append((await target.jobs.read(payload.job_id)).state)
        followup = await submit(target, message(request_id="followup"))
        observed.append(followup.status)
        return web.json_response({"disposition": "completed"})

    app = web.Application()
    app.router.add_post("/bench/replies", receive)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    prepared = PreparedTurn(reply={"type": "message", "text": "complete"}, history=[], outcome="success")
    try:
        async with target.jobs.leased_job(payload.job_id) as job:
            await job.mark_running()
            await job.save_result(prepared)
            await job.mark_sending()
            async with ClientSession(trust_env=False) as session:
                sink = BenchmarkReplySink(
                    session, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/bench/replies",
                    auth_mode="key", key=KEY, allow_loopback=True)
                await sink.send(payload, prepared)
            assert observed == ["sending", 202]
            assert (await submit(target, message(request_id="third"))).status == 429
            assert (await target.ingress.runs.load("run-1")).used == 2
            await job.complete(payload.job_id)
    finally:
        await runner.cleanup()


async def test_validation_storage_503_does_not_permanently_fail_accepted_job(target):
    from azure.core.exceptions import HttpResponseError

    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer() as (url, session, received):
        executor = make_executor(target, session, url)
        target.transport.failures.append(("read", 503, "ServerBusy"))
        with pytest.raises(HttpResponseError) as error:
            await executor.validate(payload)
        assert error.value.status_code == 503
        await executor.validate(payload)
        assert not (await target.jobs.read(payload.job_id)).terminal
        assert received == []


async def test_ingress_persists_host_observed_boot_snapshot_not_request_counter(target):
    from advisor_shared.benchmark import BootObservation, SafeTelemetry
    from teams_adapter.cloud.benchmark import BenchmarkIngress

    snapshot = SafeTelemetry(boots=[BootObservation(
        group="http", boot_id="actual-test-process-boot",
        instance_id="actual-test-instance", first_request=False)])
    target.ingress = BenchmarkIngress(
        target.jobs, target.dispatcher, settings=target.settings, auth=target.auth,
        telemetry_provider=lambda: snapshot)
    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    _, reserved = await target.ingress.runs.validate_payload(payload)
    assert reserved.telemetry.boots == snapshot.boots
    assert reserved.telemetry.boots[0].first_request is False
    assert reserved.telemetry.stages[0].name == "auth"
    assert reserved.telemetry.stages[0].duration_ms >= 0


async def test_real_core_local_http_observations_exclude_bodies_and_unknown_metrics():
    from advisor_agent.factory import AdvisorComponents, AzureAdvisor
    from advisor_agent.search.telemetry import SearchTrace
    from advisor_agent.sessions import InMemorySessionStore
    from advisor_shared.messages import AdvisorRequest
    from advisor_shared.telemetry import step, trace_scope
    from teams_adapter.cloud.benchmark_observations import (
        ObservedComponents, capture_preparation, preparation_telemetry,
    )

    requests = []

    async def source(request):
        requests.append(request.path)
        return web.json_response({"answer": "private-answer", "input_tokens": 13, "output_tokens": 5})

    app = web.Application()
    app.router.add_get("/model-fixture", source)
    app.router.add_get("/web-fixture", source)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession(trust_env=False) as session:
            class Backend:
                async def run(self, user_text, history, images=None):
                    async def retrieve():
                        async with session.get(base + "/web-fixture") as response:
                            await response.read()
                            return []

                    await SearchTrace("web", "local-fixture", 2).run(retrieve())
                    with step("llm.completion", model="private-deployment") as timing:
                        async with session.get(base + "/model-fixture") as response:
                            body = await response.json()
                            timing.attributes.update(
                                input_tokens=body["input_tokens"], output_tokens=body["output_tokens"])
                            return body["answer"]

            owner = AdvisorComponents(AzureAdvisor(
                Backend(), channel_name="benchmark", azure_clients=(), credential_owner=None))
            with capture_preparation() as observed, trace_scope(new=True):
                core = ObservedComponents(owner).new_core(InMemorySessionStore(), event_sink=lambda event: None)
                await core.handle(AdvisorRequest(
                    text="private-question", conversation_key="private-key", channel_id="private-channel",
                    user_id="private-user", user_name="private-name", is_group=False))
            telemetry = preparation_telemetry(observed)
            assert requests == ["/web-fixture", "/model-fixture"]
            assert telemetry.model_rounds == 1
            assert (telemetry.input_tokens, telemetry.output_tokens) == (13, 5)
            assert telemetry.dependencies[0].model_dump() == {"dependency": "web", "calls": 1, "throttled": 0}
            assert {stage.name for stage in telemetry.stages} >= {"model", "web", "agent"}
            assert "private-" not in telemetry.model_dump_json()
            assert telemetry.cache_hit is None and telemetry.pending_jobs is None
            assert telemetry.boots == []
            assert not any(item.dependency == "model" for item in telemetry.dependencies)
    finally:
        await runner.cleanup()


async def test_delivery_handoff_window_is_bounded_and_never_refunds_budget(target):
    from advisor_shared.cloud import PreparedTurn

    assert (await register(target, registration(max_requests=3, max_concurrency=1))).status == 201
    prepared = PreparedTurn(reply={"type": "message", "text": "complete"}, history=[], outcome="success")
    first = await submit(target)
    async with target.jobs.leased_job(first.body["job_id"]) as first_job:
        await first_job.mark_running()
        await first_job.save_result(prepared)
        await first_job.mark_sending()
        second = await submit(target, message(request_id="second"))
        assert second.status == 202
        async with target.jobs.leased_job(second.body["job_id"]) as second_job:
            await second_job.mark_running()
            await second_job.save_result(prepared)
            await second_job.mark_sending()
            assert (await submit(target, message(request_id="third"))).status == 429
            assert len((await target.ingress.runs.load("run-1")).active) == 2
            await first_job.complete(first_job.job_id)
            assert (await submit(target, message(request_id="third"))).status == 202
            await second_job.complete(second_job.job_id)
            assert (await submit(target, message(request_id="fourth"))).status == 429
    assert (await target.ingress.runs.load("run-1")).used == 3


async def test_worker_releases_transient_validation_error_without_terminal_failure(target, monkeypatch):
    from azure.core.exceptions import HttpResponseError

    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    original = target.gateway.read
    failed = False

    async def read_with_one_run_outage(name):
        nonlocal failed
        if name == target.ingress.runs.run_name("run-1") and not failed:
            failed = True
            target.transport.failures.append(("read", 503, "ServerBusy"))
        return await original(name)

    async with observer() as (url, session, received):
        executor = make_executor(target, session, url)
        worker = SessionWorker(target.jobs, target.history, target.dispatcher, executor)
        monkeypatch.setattr(target.gateway, "read", read_with_one_run_outage)
        with pytest.raises(HttpResponseError):
            await worker.process_session(payload.partition_key, payload.trace_id)
        record = await target.jobs.read(payload.job_id)
        assert not record.terminal and record.generation_attempts == 0
        assert received == []
        assert await worker.process_session(payload.partition_key, payload.trace_id) == "drained"
        assert (await target.jobs.read(payload.job_id)).state == "completed"


async def test_worker_checkpoint_carries_actual_ingress_and_worker_boot_snapshots(target):
    from advisor_shared.benchmark import BootObservation, SafeTelemetry
    from teams_adapter.cloud.benchmark import BenchmarkIngress

    ingress = SafeTelemetry(boots=[BootObservation(
        group="http", boot_id="ingress-process-boot", instance_id="ingress-instance", first_request=True)])
    worker_snapshot = SafeTelemetry(boots=[BootObservation(
        group="worker", boot_id="worker-process-boot", instance_id="worker-instance", first_request=True)])
    target.ingress = BenchmarkIngress(
        target.jobs, target.dispatcher, settings=target.settings, auth=target.auth,
        telemetry_provider=lambda: ingress)
    assert (await register(target)).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer() as (url, session, received):
        executor = make_executor(target, session, url, host_telemetry=lambda payload: worker_snapshot)
        worker = SessionWorker(target.jobs, target.history, target.dispatcher, executor)
        assert await worker.process_session(payload.partition_key, payload.trace_id) == "drained"
        final = next(item for item in received if item["event"] == "final")
        assert final["telemetry"]["boots"] == [
            ingress.boots[0].model_dump(), worker_snapshot.boots[0].model_dump()]
        assert final["telemetry"]["business_attempts"] == 1
        assert final["telemetry"]["delivery_attempts"] == 1
        assert final["telemetry"]["model_rounds"] is None
        assert final["telemetry"]["input_tokens"] is None


async def test_synthetic_stage_measures_wait_separately_from_pipeline_work():
    from advisor_shared.telemetry import step, trace_scope
    from teams_adapter.cloud.benchmark_connector import _SyntheticBackend
    from teams_adapter.cloud.benchmark_observations import PreparationObservations, preparation_telemetry

    with trace_scope(new=True) as trace, step("agent.turn"):
        await asyncio.sleep(0.06)
        await _SyntheticBackend().run("approved", [])
    telemetry = preparation_telemetry(PreparationObservations(trace=trace), synthetic=True)
    stages = {stage.name: stage.duration_ms for stage in telemetry.stages}
    assert {"agent", "synthetic"} <= stages.keys()
    assert stages["synthetic"] >= 990
    assert stages["agent"] - stages["synthetic"] >= 50
    assert telemetry.model_rounds is None and telemetry.input_tokens is None
    assert telemetry.dependencies == [] and telemetry.paths == []

import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import azure.functions as func
import pytest

from advisor_shared.cloud import TaskPayload
from test_aca_lifecycle import runtime_case
from test_executor import payload_for


def tracker(**kwargs):
    from teams_adapter.cloud.observability import HostObservations
    return HostObservations(environment={"WEBSITE_INSTANCE_ID": "observed-instance"}, **kwargs)


def test_snapshots_are_read_only_and_never_manufacture_first_requests():
    observations = tracker()
    assert observations.snapshot().boots == []
    assert observations.snapshot().boots == []
    with observations.invocation("http", host_kind="functions") as invocation:
        first = observations.snapshot()
        assert first.boots[0].first_request is True
        assert observations.snapshot() == first
        assert first.boots[0].instance_id == "observed-instance"
        first.boots.clear()
        assert len(observations.snapshot().boots) == 1
    assert observations.snapshot().boots == []
    assert invocation.snapshot().boots[0].first_request is True
    with observations.invocation("http", host_kind="functions"):
        assert observations.snapshot().boots[0].first_request is False


def test_ordinary_work_and_maintenance_consume_first_workload_observation():
    observations = tracker()
    with observations.invocation("http", host_kind="functions"):
        assert observations.snapshot().boots[0].first_request
    with observations.invocation("maintenance", host_kind="functions"):
        assert observations.snapshot().boots == []
    with observations.invocation("worker", host_kind="functions"):
        snapshot = observations.snapshot()
        assert snapshot.boots[0].group == "worker"
        assert snapshot.boots[0].first_request is False


async def test_concurrent_invocations_have_one_observed_first_flag():
    observations = tracker()
    entered = asyncio.Event()
    count = 0

    async def invoke():
        nonlocal count
        with observations.invocation("http", host_kind="functions"):
            count += 1
            if count == 2:
                entered.set()
            await entered.wait()
            return observations.snapshot()

    a, b = await asyncio.gather(invoke(), invoke())
    assert [a.boots[0].first_request, b.boots[0].first_request] == [True, False]
    assert a.boots[0].boot_id == b.boots[0].boot_id
    assert a.trace_id != b.trace_id


def test_invalid_instance_metadata_is_omitted_without_disclosure(caplog):
    from teams_adapter.cloud.observability import HostObservations
    observations = HostObservations(
        environment={"WEBSITE_INSTANCE_ID": "https://private-host/?token=private-token"})
    with observations.invocation("http", host_kind="functions"):
        assert observations.snapshot().boots == []
    assert "private-host" not in caplog.text
    assert "private-token" not in caplog.text


def test_missing_platform_instance_does_not_invent_a_hostname_based_boot(monkeypatch):
    from teams_adapter.cloud.observability import HostObservations
    monkeypatch.setattr("socket.gethostname", lambda: "actual-local-hostname")
    observations = HostObservations(environment={})
    with observations.invocation("http", host_kind="functions") as invocation:
        assert invocation.first_request is True
        assert observations.snapshot().boots == []
        assert observations.identity()["instance_id"] is None
        assert observations.identity()["instance_source"] is None
    assert observations.identity()["boot_id"]


def test_completed_host_stages_use_real_clock_measurements_and_exclude_open_intervals():
    from teams_adapter.cloud.observability import observe_host_stage
    clock = SimpleNamespace(now=100.0)
    observations = tracker(clock=lambda: clock.now)
    with observations.invocation("http", host_kind="functions") as invocation:
        with observe_host_stage("connection"):
            clock.now += 0.125
            assert observations.snapshot().stages == []
        assert [(s.name, s.duration_ms) for s in observations.snapshot().stages] == [
            ("connection", 125.0)]
        clock.now += 0.375
        invocation.http_status(202)
    assert [(s.name, s.duration_ms) for s in invocation.snapshot().stages] == [
        ("connection", 125.0), ("ingress", 500.0)]


async def test_worker_init_is_observed_only_for_actual_once_per_process_setup(runtime_case):
    from teams_adapter.cloud.observability import observe_job
    observations = tracker()
    payload = payload_for()
    try:
        with observations.invocation("worker", host_kind="functions"):
            await runtime_case.runtime.get_worker()
            observe_job(payload)
            first = runtime_case.runtime.host_telemetry(payload)
            assert first.boots[0].first_request
            assert len([s for s in first.stages if s.name == "worker_init"]) == 1
            assert runtime_case.runtime.host_telemetry(payload) == first
            later = payload.model_copy(update={"job_id": "second-real-job"})
            observe_job(later)
            second = runtime_case.runtime.host_telemetry(later)
            assert not second.boots[0].first_request
            assert not any(s.name == "worker_init" for s in second.stages)
        with observations.invocation("worker", host_kind="functions"):
            await runtime_case.runtime.get_worker()
            observe_job(payload)
            warm = runtime_case.runtime.host_telemetry(payload)
            assert not warm.boots[0].first_request
            assert not any(s.name == "worker_init" for s in warm.stages)
        assert runtime_case.state.builds == 1
    finally:
        await runtime_case.runtime.aclose()


async def test_functions_probes_preserve_first_workload_but_auth_attempts_consume_it(monkeypatch):
    from teams_adapter.cloud import functions, observability
    from teams_adapter.cloud.ingress import HttpResult
    observations = tracker()
    monkeypatch.setattr(observability, "_observations", observations)
    seen = []

    class Ingress:
        async def handle_http(self, *args):
            seen.append(functions.current_http_telemetry())
            return HttpResult(401)

    monkeypatch.setattr(functions, "get_ingress", lambda: Ingress())
    health = func.HttpRequest(method="GET", url="http://localhost/healthz", body=b"")
    assert (await functions.handle_function_health(health)).status_code == 200
    req = func.HttpRequest(method="POST", url="http://localhost/api/messages", body=b"{}")
    assert (await functions.handle_function_request(req)).status_code == 401
    assert (await functions.handle_function_request(req)).status_code == 401
    assert seen[0].boots[0].group == "http"
    assert [item.boots[0].first_request for item in seen] == [True, False]


async def test_aca_middleware_observes_all_routes_including_future_registration(runtime_case, monkeypatch):
    from teams_adapter.app import create_cloud_app
    from teams_adapter.cloud import observability
    observations = tracker()
    monkeypatch.setattr(observability, "_observations", observations)

    class EmptyQueue:
        async def receive(self):
            if False:
                yield

    runtime_case.runtime.queue = EmptyQueue()
    app = create_cloud_app(
        runtime_case.runtime.ingress, runtime_case.runtime.get_worker, runtime=runtime_case.runtime)
    seen = []

    async def registration(request):
        seen.append(observability.current_host_telemetry())
        return web.Response(status=201)

    app.router.add_post("/test-registration-boundary", registration)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/healthz")).status == 200
        assert (await client.post("/test-registration-boundary")).status == 201
        assert (await client.get("/readyz")).status == 200
        assert (await client.post("/test-registration-boundary")).status == 201
    assert seen[0].boots[0].group == "app"
    assert [item.boots[0].first_request for item in seen] == [True, False]


async def test_aca_startup_and_readiness_probes_preserve_only_first_actual_message(
        runtime_case, monkeypatch, caplog):
    from teams_adapter.app import create_cloud_app
    from teams_adapter.cloud import observability
    from teams_adapter.cloud.ingress import HttpResult
    observations = tracker()
    monkeypatch.setattr(observability, "_observations", observations)
    seen = []

    class EmptyQueue:
        async def receive(self):
            if False:
                yield

    class Ingress:
        async def handle_http(self, *args):
            first = observability.current_host_telemetry()
            assert observability.current_host_telemetry() == first
            seen.append(first)
            return HttpResult(202)

    runtime_case.runtime.queue = EmptyQueue()
    app = create_cloud_app(Ingress(), runtime_case.runtime.get_worker, runtime=runtime_case.runtime)
    with caplog.at_level("INFO"):
        async with TestClient(TestServer(app)) as client:
            for path in ("/healthz", "/healthz", "/readyz"):
                assert (await client.get(path)).status == 200
            assert (await client.post("/api/messages", json={})).status == 202
            assert (await client.get("/healthz")).status == 200
            assert (await client.post("/api/messages", json={})).status == 202
    assert [item.boots[0].first_request for item in seen] == [True, False]
    assert seen[0].boots[0].boot_id == seen[1].boots[0].boot_id
    completed = [record.telemetry for record in caplog.records
                 if record.msg == "host_invocation_completed"]
    probes = [item for item in completed if item["probe"]]
    assert len(probes) == 4
    assert all(item["workload_index"] is None and not item["first_request"] for item in probes)


@pytest.mark.parametrize("kind", ["http", "worker", "maintenance"])
def test_probes_never_reset_history_after_any_prior_workload(kind):
    observations = tracker()
    with observations.invocation(kind, host_kind="aca"):
        assert observations.snapshot().boots[0].first_request
    for _ in range(3):
        with observations.invocation("http", host_kind="aca", workload=False):
            assert observations.snapshot().boots == []
            assert observations.snapshot().boots == []
    with observations.invocation("http", host_kind="aca"):
        assert observations.snapshot().boots[0].first_request is False


def test_failed_observation_logging_never_leaks_request_context(monkeypatch):
    from teams_adapter.cloud import observability
    observations = tracker()

    def unavailable(*args, **kwargs):
        raise RuntimeError("log sink unavailable")

    monkeypatch.setattr(observability.logger, "info", unavailable)
    with pytest.raises(RuntimeError, match="log sink unavailable"):
        with observations.invocation("http", host_kind="functions"):
            pass
    assert observability.current_host_telemetry().boots == []


async def test_worker_setup_failure_is_observed_without_exception_text(runtime_case):
    from teams_adapter.cloud.observability import observe_job
    observations = tracker()
    payload = payload_for()

    async def failed_setup():
        raise RuntimeError("private-setup-details")

    runtime_case.runtime._worker_factory = failed_setup
    try:
        with observations.invocation("worker", host_kind="functions"):
            with pytest.raises(RuntimeError):
                await runtime_case.runtime.get_worker()
            observe_job(payload)
            snapshot = runtime_case.runtime.host_telemetry(payload)
            assert [(stage.name, stage.outcome) for stage in snapshot.stages] == [
                ("worker_init", "failed")]
            assert snapshot.input_tokens is None and snapshot.output_tokens is None
            assert "private-setup-details" not in snapshot.model_dump_json()
    finally:
        await runtime_case.runtime.aclose()

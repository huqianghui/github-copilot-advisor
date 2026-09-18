import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import threading
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
import pytest

from advisor_shared.cloud import QueueWake

CONFIG = {
    "ADVISOR_STORAGE_AUTH_MODE": "key",
    "ADVISOR_STORAGE_ACCOUNT_NAME": "offline",
    "ADVISOR_STORAGE_ACCOUNT_KEY": "b2ZmbGluZQ==",
    "ADVISOR_BLOB_SERVICE_URL": "http://127.0.0.1:10000/offline",
    "ADVISOR_QUEUE_SERVICE_URL": "http://127.0.0.1:10001/offline",
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": "offline-app",
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "offline-tenant",
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET": "offline-not-a-secret",
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED": "False",
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__VALIDATE_ISSUER": "True",
}


class DeliveryBoundary:
    body = QueueWake(partition_key="partition", trace_id="trace-worker").model_dump_json().encode()

    def __init__(self):
        self.actions = []
        self.finished = asyncio.Event()
        self.lost = asyncio.Event()

    async def __aenter__(self):
        self.actions.append("enter")
        return self

    async def __aexit__(self, *args):
        self.actions.append("exit")

    async def ack(self):
        self.actions.append("ack")
        self.finished.set()

    async def release(self, delay):
        self.actions.append(("release", delay))
        self.finished.set()


@pytest.fixture
def runtime_case():
    from teams_adapter.cloud.runtime import CloudRuntime
    delivery = DeliveryBoundary()
    started, stopped = asyncio.Event(), asyncio.Event()
    state = SimpleNamespace(builds=0, closes=0, result="drained", error=None, block=False)

    class Queue:
        async def receive(self):
            if not started.is_set():
                yield delivery

    class Worker:
        async def process_session(self, partition, trace):
            assert (partition, trace) == ("partition", "trace-worker")
            started.set()
            try:
                if state.block:
                    await asyncio.Event().wait()
                if state.error:
                    raise state.error
                return state.result
            finally:
                stopped.set()

    async def build_worker():
        state.builds += 1
        await asyncio.sleep(0)
        return Worker()

    class Maintenance:
        async def run_once(self):
            return SimpleNamespace(
                scanned=1, republished=0, expired=0, failed=0,
                quarantined=0, skipped=0, busy=False, timed_out=False)

    class Resources:
        async def aclose(self):
            state.closes += 1

    class Ingress:
        jobs = SimpleNamespace(
            gateway=SimpleNamespace(create=None),
            now=lambda: datetime(2026, 9, 18, tzinfo=timezone.utc))

        async def handle_http(self, method, headers, body):
            from teams_adapter.cloud.ingress import HttpResult
            return HttpResult(401)

    runtime = CloudRuntime(
        ingress=Ingress(), queue=Queue(), worker_factory=build_worker,
        maintenance=Maintenance(), closeables=[Resources()], parallelism=1,
        shutdown_seconds=0.02)
    return SimpleNamespace(
        runtime=runtime, state=state, delivery=delivery, started=started, stopped=stopped)


async def test_worker_is_constructed_once_under_concurrent_requests(runtime_case):
    runtime = runtime_case.runtime
    a, b = await asyncio.gather(runtime.get_worker(), runtime.get_worker())
    assert a is b and runtime_case.state.builds == 1
    await runtime.aclose()


@pytest.mark.parametrize("result", ["drained", "rescheduled", "busy"])
async def test_aca_explicitly_settles_only_after_complete_processing(runtime_case, result):
    case = runtime_case
    case.state.result = result
    await case.runtime.start()
    await asyncio.wait_for(case.delivery.finished.wait(), 2)
    await case.runtime.aclose()
    assert case.delivery.actions[0] == "enter"
    assert case.delivery.actions[-1] == "exit"
    if result == "busy":
        assert ("release", 5) in case.delivery.actions
        assert "ack" not in case.delivery.actions
    else:
        assert "ack" in case.delivery.actions
    assert case.state.closes == 1


async def test_shutdown_cancels_active_turn_and_awaits_cleanup_before_close(runtime_case):
    from teams_adapter.app import create_cloud_app
    case = runtime_case
    case.state.block = True
    app = create_cloud_app(case.runtime.ingress, case.runtime.get_worker, runtime=case.runtime)
    async with TestClient(TestServer(app)) as client:
        await asyncio.wait_for(case.started.wait(), 2)
        assert (await client.get("/healthz")).status == 200
        assert (await client.get("/readyz")).status == 200
        assert (await client.post("/api/messages", json={})).status == 401
    assert case.stopped.is_set()
    assert case.delivery.actions == ["enter", "exit"]
    assert case.state.closes == 1
    await case.runtime.aclose()
    assert case.state.closes == 1


async def test_consumer_failure_marks_app_unhealthy_and_stops_sibling_tasks(runtime_case, caplog):
    from teams_adapter.app import create_cloud_app
    case = runtime_case
    case.state.error = RuntimeError("private-worker-error")
    app = create_cloud_app(case.runtime.ingress, case.runtime.get_worker, runtime=case.runtime)
    async with TestClient(TestServer(app)) as client:
        await asyncio.wait_for(case.stopped.wait(), 2)
        for _ in range(20):
            if not case.runtime.healthy:
                break
            await asyncio.sleep(0.01)
        assert (await client.get("/healthz")).status == 503
        assert (await client.get("/readyz")).status == 503
    assert case.state.closes == 1
    assert "ack" not in case.delivery.actions
    assert "private-worker-error" not in caplog.text


async def test_repeated_start_never_duplicates_consumers_or_closes(runtime_case):
    case = runtime_case
    case.state.block = True
    await case.runtime.start()
    await case.runtime.start()
    await case.started.wait()
    await asyncio.gather(case.runtime.aclose(), case.runtime.aclose())
    assert case.state.builds == 1 and case.state.closes == 1


async def test_production_composition_reuses_real_worker_stores_and_components(monkeypatch):
    from advisor_agent.factory import AdvisorComponents, AzureAdvisor
    from advisor_agent.durable.worker import SessionWorker
    from advisor_agent.durable.maintenance import Maintenance
    from teams_adapter.cloud.runtime import build_runtime
    from test_executor import ModelBoundary

    owner = AzureAdvisor(
        ModelBoundary(), channel_name="teams", azure_clients=(), credential_owner=None)
    builds = []

    def components(channel_name, *, credentials):
        builds.append((channel_name, credentials))
        return AdvisorComponents(owner)

    monkeypatch.setattr("advisor_agent.factory.build_components", components)
    runtime = build_runtime(CONFIG)
    assert isinstance(runtime.maintenance, Maintenance)
    assert builds == []
    first, second = await asyncio.gather(runtime.get_worker(), runtime.get_worker())
    assert first is second and isinstance(first, SessionWorker)
    assert first.jobs is runtime.ingress.jobs
    assert first.outbox is runtime.dispatcher
    assert runtime.maintenance.history is first.history
    assert len(builds) == 1
    await runtime.aclose()
    assert owner._azure_closed


@pytest.mark.parametrize("changes", [
    {"CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED": "True"},
    {"CONNECTIONS__SERVICE_CONNECTION__SETTINGS__VALIDATE_ISSUER": "False"},
    {"ADVISOR_BLOB_SERVICE_URL": ""},
    {"ADVISOR_WORKER_PARALLELISM": "0"},
])
def test_invalid_production_configuration_fails_before_serving(changes):
    from teams_adapter.cloud.runtime import build_runtime
    with pytest.raises(ValueError):
        build_runtime({**CONFIG, **changes})


async def test_process_getters_cache_the_same_runtime_without_extra_clients(
        runtime_case, monkeypatch):
    from teams_adapter.cloud import runtime as module
    builds = []

    def build():
        builds.append(True)
        return runtime_case.runtime

    monkeypatch.setattr(module, "_runtime", None)
    monkeypatch.setattr(module, "build_runtime", build)
    assert module.get_ingress() is runtime_case.runtime.ingress
    assert module.get_maintenance() is runtime_case.runtime.maintenance
    assert module.get_runtime() is runtime_case.runtime
    a, b = await asyncio.gather(module.get_worker(), module.get_worker())
    assert a is b
    assert len(builds) == 1
    await runtime_case.runtime.aclose()


async def test_maintenance_has_its_own_timed_segment_and_safe_summary(runtime_case, caplog):
    from advisor_shared.telemetry import trace_scope

    with caplog.at_level("INFO"), trace_scope(new=True, trace_id="request-trace") as caller:
        result = await runtime_case.runtime.run_maintenance_once()
    assert result.scanned == 1
    summary = next(record.telemetry for record in caplog.records
                   if record.msg == "maintenance_summary")
    assert summary["scanned"] == 1
    assert summary["segment_id"] != caller.segment_id
    assert "cursors" not in summary
    assert caller.timings == []
    await runtime_case.runtime.aclose()


async def test_cleanup_failure_closes_remaining_clients_and_is_safe(runtime_case, caplog):
    class FailingResource:
        async def aclose(self):
            raise RuntimeError("private-client-close")
    runtime_case.runtime._closeables.append(FailingResource())
    with pytest.raises(RuntimeError, match="cloud_cleanup_failed"):
        await runtime_case.runtime.aclose()
    assert runtime_case.state.closes == 1
    assert not runtime_case.runtime.healthy
    assert "private-client-close" not in caplog.text


async def test_stop_consumer_does_not_hide_an_unsupervised_failure(runtime_case, caplog):
    async def fail():
        raise RuntimeError("private-consumer-error")
    await runtime_case.runtime.stop_consumer(asyncio.create_task(fail()))
    assert not runtime_case.runtime.healthy
    assert "cloud_background_failed" in caplog.text
    assert "private-consumer-error" not in caplog.text
    await runtime_case.runtime.aclose()


@pytest.mark.parametrize("cancel", [False, True])
async def test_heavy_initialization_keeps_ingress_responsive_and_owns_cancellation(
        monkeypatch, cancel):
    from advisor_agent.factory import AdvisorComponents, AzureAdvisor
    from teams_adapter.cloud.runtime import build_runtime
    from test_executor import ModelBoundary

    entered, release = threading.Event(), threading.Event()
    owner = AzureAdvisor(
        ModelBoundary(), channel_name="teams", azure_clients=(), credential_owner=None)

    def components(*args, **kwargs):
        entered.set()
        release.wait(1)
        return AdvisorComponents(owner)

    monkeypatch.setattr("advisor_agent.factory.build_components", components)
    runtime = build_runtime(CONFIG)
    task = asyncio.create_task(runtime.get_worker())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done(), "synchronous model setup blocked the HTTP event loop"
        if cancel:
            task.cancel()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert owner._azure_closed


        else:
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.aclose()
    assert owner._azure_closed


async def test_malformed_wake_is_quarantined_without_cancelling_an_active_turn(runtime_case, caplog):
    case = runtime_case
    good, bad = case.delivery, DeliveryBoundary()
    bad.body = b'{"private-malformed-wake": invalid'
    remaining = deque([good, bad])
    released = asyncio.Event()
    quarantine = {}

    class Queue:
        async def receive(self):
            if remaining:
                yield remaining.popleft()

    class Worker:
        async def process_session(self, *args):
            case.started.set()
            await released.wait()
            return "drained"

    async def build():
        case.state.builds += 1
        return Worker()

    async def create(name, body):
        quarantine[name] = json.loads(body)
        return True

    case.runtime.ingress.jobs.gateway.create = create
    case.runtime.queue = Queue()
    case.runtime.parallelism = 2
    case.runtime._worker_factory = build
    try:
        await case.runtime.start()
        await asyncio.wait_for(bad.finished.wait(), 3)
        assert case.started.is_set() and not good.finished.is_set()
        assert case.runtime.healthy
        assert case.state.builds == 1
        digest = hashlib.sha256(bad.body).hexdigest()
        metadata = quarantine[f"poison/wake-{digest}.json"]
        assert metadata["code"] == "invalid_wake"
        assert metadata["body_digest"] == digest
        assert "private-malformed-wake" not in json.dumps(metadata)
        assert "private-malformed-wake" not in caplog.text
        released.set()
        await asyncio.wait_for(good.finished.wait(), 3)
    finally:
        released.set()
        await case.runtime.aclose()
    assert good.actions == ["enter", "ack", "exit"]
    assert bad.actions == ["enter", "ack", "exit"]


@pytest.mark.parametrize("error", [
    ServiceRequestError("private-storage-error"),
    ServiceResponseError("private-storage-error"),
    TimeoutError("private-storage-error"),
    HttpResponseError(message="private-storage-error", response=SimpleNamespace(
        status_code=503, reason="unavailable", headers={})),
])
async def test_retryable_delivery_failure_is_released_without_failing_the_pool(
        runtime_case, error, caplog):
    case = runtime_case
    case.state.error = error
    try:
        await case.runtime.start()
        await asyncio.wait_for(case.delivery.finished.wait(), 3)
        assert case.runtime.healthy
        assert ("release", 5) in case.delivery.actions
        assert "ack" not in case.delivery.actions
        assert "queue_delivery_retry" in caplog.text
        assert "private-storage-error" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_visibility_loss_is_not_acknowledged_or_promoted_to_pool_failure(runtime_case, caplog):
    from advisor_agent.durable.queue import DeliveryLost
    case = runtime_case
    case.state.error = DeliveryLost("private-visibility-error")
    try:
        await case.runtime.start()
        await asyncio.wait_for(case.stopped.wait(), 3)
        await asyncio.sleep(0.05)
        assert case.runtime.healthy
        assert case.delivery.actions == ["enter", "exit"]
        assert "queue_delivery_retry" in caplog.text
        assert "private-visibility-error" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_failed_quarantine_never_acks_or_starts_a_worker(runtime_case, caplog):
    case = runtime_case
    case.delivery.body = b"private-bad-wake"
    received = False

    class Queue:
        async def receive(self):
            nonlocal received
            if not received:
                received = True
                yield case.delivery

    async def unavailable(*args):
        raise ServiceRequestError("private-quarantine-error")

    case.runtime.queue = Queue()
    case.runtime.ingress.jobs.gateway.create = unavailable
    try:
        await case.runtime.start()
        await asyncio.wait_for(case.delivery.finished.wait(), 3)
        assert case.runtime.healthy and case.state.builds == 0
        assert "ack" not in case.delivery.actions
        assert ("release", 5) in case.delivery.actions
        assert "private-bad-wake" not in caplog.text
        assert "private-quarantine-error" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_empty_queue_has_one_shared_two_second_poll_and_stops_promptly(runtime_case):
    case = runtime_case
    calls = []
    polled = asyncio.Event()

    class EmptyQueue:
        async def receive(self):
            calls.append(asyncio.get_running_loop().time())
            polled.set()
            if False:
                yield

    case.runtime.queue = EmptyQueue()
    case.runtime.parallelism = 4
    try:
        await case.runtime.start()
        await asyncio.wait_for(polled.wait(), 2)
        await asyncio.sleep(0.4)
        assert len(calls) == 1
        assert case.state.builds == 0
    finally:
        await asyncio.wait_for(case.runtime.aclose(), 1)


@pytest.mark.parametrize("interval", [0.25, 0, -1, float("nan"), float("inf")])
def test_production_poll_interval_cannot_enable_tight_idle_polling(interval):
    from teams_adapter.cloud.runtime import build_runtime
    with pytest.raises(ValueError):
        build_runtime({**CONFIG, "ADVISOR_QUEUE_POLL_SECONDS": str(interval)})


async def test_transient_poll_failure_obeys_backoff_and_recovers(runtime_case, caplog):
    case = runtime_case
    calls = []

    class Queue:
        async def receive(self):
            calls.append(asyncio.get_running_loop().time())
            if len(calls) == 1:
                raise ServiceRequestError("private-poll-error")
            if len(calls) == 2:
                yield case.delivery

    case.runtime.queue = Queue()
    try:
        await case.runtime.start()
        await asyncio.wait_for(case.delivery.finished.wait(), 5)
        assert case.runtime.healthy
        assert calls[1] - calls[0] >= 1.9
        assert "ack" in case.delivery.actions
        assert "queue_poll_retry" in caplog.text
        assert "private-poll-error" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_receipt_loss_during_release_never_acks_or_fails_other_consumers(runtime_case, caplog):
    from advisor_agent.durable.queue import DeliveryLost
    case = runtime_case
    case.state.error = TimeoutError("private-transient")

    async def lose_receipt(delay):
        case.delivery.actions.append(("release_attempt", delay))
        case.delivery.lost.set()
        raise DeliveryLost("private-receipt")

    case.delivery.release = lose_receipt
    try:
        result = await case.runtime.process_delivery(case.delivery)
        assert result == "unsettled"
        assert case.runtime.healthy
        assert "ack" not in case.delivery.actions
        assert "queue_release_deferred" in caplog.text
        assert "private-" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_native_host_logging_values_do_not_break_sdk_auth_configuration():
    from teams_adapter.cloud.runtime import build_runtime

    runtime = build_runtime({
        **CONFIG, "LOGGING__LOGLEVEL__MICROSOFT.HOSTING.LIFETIME": "None"})
    try:
        assert runtime.ingress.auth_config.CLIENT_ID == "offline-app"
        assert runtime.ingress.auth_config.VALIDATE_ISSUER
        assert not runtime.ingress.auth_config.ANONYMOUS_ALLOWED
    finally:
        await runtime.aclose()


async def test_quarantine_uses_public_record_and_store_clock(runtime_case):
    from advisor_agent.durable.jobs import QuarantineRecord
    case = runtime_case
    case.delivery.body = b"original-invalid-wake"
    written = []
    clock = datetime(2024, 1, 2, tzinfo=timezone.utc)
    case.runtime.ingress.jobs.now = lambda: clock

    async def create(name, body):
        written.append((name, body))
        return True

    case.runtime.ingress.jobs.gateway.create = create
    try:
        assert await case.runtime.process_delivery(case.delivery) == "quarantined"
        name, body = written[0]
        record = QuarantineRecord.model_validate_json(body)
        digest = hashlib.sha256(b"original-invalid-wake").hexdigest()
        assert name == f"poison/wake-{digest}.json"
        assert record.body_digest == digest and record.code == "invalid_wake"
        assert record.quarantined_at == clock
        assert record.job_id is None and record.sequence is None
        assert case.state.builds == 0
    finally:
        await case.runtime.aclose()


async def test_duplicate_quarantine_is_verified_before_ack_without_refreshing_timestamp(runtime_case):
    from advisor_agent.durable.jobs import QuarantineRecord
    case = runtime_case
    case.delivery.body = b"duplicate-invalid-wake"
    old_time = datetime(2024, 1, 2, tzinfo=timezone.utc)
    existing = QuarantineRecord(
        code="invalid_wake", body_digest=hashlib.sha256(case.delivery.body).hexdigest(),
        quarantined_at=old_time).model_dump_json().encode()
    operations = []
    original_ack = case.delivery.ack

    async def create(name, body):
        operations.append("create")
        return False

    async def read(name):
        operations.append("read")
        return SimpleNamespace(body=existing)

    async def ack():
        operations.append("ack")
        await original_ack()

    case.runtime.ingress.jobs.gateway.create = create
    case.runtime.ingress.jobs.gateway.read = read
    case.delivery.ack = ack
    try:
        assert await case.runtime.process_delivery(case.delivery) == "quarantined"
        assert operations == ["create", "read", "ack"]
        assert QuarantineRecord.model_validate_json(existing).quarantined_at == old_time
        assert case.state.builds == 0
    finally:
        await case.runtime.aclose()


@pytest.mark.parametrize("existing", [
    None,
    b"private-corrupt-record",
    b'{"version":1,"code":"invalid_wake","body_digest":"'
    + b"0" * 64 + b'","quarantined_at":"2024-01-02T00:00:00Z"}',
    ServiceRequestError("private-quarantine-read"),
])
async def test_unconfirmed_quarantine_is_not_acknowledged_or_treated_as_pool_failure(
        runtime_case, existing, caplog):
    case = runtime_case
    case.delivery.body = b"private-bad-wake"

    async def create(*args):
        return False

    async def read(*args):
        if isinstance(existing, Exception):
            raise existing
        return None if existing is None else SimpleNamespace(body=existing)

    case.runtime.ingress.jobs.gateway.create = create
    case.runtime.ingress.jobs.gateway.read = read
    try:
        assert await case.runtime.process_delivery(case.delivery) in {"released", "unsettled"}
        assert "ack" not in case.delivery.actions
        assert case.runtime.healthy and case.state.builds == 0
        assert "private-" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_permission_failure_during_release_retains_delivery_and_alerts(runtime_case, caplog):
    case = runtime_case
    case.state.error = TimeoutError("private-storage")

    async def forbidden(delay):
        raise HttpResponseError(
            message="private-permission", response=SimpleNamespace(
                status_code=403, reason="Forbidden", headers={}))

    case.delivery.release = forbidden
    try:
        assert await case.runtime.process_delivery(case.delivery) == "unsettled"
        assert "ack" not in case.delivery.actions and case.runtime.healthy
        record = next(r for r in caplog.records if r.msg == "queue_release_deferred")
        assert record.levelname == "ERROR"
        assert record.telemetry["http_status"] == 403
        assert record.telemetry["reason"] == "access_denied"
        assert "private-" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_permission_denial_defers_valid_work_without_quarantine(runtime_case, caplog):
    case = runtime_case
    case.state.error = HttpResponseError(
        message="private-permission", response=SimpleNamespace(
            status_code=401, reason="Unauthorized", headers={}))
    try:
        assert await case.runtime.process_delivery(case.delivery) == "released"
        assert ("release", 30) in case.delivery.actions
        assert "ack" not in case.delivery.actions
        record = next(r for r in caplog.records if r.msg == "queue_delivery_retry")
        assert record.levelname == "ERROR"
        assert record.telemetry["reason"] == "access_denied"
        assert "private-" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_downstream_value_error_is_not_misclassified_as_malformed_wake(runtime_case):
    case = runtime_case
    case.state.error = ValueError("downstream invariant failure")

    async def forbidden(*args):
        pytest.fail("valid work must not be quarantined for a downstream ValueError")

    case.runtime.ingress.jobs.gateway.create = forbidden
    try:
        with pytest.raises(RuntimeError, match="queue_consumer_failed"):
            await case.runtime.process_delivery(case.delivery)
        assert "ack" not in case.delivery.actions
    finally:
        await case.runtime.aclose()


async def test_poll_permission_failure_alerts_and_backs_off_without_dropping_work(
        runtime_case, caplog):
    case = runtime_case
    polled = asyncio.Event()
    calls = []

    class Queue:
        async def receive(self):
            calls.append(True)
            polled.set()
            raise HttpResponseError(
                message="private-queue-permission", response=SimpleNamespace(
                    status_code=403, reason="Forbidden", headers={}))
            yield  # pragma: no cover

    case.runtime.queue = Queue()
    try:
        await case.runtime.start()
        await asyncio.wait_for(polled.wait(), 2)
        await asyncio.sleep(0.05)
        assert case.runtime.healthy and case.state.builds == 0
        assert len(calls) == 1 and "ack" not in case.delivery.actions
        record = next(r for r in caplog.records if r.msg == "queue_poll_retry")
        assert record.levelname == "ERROR"
        assert record.telemetry["reason"] == "access_denied"
        assert "private-" not in caplog.text
    finally:
        await case.runtime.aclose()


async def test_release_cancellation_is_not_swallowed(runtime_case):
    case = runtime_case
    case.state.error = TimeoutError("storage transient")

    async def cancelled(delay):
        raise asyncio.CancelledError()

    case.delivery.release = cancelled
    try:
        with pytest.raises(asyncio.CancelledError):
            await case.runtime.process_delivery(case.delivery)
        assert "ack" not in case.delivery.actions
    finally:
        await case.runtime.aclose()

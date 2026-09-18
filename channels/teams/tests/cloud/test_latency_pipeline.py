"""Actual completed-stage evidence and bounded, joinable completion exports."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
import pytest

from advisor_agent.durable.worker import SessionWorker
from advisor_shared.cloud import PreparedTurn
from teams_adapter.cloud.observability import HostObservations, observe_job
from test_benchmark_target import (
    KEY, make_executor, observer, register, registration, submit, target,
)
from test_executor import payload_for


def completed(caplog, name):
    return [
        record.telemetry for record in caplog.records
        if record.message == "step_completed" and record.telemetry["name"] == name
    ]


def exported(caplog):
    return [record.telemetry for record in caplog.records
            if record.message == "benchmark_stage_completed"]


def test_queue_wait_uses_utc_acceptance_once_per_validated_job(caplog):
    caplog.set_level("INFO")
    clock = SimpleNamespace(utc=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    observations = HostObservations(
        environment={}, clock=lambda: 987654.0, utc_clock=lambda: clock.utc)
    first = payload_for().model_copy(update={
        "accepted_at": clock.utc - timedelta(seconds=2.5)})
    second = first.model_copy(update={
        "job_id": "other-job", "accepted_at": clock.utc - timedelta(seconds=1)})
    with observations.invocation("worker", host_kind="functions"):
        assert observations.snapshot(first).stages == []
        observe_job(first)
        snapshot = observations.snapshot(first)
        assert [(stage.name, stage.duration_ms) for stage in snapshot.stages] == [
            ("queue_wait", 2500.0)]
        clock.utc += timedelta(seconds=5)
        observe_job(first)
        assert observations.snapshot(first) == snapshot
        observe_job(second)
        assert [(stage.name, stage.duration_ms) for stage in observations.snapshot(second).stages] == [
            ("queue_wait", 6000.0)]
        assert observations.snapshot(first) == snapshot
    links = [record.telemetry for record in caplog.records
             if record.message == "host_job_trace_link"]
    assert len(links) == 2
    assert links[0]["trace_id"] == first.trace_id
    assert links[0]["host_trace_id"] != first.trace_id


@pytest.mark.parametrize(("accepted", "diagnostic"), [
    (datetime(2026, 9, 18, tzinfo=timezone.utc), "clock_skew"),
    (datetime(2026, 9, 16), "invalid_timestamp"),
    ("private-question-private-token", "invalid_timestamp"),
])
def test_invalid_queue_intervals_are_omitted_not_clamped(accepted, diagnostic, caplog):
    observations = HostObservations(
        environment={}, utc_clock=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc))
    payload = payload_for().model_copy(update={"accepted_at": accepted})
    with observations.invocation("worker", host_kind="aca"):
        observe_job(payload)
        observe_job(payload)
        assert not observations.snapshot(payload).stages
    diagnostics = [record.telemetry for record in caplog.records
                   if record.message == "queue_wait_omitted"]
    assert [item["reason"] for item in diagnostics] == [diagnostic]
    assert "private-question" not in caplog.text
    assert "private-token" not in caplog.text


def test_late_model_connection_setup_belongs_to_current_job_not_first_in_batch(caplog):
    from advisor_shared.telemetry import trace_scope
    from teams_adapter.cloud.observability import observe_host_stage

    caplog.set_level("INFO")
    clock = SimpleNamespace(now=10.0)
    observations = HostObservations(environment={}, clock=lambda: clock.now)
    first = payload_for()
    second = first.model_copy(update={"job_id": "second-job", "trace_id": "second-trace"})
    with observations.invocation("worker", host_kind="functions"):
        observe_job(first)
        before = observations.snapshot(first)
        with trace_scope(new=True, trace_id=second.trace_id):
            observe_job(second)
            with observe_host_stage("connection"):
                clock.now += 0.125
                assert not any(stage.name == "connection"
                               for stage in observations.snapshot(second).stages)
        assert observations.snapshot(first) == before
        assert [(stage.name, stage.duration_ms) for stage in observations.snapshot(second).stages
                if stage.name == "connection"] == [("connection", 125.0)]
    stage = next(record.telemetry for record in caplog.records
                 if record.message == "host_stage_completed")
    assert stage == {
        "trace_id": second.trace_id, "name": "connection",
        "duration_ms": 125.0, "outcome": "success",
    }


def test_completed_ingress_has_joinable_trace_not_an_early_partial_duration(caplog):
    caplog.set_level("INFO")
    clock = SimpleNamespace(now=30.0)
    observations = HostObservations(environment={}, clock=lambda: clock.now)
    with observations.invocation("http", host_kind="aca") as invocation:
        trace_id = observations.snapshot().trace_id
        clock.now += 0.25
        assert observations.snapshot().stages == []
        assert not [r for r in caplog.records if r.message == "host_invocation_completed"]
        invocation.http_status(202)
    final = next(r.telemetry for r in caplog.records if r.message == "host_invocation_completed")
    assert final["trace_id"] == trace_id
    assert final["name"] == "ingress"
    assert final["duration_ms"] == 250
    assert final["outcome"] == "success"


@pytest.mark.parametrize("visible", [True, False])
async def test_send_is_logged_only_after_real_http_completion_without_invented_wire_stage(
    caplog, visible,
):
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink

    caplog.set_level("INFO")
    entered, release = asyncio.Event(), asyncio.Event()
    received = []

    async def receive(request):
        assert request.headers["X-Benchmark-Key"] == KEY
        received.append(await request.json())
        entered.set()
        await release.wait()
        return web.json_response({"disposition": "completed"})

    app = web.Application()
    app.router.add_post("/bench/replies", receive)
    payload = payload_for().model_copy(update={
        "target": "benchmark", "run_id": "private-run", "request_id": "private-request"})
    prepared = PreparedTurn(
        reply={"type": "message", "text": "private-answer"} if visible else None,
        history=[], outcome="success" if visible else "ignored")
    async with TestServer(app) as server, ClientSession(trust_env=False) as session:
        sink = BenchmarkReplySink(
            session, str(server.make_url("/bench/replies")),
            auth_mode="key", key=KEY, allow_loopback=True)
        sending = asyncio.create_task(sink.send(payload, prepared))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not completed(caplog, "benchmark.send")
            assert not exported(caplog)
            assert not any(stage["name"] == "send"
                           for stage in received[0]["telemetry"]["stages"])
        finally:
            release.set()
            assert await sending == payload.job_id
        assert not session.closed
    send = completed(caplog, "benchmark.send")
    auth = completed(caplog, "benchmark.callback_auth")
    assert len(send) == len(auth) == 1
    assert send[0]["trace_id"] == auth[0]["trace_id"] == payload.trace_id
    assert send[0]["duration_ms"] >= 0 and send[0]["status"] == "success"
    assert exported(caplog) == [{
        "event": "benchmark_stage_completed", "version": 1,
        "trace_id": payload.trace_id, "job_id": payload.job_id,
        "boot_id": send[0]["boot_id"], "segment_id": send[0]["segment_id"],
        "span_id": send[0]["span_id"],
        "stage": {"name": "send", "duration_ms": send[0]["duration_ms"], "outcome": "success"},
    }]
    safe = json.dumps(send + auth + exported(caplog))
    assert all(private not in safe for private in (
        KEY, "private-answer", "private-run", "private-request", "127.0.0.1"))


async def test_real_worker_stages_link_ingress_history_and_callback_without_model(target, caplog):
    caplog.set_level("INFO")
    observations = HostObservations(environment={})
    target.ingress.telemetry_provider = observations.snapshot
    assert (await register(target, registration(max_requests=1))).status == 201
    with observations.invocation("http", host_kind="functions") as invocation:
        ack = await submit(target)
        invocation.http_status(ack.status)
        ingress_trace = observations.snapshot().trace_id
        assert not exported(caplog)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    async with observer() as (url, session, received):
        executor = make_executor(target, session, url, host_telemetry=observations.snapshot)
        worker = SessionWorker(target.jobs, target.history, target.dispatcher, executor)
        with observations.invocation("worker", host_kind="functions"):
            assert await worker.process_session(payload.partition_key, payload.trace_id) == "drained"
    final = next(reply for reply in received if reply["event"] == "final")
    names = [stage["name"] for stage in final["telemetry"]["stages"]]
    assert "queue_wait" in names and "auth" in names and "agent" in names
    assert "model" not in names and "send" not in names and "ingress" not in names
    assert final["telemetry"]["model_rounds"] is None
    assert "history_read" in names and "history_commit" in names
    for name in ("history.read", "history.commit", "benchmark.send"):
        stages = completed(caplog, name)
        assert len(stages) == 1
        assert stages[0]["trace_id"] == payload.trace_id
        assert stages[0]["status"] == "success" and stages[0]["duration_ms"] >= 0
    links = [record.telemetry for record in caplog.records
             if record.message == "benchmark_trace_link"]
    assert links and all(link == {
        "trace_id": payload.trace_id, "ingress_trace_id": ingress_trace,
    } for link in links)
    from teams_adapter.cloud.benchmark_observations import StageCompletion
    exports = [StageCompletion.model_validate(item) for item in exported(caplog)]
    assert [item.stage.name for item in exports] == [
        "ingress", "history_read", "history_commit", "send"]
    assert all(item.trace_id == payload.trace_id and item.job_id == payload.job_id for item in exports)
    assert len({(item.boot_id, item.segment_id, item.span_id) for item in exports}) == 4
    assert all(item.stage.outcome == "success" for item in exports)
    for name in ("history_read", "history_commit"):
        stage = next(item["stage"] for item in exported(caplog) if item["stage"]["name"] == name)
        assert next(item for item in final["telemetry"]["stages"] if item["name"] == name) == stage
    assert not any(item.stage.name == "history_setup" for item in exports)
    from advisor_shared.logging import TelemetryFormatter
    exported_fields = {
        "event", "version", "trace_id", "job_id", "boot_id", "segment_id", "span_id", "stage"}
    for record in caplog.records:
        if record.message != "benchmark_stage_completed":
            continue
        line = json.loads(TelemetryFormatter().format(record))
        assert set(line) == exported_fields | {"timestamp", "level", "logger", "message"}
        assert StageCompletion(**{key: line[key] for key in exported_fields}).job_id == payload.job_id


@pytest.mark.parametrize(("status", "definite", "retryable"), [
    (429, True, True), (500, False, False),
])
async def test_completed_http_failure_keeps_delivery_uncertainty(
    caplog, status, definite, retryable,
):
    from advisor_shared.delivery import DeliveryFailure
    from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink

    caplog.set_level("INFO")
    payload = payload_for().model_copy(update={
        "target": "benchmark", "run_id": "run", "request_id": "request"})
    prepared = PreparedTurn(reply=None, history=[], outcome="ignored")
    async with observer(status=status) as (url, session, received):
        sink = BenchmarkReplySink(
            session, url, auth_mode="key", key=KEY, allow_loopback=True)
        with pytest.raises(DeliveryFailure) as raised:
            await sink.send(payload, prepared)
    assert raised.value.definitely_not_sent is definite
    assert raised.value.retryable is retryable
    stages = completed(caplog, "benchmark.send")
    assert len(stages) == 1 and stages[0]["status"] == "error"
    assert stages[0]["trace_id"] == payload.trace_id
    assert stages[0]["duration_ms"] >= 0
    assert received[0]["telemetry"]["stages"] == []
    assert exported(caplog)[0]["stage"]["outcome"] == "failed"


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 1.0},
    {"body": "private-content"}, {"job_id": "private/"}, {"trace_id": "x" * 129},
    {"stage": {"name": "send", "duration_ms": float("nan"), "outcome": "success"}},
    {"stage": {"name": "send", "duration_ms": 3, "outcome": "success", "url": "private-url"}},
    {"segment_id": None}, {"boot_id": None}, {"span_id": None},
])
def test_completion_export_is_strict_bounded_and_has_no_payload_fields(changes):
    from pydantic import ValidationError
    from teams_adapter.cloud.benchmark_observations import StageCompletion

    valid = {
        "event": "benchmark_stage_completed", "version": 1,
        "trace_id": "trace", "job_id": "job", "boot_id": "boot",
        "segment_id": "segment", "span_id": "span",
        "stage": {"name": "send", "duration_ms": 3, "outcome": "success"},
    }
    assert StageCompletion.model_validate(valid).model_dump() == valid
    with pytest.raises(ValidationError) as raised:
        StageCompletion.model_validate(valid | changes)
    assert "private-" not in str(raised.value)


def test_completion_export_requires_explicit_schema_discriminator():
    from pydantic import ValidationError
    from teams_adapter.cloud.benchmark_observations import StageCompletion

    values = {
        "trace_id": "trace", "job_id": "job", "boot_id": "boot",
        "segment_id": "segment", "span_id": "span",
        "stage": {"name": "history_commit", "duration_ms": 1, "outcome": "success"},
    }
    for missing in (values, values | {"version": 1},
                    values | {"event": "benchmark_stage_completed"}):
        with pytest.raises(ValidationError):
            StageCompletion(**missing)


def test_history_callback_observations_are_isolated_by_trace_and_job():
    from advisor_shared.telemetry import step, trace_scope
    from teams_adapter.cloud.benchmark_observations import completed_history_telemetry

    payload = payload_for()
    with trace_scope(new=True, trace_id=payload.trace_id):
        with step("history.read", job_id="another-job"):
            pass
        assert completed_history_telemetry(payload, "history_read").stages == []
        with step("history.read", job_id=payload.job_id):
            pass
        stages = completed_history_telemetry(payload, "history_read").stages
        assert len(stages) == 1 and stages[0].name == "history_read"
        assert completed_history_telemetry(payload, "history_commit").stages == []
    with trace_scope(new=True, trace_id="another-trace"):
        with step("history.read", job_id=payload.job_id):
            pass
        assert completed_history_telemetry(payload, "history_read").stages == []


async def test_failed_commit_export_and_recovery_do_not_regenerate_or_rewrite_checkpoint(
    target, caplog, monkeypatch,
):
    caplog.set_level("INFO")
    assert (await register(target, registration(max_requests=1))).status == 201
    ack = await submit(target)
    payload = await target.jobs.load_payload(ack.body["job_id"])
    write = target.gateway.write
    fail = True

    async def storage_outage(name, *args, **kwargs):
        nonlocal fail
        if name.startswith("context/") and fail:
            fail = False
            raise ConnectionError("private-storage-error")
        return await write(name, *args, **kwargs)

    monkeypatch.setattr(target.gateway, "write", storage_outage)
    async with observer() as (url, session, received):
        executor = make_executor(target, session, url)
        send = executor.sink.send

        async def check_checkpoint_before_receipt(*args, **kwargs):
            stored = await target.jobs.read(payload.job_id)
            assert stored.result.model_dump_json() == checkpoint
            return await send(*args, **kwargs)

        monkeypatch.setattr(executor.sink, "send", check_checkpoint_before_receipt)
        worker = SessionWorker(
            target.jobs, target.history, target.dispatcher, executor)
        with pytest.raises(ConnectionError):
            await worker.process_session(payload.partition_key, payload.trace_id)
        assert not any(item["event"] == "final" for item in received)
        saved = await target.jobs.read(payload.job_id)
        async with target.jobs.leased_job(payload.job_id) as job:
            checkpoint = job.result.model_dump_json()
            assert "history_read" in checkpoint and "history_commit" not in checkpoint
        assert await worker.process_session(payload.partition_key, payload.trace_id) == "drained"
        completed_job = await target.jobs.read(payload.job_id)
        assert completed_job.result_written_at == saved.result_written_at
        assert completed_job.generation_attempts == 1
        assert completed_job.result is None
    assert [item["stage"]["outcome"] for item in exported(caplog)
            if item["stage"]["name"] == "history_commit"] == ["failed", "success"]
    assert [item["stage"]["name"] for item in exported(caplog)].count("history_read") == 1
    assert all(item["job_id"] == payload.job_id and item["trace_id"] == payload.trace_id
               for item in exported(caplog))
    assert "private-storage-error" not in json.dumps(exported(caplog))
    final = next(item for item in received if item["event"] == "final")
    assert [item["outcome"] for item in final["telemetry"]["stages"]
            if item["name"] == "history_commit"] == ["success"]


async def test_failed_publish_emits_failed_complete_ingress_without_request_content(
    target, caplog, monkeypatch,
):
    caplog.set_level("INFO")
    observations = HostObservations(environment={})
    assert (await register(target)).status == 201

    async def unavailable(*args):
        raise ConnectionError("private-queue-token")

    monkeypatch.setattr(target.dispatcher, "notify", unavailable)
    with observations.invocation("http", host_kind="functions") as invocation:
        response = await submit(target)
        assert response.status == 503
        invocation.http_status(response.status)
        assert not exported(caplog)
    assert len(exported(caplog)) == 1
    event = exported(caplog)[0]
    payload = await target.jobs.load_payload(event["job_id"])
    assert event["trace_id"] == payload.trace_id
    assert event["stage"]["name"] == "ingress"
    assert event["stage"]["outcome"] == "failed"
    assert event["stage"]["duration_ms"] >= 0
    assert "private-queue-token" not in json.dumps(event)

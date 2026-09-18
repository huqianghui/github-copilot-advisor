from dataclasses import asdict
import asyncio
import json
import os
import time
from uuid import uuid4

import pytest

from advisor_deploy.bench.models import Sample


def test_reports_separate_outcomes_actual_window_and_missing_cold_evidence(tmp_path):
    from advisor_deploy.bench.report import render_report, write_result
    from advisor_deploy.bench.runner import RunResult

    result = RunResult(
        run_id="test-run", profile="local-smoke", concurrency=1,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:10Z",
        window_seconds=10., submission_seconds=8., stop_reason="request_deadline",
        samples=[
            Sample("a", "kb-01", "success", 0., 2.),
            Sample("b", "kb-01", "degraded", 2., 3.),
            Sample("c", "kb-01", "failed", 3., 4.),
            Sample("d", "kb-01", "unknown", 4., 5.),
            Sample("e", "kb-01", "censored", 5., None),
        ], plan={"target_completions": 100, "min_seconds": 180.},
    )
    report = render_report(result)
    assert "| success | 1 |" in report
    assert "| degraded | 1 |" in report
    assert "| failed | 1 |" in report
    assert "| unknown | 1 |" in report
    assert "| censored | 1 |" in report
    assert "0.2" in report
    assert "10.000" in report
    assert "insufficient" in report
    assert "unverified" in report
    assert "not an SLA" in report
    path = tmp_path / "result.json"
    write_result(result, path)
    assert len(json.loads(path.read_text())["samples"]) == 5


def test_invalid_raw_results_cannot_be_reported_as_measurements():
    from advisor_deploy.bench.report import read_result

    for value in ({"question": "secret"}, {"samples": [], "window_seconds": float("nan")}):
        import pytest
        with pytest.raises(ValueError):
            read_result(value)


def test_historical_manifest_expiry_does_not_prevent_reporting_old_measurements():
    from advisor_deploy.bench.plan import RunPlan
    from advisor_deploy.bench.report import read_result, render_report
    from advisor_deploy.bench.runner import RunResult
    from test_bench_runner import cloud_plan_values

    plan = RunPlan.model_validate(cloud_plan_values()).model_dump()
    plan["deployment"]["valid_until"] = "2020-01-01T00:00:00Z"
    result = RunResult(
        run_id="old-run", profile="concurrency", concurrency=1,
        started_at="2019-01-01T00:00:00Z", ended_at="2019-01-01T00:00:01Z",
        window_seconds=1., submission_seconds=1., stop_reason="request_budget",
        samples=[Sample("a", "kb-01", "success", 0., .5)], plan=plan)
    assert "| success | 1 |" in render_report(read_result(asdict(result)))


@pytest.fixture
async def azurite_results():
    if os.environ.get("ADVISOR_BENCH_AZURITE") != "1":
        pytest.skip("set ADVISOR_BENCH_AZURITE=1 for real loopback Azurite evidence")
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob.aio import ContainerClient

    # Published Azurite development-only credential; never used for Azure.
    key = ("Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsu"
           "Fq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==")
    async with ContainerClient(
        "http://127.0.0.1:11000/devstoreaccount1", "bench-contract-" + uuid4().hex,
        credential=key, retry_total=0, api_version="2023-11-03",
    ) as container:
        await container.create_container()
        try:
            yield container
        finally:
            try:
                await container.delete_container()
            except ResourceNotFoundError:
                pass


async def test_bounded_evidence_queue_rejects_payloads_and_overflow(tmp_path):
    from advisor_deploy.bench.sink import EvidenceEvent, EvidenceFailure, EvidenceSink, FileEvidenceStore
    from test_bench_runner import local_plan

    async with EvidenceSink(FileEvidenceStore(tmp_path), max_pending=1) as sink:
        await sink.start_run("run-1", local_plan())
        sink.emit(EvidenceEvent(run_id="run-1", kind="request_intent",
                                request_id="request-1", case_id="kb-01"))
        with pytest.raises(EvidenceFailure):
            sink.emit(EvidenceEvent(run_id="run-1", kind="request_intent",
                                    request_id="request-2", case_id="kb-01"))
        assert sink.failed
        private = "not-for-evidence"
        with pytest.raises(ValueError) as error:
            EvidenceEvent(run_id="run-1", kind="final", reply=private)
        assert private not in str(error.value)


async def test_actual_http_answers_are_durable_in_real_azurite_without_bodies(azurite_results):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import BlobEvidenceStore, EvidenceSink
    from test_bench_runner import ack, harness

    emitted_stages = []

    async def handler(body, send, tasks):
        stages = []
        for name in ("history_setup", "synthetic"):
            started = time.perf_counter()
            await asyncio.sleep(0)
            stages.append({"name": name, "duration_ms": (time.perf_counter() - started) * 1000,
                           "outcome": "success"})
        emitted_stages.append(stages)
        await send(body, telemetry={"stages": stages})
        return ack(body)

    async with EvidenceSink(BlobEvidenceStore(azurite_results)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, _, _ = values
            result = await run_level(plan, submit, receiver, concurrency=1, evidence_sink=sink)
            assert result.stop_reason == "target_reached"
        final = json.loads(await (await azurite_results.download_blob(
            f"{result.run_id}/final.json")).readall())
        assert final["samples"][0]["status"] == "success"
        assert final["telemetry"][0]["stages"] == emitted_stages[0]
        checkpoint = json.loads(await (await azurite_results.download_blob(
            f"{result.run_id}/checkpoint.json")).readall())
        assert checkpoint["state"] == "finished"
        assert checkpoint["pending"] == {}
        names = [blob.name async for blob in azurite_results.list_blobs()]
        assert any("/events/" in name for name in names)
        for name in names:
            raw = (await (await azurite_results.download_blob(name)).readall()).decode()
            assert "Complete rendered local test reply." not in raw
            assert '"message"' not in raw
            assert '"reply"' not in raw
            assert '"question"' not in raw
            assert "Bearer " not in raw


async def test_interruption_preserves_censored_final_and_checkpoint_in_azurite(azurite_results):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import BlobEvidenceStore, EvidenceSink
    from test_bench_runner import ack, harness

    admitted = asyncio.Event()

    async def handler(body, send, tasks):
        admitted.set()
        return ack(body)

    async with EvidenceSink(BlobEvidenceStore(azurite_results)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, _, _ = values
            task = asyncio.create_task(run_level(plan, submit, receiver, concurrency=1,
                                                 evidence_sink=sink))
            await admitted.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            run_id = next(iter(receiver.origins))
        final = json.loads(await (await azurite_results.download_blob(
            f"{run_id}/final.json")).readall())
        assert final["stop_reason"] == "driver_interrupted"
        assert final["samples"][0]["status"] == "censored"
        assert final["samples"][0]["finished_s"] is None
        checkpoint = json.loads(await (await azurite_results.download_blob(
            f"{run_id}/checkpoint.json")).readall())
        assert checkpoint["state"] == "interrupted"


async def test_storage_loss_stops_submissions_and_preserves_local_failure_result(
    azurite_results, tmp_path,
):
    from advisor_deploy.bench.report import write_result
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import BlobEvidenceStore, EvidenceSink
    from test_bench_runner import ack, harness

    async def handler(body, send, tasks):
        await azurite_results.delete_container()
        return ack(body)

    async with EvidenceSink(BlobEvidenceStore(azurite_results)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, requests, _ = values
            result = await run_level(
                plan, submit, receiver, concurrency=1, evidence_sink=sink,
                result_sink=lambda result: write_result(result, tmp_path / "fallback.json"))
            assert result.stop_reason == "observer_evidence_failure"
            assert len(requests) == 1
            assert result.samples[0].status == "censored"
            assert json.loads((tmp_path / "fallback.json").read_text())["stop_reason"] == (
                "observer_evidence_failure")


async def test_missing_azurite_container_is_not_automatically_recreated(azurite_results):
    from azure.core.exceptions import ResourceNotFoundError
    from advisor_deploy.bench.sink import BlobEvidenceStore, EvidenceFailure, EvidenceSink

    await azurite_results.delete_container()
    with pytest.raises(EvidenceFailure):
        async with EvidenceSink(BlobEvidenceStore(azurite_results)):
            raise AssertionError("missing pre-provisioned container must fail closed")
    with pytest.raises(ResourceNotFoundError):
        await azurite_results.get_container_properties()


async def test_cancel_during_zero_wait_preserves_an_interrupted_preparation_checkpoint(tmp_path):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import EvidenceSink, FileEvidenceStore
    from test_bench_runner import ack, harness

    waiting = asyncio.Event()

    async def handler(body, send, tasks):
        raise AssertionError("cold wait must not submit a query")

    async def before_load(since):
        waiting.set()
        await asyncio.Event().wait()

    async with EvidenceSink(FileEvidenceStore(tmp_path)) as sink:
        async with harness(handler, profile="cold") as values:
            plan, submit, receiver, requests, registrations = values
            task = asyncio.create_task(run_level(plan, submit, receiver, concurrency=1,
                                                 before_load=before_load, evidence_sink=sink))
            await waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            run_id = registrations[0]["run_id"]
            assert requests == []
        checkpoint = json.loads((tmp_path / run_id / "checkpoint.json").read_text())
        assert checkpoint["state"] == "interrupted"
        final = json.loads((tmp_path / run_id / "final.json").read_text())
        assert final["samples"] == []


@pytest.mark.parametrize("field", ["driver", "observations", "cold"])
def test_result_metadata_never_accepts_untyped_payload_fields(field, tmp_path):
    from advisor_deploy.bench.report import write_result
    from advisor_deploy.bench.runner import RunResult

    result = RunResult(
        run_id="test-run", profile="local-smoke", concurrency=1,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:01Z",
        window_seconds=1., submission_seconds=1., stop_reason="target_reached",
        samples=[Sample("a", "kb-01", "success", 0., .5)],
        plan={"target_completions": 1, "min_seconds": 0.})
    setattr(result, field, {"reply": "private-rendered-body"})
    path = tmp_path / "result.json"
    with pytest.raises(ValueError) as error:
        write_result(result, path)
    assert "private-rendered-body" not in str(error.value)
    assert not path.exists()


async def test_finalization_checkpoint_failure_cannot_publish_a_clean_final(tmp_path):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import EvidenceSink, FileEvidenceStore
    from test_bench_runner import ack, harness

    class FailingFinalCheckpoint(FileEvidenceStore):
        async def write(self, name, data, *, overwrite):
            if name.endswith("/checkpoint.json") and json.loads(data)["state"] == "finished":
                raise OSError("injected finalization checkpoint failure")
            await super().write(name, data, overwrite=overwrite)

    async def handler(body, send, tasks):
        await send(body)
        return ack(body)

    async with EvidenceSink(FailingFinalCheckpoint(tmp_path)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, _, _ = values
            result = await run_level(plan, submit, receiver, concurrency=1, evidence_sink=sink)
        assert result.stop_reason == "observer_evidence_failure"
        final = json.loads((tmp_path / result.run_id / "final.json").read_text())
        assert final["stop_reason"] == "observer_evidence_failure"
        assert final["samples"][0]["status"] == "success"
        assert list((tmp_path / result.run_id / "events").glob("*.json"))


async def test_final_result_is_the_last_successful_durable_commit(tmp_path):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import EvidenceSink, FileEvidenceStore
    from test_bench_runner import ack, harness

    committed = []

    class RecordingStore(FileEvidenceStore):
        async def write(self, name, data, *, overwrite):
            await super().write(name, data, overwrite=overwrite)
            committed.append(name)

    async def handler(body, send, tasks):
        await send(body)
        return ack(body)

    async with EvidenceSink(RecordingStore(tmp_path)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, _, _ = values
            result = await run_level(plan, submit, receiver, concurrency=1, evidence_sink=sink)
    assert committed[-2:] == [f"{result.run_id}/checkpoint.json", f"{result.run_id}/final.json"]
    assert json.loads((tmp_path / result.run_id / "final.json").read_text())["stop_reason"] == (
        "target_reached")

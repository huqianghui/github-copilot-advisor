import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json

from aiohttp import ClientSession, web
import pytest

from test_bench_receiver import running_server


def local_plan(**changes):
    from advisor_deploy.bench.plan import RunPlan

    return RunPlan.model_validate(dict(
        target_url="http://127.0.0.1:1", callback_url="http://127.0.0.1:2/bench/replies",
        expected_target_id="local-synthetic", local_test=True, profile="local-smoke",
        scenario="synthetic", history_mode="empty", case_ids=["kb-01", "web-01"],
        seed=17, dataset_sha256="a" * 64, concurrency_levels=[1],
        target_completions=2, min_seconds=.01, max_submit_seconds=5.,
        request_timeout_seconds=3., drain_seconds=3., trend_seconds=1.,
        http_timeout_seconds=5., max_requests=20,
    ) | changes)


@asynccontextmanager
async def harness(handler, **changes):
    from advisor_deploy.bench.receiver import ReplyReceiver
    from advisor_deploy.bench.runner import HttpSubmitter

    receiver = ReplyReceiver(local_test=True)
    registrations = []
    requests = []
    tasks = []
    async with ClientSession() as client:
        async with running_server(receiver.application()) as callback:
            callback_url = callback + "/bench/replies"

            async def send_reply(body, *, outcome="success", **extra):
                payload = dict(run_id=body["run_id"], request_id=body["request_id"],
                               job_id="job-" + body["request_id"],
                               event="final", outcome=outcome) | extra
                if outcome in {"success", "degraded"}:
                    payload["reply"] = {"type": "message", "format": "markdown",
                                          "text": "Complete rendered local test reply."}
                async with client.post(callback_url, json=payload) as response:
                    assert response.status == 200

            async def register(request):
                body = await request.json()
                registrations.append(body)
                return web.json_response(dict(
                    run_id=body["run_id"], target_id="local-synthetic",
                    dataset_sha256=body["dataset_sha256"]), status=201)

            async def message(request):
                body = await request.json()
                requests.append(body)
                return await handler(body, send_reply, tasks)

            app = web.Application()
            app.router.add_post("/bench/runs", register)
            app.router.add_post("/bench/messages", message)
            async with running_server(app) as target:
                plan = local_plan(target_url=target, callback_url=callback_url, **changes)
                submitter = HttpSubmitter(client, plan)
                try:
                    yield plan, submitter, receiver, requests, registrations
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)


def ack(body):
    return web.json_response(dict(run_id=body["run_id"], request_id=body["request_id"],
                                  job_id="job-" + body["request_id"]), status=202)


async def test_ack_never_releases_slot_and_every_logical_question_has_new_id():
    from advisor_deploy.bench.runner import run_level

    first_received = asyncio.Event()
    complete = asyncio.Event()

    async def handler(body, send, tasks):
        first_received.set()

        async def later():
            await complete.wait()
            await send(body)
        tasks.append(asyncio.create_task(later()))
        return ack(body)

    async with harness(handler) as (plan, submitter, receiver, requests, registrations):
        task = asyncio.create_task(run_level(plan, submitter, receiver, concurrency=1))
        await first_received.wait()
        async with asyncio.timeout(3):
            while next(iter(receiver.entries.values())).ack_ms is None:
                await asyncio.sleep(.01)
        await asyncio.sleep(.02)
        assert len(requests) == 1
        complete.set()
        result = await task
        assert len(requests) == 2
        assert len({r["request_id"] for r in requests}) == 2
        assert len(registrations) == 1
        assert all(s.status == "success" for s in result.samples)
        assert result.samples[0].duration_ms >= result.samples[0].ack_ms


async def test_reply_before_ack_and_retry_preserves_logical_request_identity():
    from advisor_deploy.bench.runner import run_level

    attempts = {}

    async def handler(body, send, tasks):
        count = attempts.get(body["request_id"], 0) + 1
        attempts[body["request_id"]] = count
        if count == 1:
            return web.Response(status=503)
        await send(body)
        return ack(body)

    async with harness(handler, target_completions=1, min_seconds=0.) as values:
        plan, submitter, receiver, requests, _ = values
        result = await run_level(plan, submitter, receiver, concurrency=1)
        assert len(result.samples) == 1
        assert result.samples[0].status == "success"
        assert len(requests) == 2
        assert requests[0] == requests[1]


@pytest.mark.parametrize("status,expected", [(403, "failed"), (503, "censored"), (202, "censored")])
async def test_known_rejection_is_not_ambiguous_or_a_deadline_failure(status, expected):
    from advisor_deploy.bench.runner import run_level

    async def handler(body, send, tasks):
        return ack(body) if status == 202 else web.Response(status=status)

    async with harness(handler, max_requests=1, request_timeout_seconds=.5) as values:
        plan, submitter, receiver, requests, _ = values
        result = await run_level(plan, submitter, receiver, concurrency=1)
        assert len(result.samples) == 1
        assert result.samples[0].status == expected
        assert (result.samples[0].finished_s is None) == (expected == "censored")


async def test_overdue_request_stops_new_submissions_in_all_slots_and_cleans_tasks():
    from advisor_deploy.bench.runner import run_level

    async def handler(body, send, tasks):
        if body["virtual_user"] == 1:
            await asyncio.sleep(.2)
            await send(body)
        return ack(body)

    async with harness(handler, request_timeout_seconds=1.5,
                       max_submit_seconds=5., target_completions=100) as values:
        plan, submitter, receiver, requests, _ = values
        result = await run_level(plan, submitter, receiver, concurrency=2)
        assert result.stop_reason == "request_deadline"
        assert sum(s.status == "censored" for s in result.samples) == 1
        assert len(requests) < 12
        assert all(entry.future.done() for entry in receiver.entries.values())


async def test_retry_after_is_respected_and_persistent_ingress_throttle_stops_level():
    from advisor_deploy.bench.runner import run_level

    times = []

    async def handler(body, send, tasks):
        times.append(asyncio.get_running_loop().time())
        return web.Response(status=429, headers={"Retry-After": "1"})

    async with harness(handler, request_timeout_seconds=.3, max_submit_seconds=.4) as values:
        plan, submitter, receiver, requests, _ = values
        result = await run_level(plan, submitter, receiver, concurrency=1)
        assert len(requests) == 1
        assert result.samples[0].status == "failed"
        assert result.stop_reason in {"request_deadline", "retry_after_exceeds_deadline"}


async def test_same_user_and_seeded_history_are_registered_as_distinct_profiles():
    from advisor_deploy.bench.runner import run_level

    async def handler(body, send, tasks):
        await send(body)
        return ack(body)

    runs = []
    for mode in ("empty", "seeded20", "same_user"):
        async with harness(handler, history_mode=mode, min_seconds=0.,
                           target_completions=2) as values:
            plan, submitter, receiver, requests, registrations = values
            result = await run_level(plan, submitter, receiver, concurrency=2)
            runs.append(result.run_id)
            assert registrations[0]["history_mode"] == mode
            assert {r["virtual_user"] for r in requests} == (
                {0} if mode == "same_user" else {0, 1})
    assert len(set(runs)) == 3


def test_local_shortcuts_cannot_be_used_against_cloud_and_cloud_needs_manifest():
    from advisor_deploy.bench.plan import RunPlan

    values = local_plan().model_dump()
    for changes in (
        {"target_url": "https://cloud.example"},
        {"callback_url": "https://cloud.example/bench/replies"},
        {"local_test": False},
        {"max_submit_seconds": float("nan")},
        {"concurrency_levels": [3]},
    ):
        with pytest.raises(ValueError):
            RunPlan.model_validate(values | changes)


def test_trend_rules_require_actual_two_window_throttles_and_skip_ramp_backlog():
    from advisor_deploy.bench.runner import TrendGuard

    guard = TrendGuard()
    assert guard.close_window({"model": (100, 5)}, 20) is None
    assert guard.close_window({"model": (100, 0)}, 1) is None
    assert guard.close_window({"model": (100, 5)}, 2) is None
    assert guard.close_window({"model": (100, 5)}, 3) == "persistent_throttle"
    guard = TrendGuard()
    assert guard.close_window({}, 20) is None
    assert guard.close_window({}, 1) is None
    assert guard.close_window({}, 2) is None
    assert guard.close_window({}, 3) == "persistent_backlog"


async def test_host_crash_stops_future_submissions_and_elevation():
    from advisor_deploy.bench.runner import run_level

    async def handler(body, send, tasks):
        await send(body, outcome="failed", telemetry={"host_oom": True})
        return ack(body)

    async with harness(handler) as values:
        plan, submitter, receiver, requests, _ = values
        result = await run_level(plan, submitter, receiver, concurrency=1)
        assert result.stop_reason == "host_failure"
        assert len(requests) == 1


def test_zero_collector_never_calls_target_or_infers_zero_from_elapsed_time(tmp_path):
    from advisor_deploy.bench.runner import supplied_zero_evidence

    path = tmp_path / "zero.json"
    path.write_text("[]")
    assert supplied_zero_evidence(path, target_id="target", since="2026-01-01T00:00:00Z") == []
    path.write_text(json.dumps([dict(
        target_id="target", group="http", observed_at="2026-01-02T00:00:00Z",
        replicas=0, source="azure_monitor", evidence_id="metric-1",
    )]))
    proof = supplied_zero_evidence(path, target_id="target", since="2026-01-01T00:00:00Z")
    assert len(proof) == 1
    assert supplied_zero_evidence(path, target_id="other", since="2026-01-01T00:00:00Z") == []


async def test_cancellation_censors_and_persists_partial_result_without_leaked_submits():
    from advisor_deploy.bench.runner import run_level

    admitted = asyncio.Event()
    saved = []

    async def handler(body, send, tasks):
        admitted.set()
        return ack(body)

    async with harness(handler) as values:
        plan, submitter, receiver, requests, _ = values
        task = asyncio.create_task(run_level(
            plan, submitter, receiver, concurrency=1, result_sink=saved.append))
        await admitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert saved[0].stop_reason == "driver_interrupted"
        assert saved[0].samples[0].status == "censored"
        assert all(entry.future.done() for entry in receiver.entries.values())


async def test_local_example_executes_real_http_and_writes_only_safe_results(tmp_path):
    from advisor_deploy.bench.__main__ import execute
    from advisor_deploy.bench.plan import load_plan
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "bench" / "run.example.json"
    plan = load_plan(path)
    results = await execute(plan, plan_directory=path.parent, output=tmp_path)
    assert results
    assert all(sample.status == "success" for result in results for sample in result.samples)
    assert all(result.observations["http_attempts"] > 0 for result in results)
    for result in results:
        for telemetry in result.telemetry:
            assert [stage["name"] for stage in telemetry["stages"]] == ["synthetic"]
            assert 0 < telemetry["stages"][0]["duration_ms"] <= result.window_seconds * 1000
            assert telemetry["dependencies"] == []
            assert telemetry["model_rounds"] is None
    files = list(tmp_path.glob("*.json"))
    assert len(files) == len(results)
    raw = files[0].read_text()
    for forbidden in ("Authorization", "Bearer", "question", "markdown", "content"):
        assert f'"{forbidden}"' not in raw


def test_dataset_mismatch_rejected_before_any_traffic(tmp_path):
    from advisor_deploy.bench.plan import load_cases
    from pathlib import Path

    path = tmp_path / "cases.json"
    path.write_bytes((Path(__file__).resolve().parents[2] / "bench" / "cases.json").read_bytes())
    with pytest.raises(ValueError, match="digest"):
        load_cases(path, local_plan())


def test_shared_dataset_loader_rejects_duplicate_keys_and_oversized_input(tmp_path):
    from advisor_shared.benchmark import load_dataset

    path = tmp_path / "cases.json"
    path.write_text('{"version":1,"version":1,"cases":[],"seeded_history":[]}')
    with pytest.raises(ValueError):
        load_dataset(path)
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        load_dataset(path)


def test_runner_rejects_duplicate_json_even_when_decoded_digest_matches(tmp_path):
    from advisor_deploy.bench.plan import load_cases
    from advisor_shared.benchmark import dataset_digest
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "bench" / "cases.json"
    data = json.loads(source.read_bytes())
    path = tmp_path / "duplicate-cases.json"
    path.write_text('{"version":1,' + json.dumps(data)[1:], encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path, local_plan(dataset_sha256=dataset_digest(data)))


def cloud_plan_values():
    from datetime import datetime, timedelta, timezone

    return dict(
        target_url="https://target.example", callback_url="https://observer.example/bench/replies",
        expected_target_id="owned-target", profile="concurrency", case_ids=["kb-01"],
        seed=17, dataset_sha256="a" * 64,
        results=dict(account_url="https://ownedresults.blob.core.windows.net",
                     container="benchmark-results"),
        auth=dict(
            tenant_id="00000000-0000-0000-0000-000000000001",
            audience="00000000-0000-0000-0000-000000000002",
            scope="api://00000000-0000-0000-0000-000000000002/.default",
            driver_client_id="00000000-0000-0000-0000-000000000003",
            operator_client_ids=["00000000-0000-0000-0000-000000000004"],
            operator_principal_ids=["00000000-0000-0000-0000-000000000005"],
            target_client_ids=["00000000-0000-0000-0000-000000000006"],
            target_principal_ids=["00000000-0000-0000-0000-000000000007"]),
        deployment=dict(
            target_id="owned-target", endpoint="https://target.example",
            callback_url="https://observer.example/bench/replies", region="southeastasia",
            host="aca", host_mode="aca-zero", commit_sha="a" * 40, artifact_sha256="b" * 64,
            config_sha256="c" * 64, dataset_sha256="a" * 64,
            model_deployment="approved-model", model_version="approved-version",
            model_sku="GlobalStandard", retrieval_config_sha256="d" * 64,
            python_version="3.12.10", memory_mb=512, max_instances=40,
            worker_parallelism=4, worker_processes=1, queue_batch_size=1,
            queue_poll_seconds=1., min_replicas=0, always_ready=0,
            max_concurrency=128, max_requests_per_run=10000, max_submit_seconds=1800.,
            driver_replicas=1, driver_processes=1, driver_cpu_cores=1., driver_memory_mb=512,
            dedicated_benchmark_audience="00000000-0000-0000-0000-000000000002",
            results_account_url="https://ownedresults.blob.core.windows.net",
            config_verified=True, cloud_smoke_verified=True, capacity_verified=True,
            entra_registration_verified=True,
            valid_until=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()))


def test_cloud_plan_enforces_budgets_role_scope_and_separate_identity_allowlists():
    from advisor_deploy.bench.plan import RunPlan

    values = cloud_plan_values()
    assert RunPlan.model_validate(values).request_timeout_seconds == 600
    for change in (
        {"target_completions": 49}, {"min_seconds": 179.}, {"max_requests": 10001},
        {"callback_url": "https://different.example/bench/replies"},
        {"expected_target_id": "not-owned"}, {"driver_max_loop_lag_seconds": float("inf")},
        {"auth": values["auth"] | {"scope": "https://management.azure.com/.default"}},
        {"auth": values["auth"] | {"operator_client_ids": values["auth"]["target_client_ids"]}},
        {"deployment": values["deployment"] | {"capacity_verified": False}},
        {"deployment": values["deployment"] | {"valid_until": "2020-01-01T00:00:00Z"}},
    ):
        with pytest.raises(ValueError):
            RunPlan.model_validate(values | change)


async def test_cold_requires_real_zero_and_matching_first_request_boot_evidence():
    from advisor_deploy.bench.runner import ZeroProof, run_level

    async def handler(body, send, tasks):
        await send(body, telemetry={"boots": [
            {"group": "app", "boot_id": "existing-boot", "instance_id": "instance-1",
             "first_request": False}]})
        return ack(body)

    async def supplied_proof(since):
        return [ZeroProof(
            target_id="local-synthetic", group="app", observed_at=since, replicas=0,
            source="arm", evidence_id="operator-supplied-proof")]

    async with harness(handler, profile="cold") as values:
        plan, submitter, receiver, requests, _ = values
        result = await run_level(plan, submitter, receiver, concurrency=1,
                                 before_load=supplied_proof)
        assert result.cold["category"] == "unverified"
        assert result.cold["groups"]["app"]["category"] == "unverified"


async def test_no_zero_proof_means_no_load_and_no_fabricated_cold_sample():
    from advisor_deploy.bench.runner import run_level

    async def handler(body, send, tasks):
        raise AssertionError("no target messages permitted without zero proof")

    async def no_proof(since):
        return []

    async with harness(handler, profile="cold") as values:
        plan, submitter, receiver, requests, registrations = values
        result = await run_level(plan, submitter, receiver, concurrency=1,
                                 before_load=no_proof)
        assert len(registrations) == 1
        assert requests == []
        assert result.samples == []
        assert result.stop_reason == "zero_not_observed"
        assert result.cold["category"] == "unverified"


async def test_later_rejection_after_ambiguous_http_does_not_release_slot_as_failed():
    from advisor_deploy.bench.runner import run_level

    attempts = 0

    async def handler(body, send, tasks):
        nonlocal attempts
        attempts += 1
        return web.Response(status=503 if attempts == 1 else 403)

    async with harness(handler) as values:
        plan, submitter, receiver, requests, registrations = values
        result = await run_level(plan, submitter, receiver, concurrency=1)
        assert len({body["request_id"] for body in requests}) == 1
        assert result.samples[0].status == "censored"


async def test_request_budget_smaller_than_slot_count_still_submits_reserved_work():
    from advisor_deploy.bench.runner import run_level

    async def handler(body, send, tasks):
        await send(body)
        return ack(body)

    async with harness(handler, max_requests=1) as values:
        plan, submitter, receiver, requests, registrations = values
        result = await run_level(plan, submitter, receiver, concurrency=2)
        assert len(requests) == 1
        assert result.samples[0].status == "success"


async def test_expired_manifest_is_rechecked_before_registration_after_operator_wait():
    from advisor_deploy.bench.plan import RunPlan
    from advisor_deploy.bench.runner import run_level

    plan = RunPlan.model_validate(cloud_plan_values())
    plan.deployment.valid_until = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        await run_level(plan, None, None, concurrency=1)


async def test_warmup_has_separate_run_id_and_is_not_added_to_measured_warm_samples():
    from advisor_deploy.bench.runner import run_plan

    async def handler(body, send, tasks):
        await send(body)
        return ack(body)

    async with harness(handler, profile="warm", min_seconds=0., max_requests=110,
                       max_submit_seconds=30., request_timeout_seconds=2.,
                       trend_seconds=1., warmup_requests=2) as values:
        plan, submitter, receiver, requests, registrations = values
        results = await run_plan(plan, submitter, receiver)
        assert [result.profile for result in results] == ["warmup", "warm"]
        assert [len(result.samples) for result in results] == [2, 100], [
            (result.stop_reason, result.driver) for result in results]
        assert results[0].run_id != results[1].run_id


async def test_stop_submission_bound_does_not_keep_retrying_during_drain():
    from advisor_deploy.bench.runner import run_level

    async def handler(body, send, tasks):
        await asyncio.sleep(1.2)
        return web.Response(status=503)

    async with harness(handler, min_seconds=0., max_submit_seconds=1.,
                       request_timeout_seconds=3.) as values:
        plan, submitter, receiver, requests, registrations = values
        result = await run_level(plan, submitter, receiver, concurrency=1)
        assert len(requests) == 1
        assert result.samples[0].status == "censored"


def test_cloud_results_require_existing_private_storage_and_no_sas_or_key_settings():
    from advisor_deploy.bench.plan import ResultsPlan, RunPlan

    valid = dict(account_url="https://ownedresults.blob.core.windows.net",
                 container="benchmark-results")
    assert ResultsPlan(**valid).max_pending_events == 256
    for changes in (
        {"account_url": "http://ownedresults.blob.core.windows.net"},
        {"account_url": "https://ownedresults.blob.core.windows.net?sig=private"},
        {"container": "unrelated"}, {"connection_string": "private"},
        {"max_pending_events": 100000}, {"operation_timeout_seconds": 600.},
    ):
        with pytest.raises(ValueError):
            ResultsPlan(**(valid | changes))
    with pytest.raises(ValueError):
        RunPlan.model_validate(cloud_plan_values() | {"results": None})

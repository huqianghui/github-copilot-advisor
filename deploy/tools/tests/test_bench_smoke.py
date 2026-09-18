from dataclasses import asdict
import json

from aiohttp import ClientSession, TCPConnector, web
import pytest

from advisor_deploy.bench.models import Sample
from advisor_deploy.bench.plan import DeploymentBounds, RunPlan
from test_bench_runner import cloud_plan_values, local_plan
from test_bench_control import https_server, receiver, signer, tls


def smoke_values(count=1, scenario="synthetic", verified=False):
    values = cloud_plan_values()
    values.update(profile="cloud-smoke", scenario=scenario, local_test=False,
                  concurrency_levels=[1], min_seconds=0., target_completions=count,
                  max_requests=count, max_submit_seconds=600.)
    values["deployment"]["cloud_smoke_verified"] = verified
    return values


@pytest.mark.parametrize("scenario", ["synthetic", "real_agent"])
@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("count", [1, 5])
def test_cloud_smoke_allows_only_explicit_bounded_functional_bootstrap(scenario, verified, count):
    plan = RunPlan.model_validate(smoke_values(count, scenario, verified))
    assert plan.profile == "cloud-smoke"
    assert plan.local_test is False
    assert plan.deployment.cloud_smoke_verified is verified
    assert plan.max_requests == plan.target_completions == count
    assert plan.request_timeout_seconds == plan.drain_seconds == 600


@pytest.mark.parametrize("profile", ["warm", "concurrency", "cold"])
def test_normal_cloud_profiles_still_require_verified_smoke(profile):
    values = cloud_plan_values() | {"profile": profile}
    if profile == "cold":
        values["zero_evidence_path"] = "results/zero-evidence.json"
    values["deployment"]["cloud_smoke_verified"] = False
    with pytest.raises(ValueError):
        RunPlan.model_validate(values)
    values["deployment"]["cloud_smoke_verified"] = True
    assert RunPlan.model_validate(values).deployment.cloud_smoke_verified is True


@pytest.mark.parametrize("value", [0, 1, "true", "false", None])
def test_cloud_smoke_verification_is_a_strict_boolean(value):
    values = cloud_plan_values()["deployment"] | {"cloud_smoke_verified": value}
    with pytest.raises(ValueError):
        DeploymentBounds.model_validate(values)


@pytest.mark.parametrize("changes", [
    {"concurrency_levels": [2]}, {"concurrency_levels": [1, 2]},
    {"min_seconds": .1}, {"target_completions": 0, "max_requests": 0},
    {"target_completions": 6, "max_requests": 6}, {"max_requests": 2},
    {"max_submit_seconds": 600.001}, {"max_submit_seconds": 0.},
    {"max_submit_seconds": float("inf")},
    {"request_timeout_seconds": 599.}, {"request_timeout_seconds": 601.},
    {"drain_seconds": 599.}, {"drain_seconds": 601.}, {"trend_seconds": 59.},
    {"scenario": "other"}, {"auth": None}, {"results": None}, {"deployment": None},
])
def test_cloud_smoke_rejects_budget_deadline_scenario_and_auth_shortcuts(changes):
    with pytest.raises(ValueError):
        RunPlan.model_validate(smoke_values() | changes)


def test_cloud_smoke_cannot_use_the_anonymous_local_fixture_boundary():
    values = local_plan().model_dump() | {
        "profile": "cloud-smoke", "min_seconds": 0., "target_completions": 1, "max_requests": 1}
    with pytest.raises(ValueError):
        RunPlan.model_validate(values)


@pytest.mark.parametrize("gate", ["config_verified", "capacity_verified", "entra_registration_verified"])
def test_cloud_smoke_preserves_all_other_deployment_gates(gate):
    values = smoke_values()
    values["deployment"][gate] = False
    with pytest.raises(ValueError):
        RunPlan.model_validate(values)


@pytest.mark.parametrize("change", ["scope", "driver_budget", "submit_budget", "target", "audience"])
def test_cloud_smoke_preserves_manifest_and_dedicated_scope_checks(change):
    values = smoke_values()
    if change == "scope":
        values["auth"]["scope"] = "https://management.azure.com/.default"
    elif change == "driver_budget":
        values.update(target_completions=2, max_requests=2)
        values["deployment"]["max_requests_per_run"] = 1
    elif change == "submit_budget":
        values["deployment"]["max_submit_seconds"] = 599.
    elif change == "target":
        values["expected_target_id"] = "unapproved-target"
    else:
        values["auth"]["audience"] = "api://" + values["auth"]["audience"]
    with pytest.raises(ValueError):
        RunPlan.model_validate(values)


@pytest.mark.parametrize("outcome", ["success", "degraded", "failed", "unknown", "censored"])
def test_functional_smoke_report_preserves_timings_and_never_promotes_verification(outcome, tmp_path):
    from advisor_deploy.bench.report import read_result, render_report, write_result
    from advisor_deploy.bench.runner import RunResult

    plan = RunPlan.model_validate(smoke_values()).model_dump()
    sample = Sample("request-1", "kb-01", outcome, 0., None if outcome == "censored" else 2.)
    result = RunResult(
        run_id="smoke-run", profile="cloud-smoke", concurrency=1,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:03Z",
        window_seconds=3., submission_seconds=2.5, stop_reason="target_reached",
        samples=[sample], plan=plan)
    before = asdict(result)
    report = render_report(result)
    assert "**FUNCTIONAL SMOKE**" in report
    assert "NOT A CAPACITY OR PERFORMANCE RECOMMENDATION" in report
    assert "cloud_smoke_verified: false (unchanged)" in report
    assert "Operator review of completed-question and evidence" in report
    assert "Smoke observations:" in report
    assert "Sampling: **sufficient**" not in report
    if outcome != "censored":
        assert f"| {outcome} | 1 | 2000.000 |" in report
    else:
        assert "| censored | 1 | n/a |" in report
    assert asdict(result) == before
    path = tmp_path / "smoke.json"
    write_result(result, path)
    restored = read_result(json.loads(path.read_bytes()))
    assert restored.plan["deployment"]["cloud_smoke_verified"] is False
    assert restored.samples[0] == sample


@pytest.mark.parametrize("count", [1, 5])
async def test_existing_runner_completes_only_smoke_budget_via_real_local_https(tls, count):
    from advisor_deploy.bench.runner import HttpSubmitter, run_plan

    initial = RunPlan.model_validate(smoke_values(count))
    observer = receiver(initial, tls[0])
    registrations, requests = [], []
    async with ClientSession(connector=TCPConnector(ssl=tls[2])) as client:
        async with https_server(observer.application(), tls) as callback_origin:
            values = smoke_values(count)
            values["callback_url"] = callback_origin + "/bench/replies"
            values["deployment"]["callback_url"] = values["callback_url"]
            callback_token = await signer(
                initial, tls[0], oid=initial.auth.target_principal_ids[0])(
                    initial.auth.target_client_ids[0], initial.auth.scope)

            async def register(request):
                body = await request.json()
                registrations.append(body)
                return web.json_response({
                    "run_id": body["run_id"], "target_id": initial.expected_target_id,
                    "dataset_sha256": initial.dataset_sha256}, status=201)

            async def message(request):
                body = await request.json()
                requests.append(body)
                ids = dict(run_id=body["run_id"], request_id=body["request_id"],
                           job_id="job-" + body["request_id"])
                async with client.post(values["callback_url"], json={
                    **ids, "event": "final", "outcome": "success",
                    "reply": {"type": "message", "format": "markdown",
                              "text": "HTTPS smoke fixture only; no model invocation."},
                }, headers={"Authorization": "Bearer " + callback_token}) as response:
                    assert response.status == 200
                return web.json_response(ids, status=202)

            app = web.Application()
            app.router.add_post("/bench/runs", register)
            app.router.add_post("/bench/messages", message)
            async with https_server(app, tls) as target:
                values["target_url"] = values["deployment"]["endpoint"] = target
                plan = RunPlan.model_validate(values)

                async def token():
                    return await signer(plan, tls[0])(plan.auth.driver_client_id, plan.auth.scope)

                results = await run_plan(plan, HttpSubmitter(client, plan, token), observer)
    assert len(registrations) == len(results) == 1
    assert registrations[0]["profile"] == "cloud-smoke"
    assert registrations[0]["max_requests"] == count
    assert registrations[0]["max_concurrency"] == 1
    assert len(requests) == len(results[0].samples) == count
    assert results[0].stop_reason == "target_reached"
    assert all(sample.status == "success" and sample.duration_ms > 0 for sample in results[0].samples)
    assert results[0].plan["deployment"]["cloud_smoke_verified"] is False
    assert plan.deployment.cloud_smoke_verified is False

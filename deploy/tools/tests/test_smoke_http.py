"""Real loopback HTTP contracts; the CLI still requires an owned Azure origin."""
import asyncio
from contextlib import asynccontextmanager
import json
import subprocess
import sys

from aiohttp import web
import pytest

from advisor_deploy import smoke
from advisor_deploy.manifest import group_id, new_manifest


def deployed_manifest(tmp_path, *, target="aca", mode="entra"):
    data = new_manifest(
        target, "11111111-2222-4333-8444-555555555555", "smoke", "a" * 64, "b" * 64)
    group = group_id(target)
    kind = "Microsoft.Web/sites" if target == "functions" else "Microsoft.App/containerApps"
    app_id = f"{group}/providers/{kind}/smoke-app"
    storage_id = f"{group}/providers/Microsoft.Storage/storageAccounts/smokestorage"
    identity_id = f"{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/smoke"
    endpoint = ("https://smoke-app.azurewebsites.net" if target == "functions"
                else "https://smoke-app.environment-abc.southeastasia.azurecontainerapps.io")
    data.update(
        status="deployed", owned_resource_ids=[app_id, storage_id, identity_id],
        summary={"openai_auth": mode, "search_auth": mode},
        outputs={
            "target": target, "resource_group": data["resource_group"],
            "app_id": app_id, "app_name": "smoke-app", "endpoint": endpoint,
            "storage_id": storage_id,
            "blob_url": "https://smokestorage.blob.core.windows.net/",
            "queue_url": "https://smokestorage.queue.core.windows.net/",
            "identity_id": identity_id,
            "identity_client_id": "22222222-2222-4222-8222-222222222222",
            "identity_principal_id": "33333333-3333-4333-8333-333333333333",
            "bot_id": "", "created_role_assignment_ids": [],
        })
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def write_evidence(tmp_path, data, **changes):
    document = {
        "format": "advisor-smoke-evidence-v1",
        "resource_id": data["outputs"]["app_id"],
        "artifact_digest": data["artifact_digest"],
        "config_digest": data["config_digest"],
        "auth_mode": "entra",
        "checks": {name: "passed" for name in smoke.CHECKS},
        "embedding_dimensions": 3072,
        **changes,
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@asynccontextmanager
async def server(*, health_status=200, health_body=b"ok", bot_status=401, bench_status=403,
                 redirect=None, delay=0, body_size=0):
    requests = []
    app = web.Application()

    async def handler(request):
        body = await request.read()
        requests.append((request.method, request.path, dict(request.headers), body))
        if delay:
            await asyncio.sleep(delay)
        if redirect:
            return web.Response(status=302, headers={"Location": redirect})
        status = {
            "/healthz": health_status, "/api/messages": bot_status, "/bench/runs": bench_status,
        }[request.path]
        response = web.Response(
            status=status, body=b"x" * body_size if body_size else (
                health_body if request.path == "/healthz" else b"private-error-body"))
        response.set_cookie("Authorization", "private-cookie")
        return response

    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        yield origin, requests
    finally:
        await runner.cleanup()


def route_to_loopback(monkeypatch, origin, expected_origin):
    probe = smoke._probe_origin

    async def local(validated_origin, **kwargs):
        assert validated_origin == expected_origin
        return await probe(origin, **kwargs)

    monkeypatch.setattr(smoke, "_probe_origin", local)


@pytest.mark.parametrize("mode", ["key", "entra"])
async def test_real_http_probes_are_anonymous_and_do_not_imply_an_answer(tmp_path, monkeypatch, mode):
    import aiohttp
    path, data = deployed_manifest(tmp_path, mode=mode)

    def forbidden(*args):
        raise AssertionError("ambient credentials were consulted")

    monkeypatch.setattr(aiohttp.ClientSession, "_get_netrc_auth", forbidden)
    async with server() as (origin, requests):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode=mode)
    assert [(method, route) for method, route, _, _ in requests] == [
        ("GET", "/healthz"), ("POST", "/api/messages"), ("POST", "/bench/runs")]
    for _, _, headers, body in requests:
        assert "Authorization" not in headers and "Cookie" not in headers
        assert body in (b"", b"{}")
    assert report["checks"]["health"] == "passed"
    assert report["checks"]["bot_auth"] == report["checks"]["benchmark_auth"] == "passed"
    assert report["checks"]["complete_answer"] == report["checks"]["history_recovery"] == "not_run"
    assert report["checks"]["teams_client"] == report["checks"]["embedding"] == "not_run"
    assert not report["passed"]
    assert report["auth_mode"] == mode
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize("bot,bench", [(202, 202), (200, 200), (500, 500)])
async def test_acceptance_or_errors_never_pass_unauthenticated_auth_checks(
        tmp_path, monkeypatch, bot, bench):
    path, data = deployed_manifest(tmp_path)
    async with server(bot_status=bot, bench_status=bench) as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra")
    assert report["checks"]["bot_auth"] == report["checks"]["benchmark_auth"] == "failed"
    assert report["checks"]["ingress_ack"] == report["checks"]["complete_answer"] == "not_run"


async def test_disabled_benchmark_is_not_run_even_when_evidence_claims_passed(tmp_path, monkeypatch):
    path, data = deployed_manifest(tmp_path)
    evidence = write_evidence(tmp_path, data)
    async with server(bench_status=404) as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra", evidence_path=evidence)
    assert report["checks"]["benchmark_auth"] == "not_run"
    assert not report["passed"]


async def test_disabled_benchmark_never_requires_reading_an_error_body():
    async with server(bench_status=404, body_size=100) as (origin, _):
        observations = await smoke._probe_origin(origin, max_response_bytes=32)
    assert observations["benchmark_auth"]["status"] == "not_run"
    assert observations["benchmark_auth"]["http_status"] == 404


@pytest.mark.parametrize("status,body", [(202, b"ok"), (200, b"unhealthy"), (200, b"private-ok-body")])
async def test_health_requires_200_and_exact_ok_body(tmp_path, monkeypatch, status, body):
    path, data = deployed_manifest(tmp_path)
    async with server(health_status=status, health_body=body) as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra")
    assert report["checks"]["health"] == "failed"
    assert "private-" not in json.dumps(report)


async def test_redirects_are_never_followed(tmp_path, monkeypatch):
    path, data = deployed_manifest(tmp_path)
    async with server() as (other, followed):
        async with server(redirect=other + "/healthz?private-token") as (origin, _):
            route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
            report = await smoke.run_smoke(path, auth_mode="entra")
    assert followed == []
    assert all(report["checks"][name] == "failed" for name in ("health", "bot_auth", "benchmark_auth"))
    assert "private-token" not in json.dumps(report)


async def test_response_limit_and_timeout_are_explicit_safe_failures():
    async with server(body_size=100) as (origin, _):
        observations = await smoke._probe_origin(origin, max_response_bytes=32)
    assert all(value["category"] == "response_too_large" for value in observations.values())
    async with server(delay=0.15) as (origin, _):
        observations = await smoke._probe_origin(origin, timeout_seconds=0.03)
    assert all(value["category"] == "timeout" and value["status"] == "failed"
               for value in observations.values())


async def test_closed_endpoint_is_failed_without_exception_or_url():
    async with server() as (origin, _):
        pass
    observations = await smoke._probe_origin(origin, timeout_seconds=1)
    assert all(value["status"] == "failed"
               and value["category"] in ("connection_error", "timeout")
               for value in observations.values())
    assert origin not in json.dumps(observations)


async def test_connection_exception_is_sanitized(monkeypatch):
    import aiohttp

    async def failed(*args, **kwargs):
        raise aiohttp.ClientConnectionError("private-token https://private.invalid/")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", failed)
    async with server() as (origin, _):
        observations = await smoke._probe_origin(origin)
    assert all(value["category"] == "connection_error" for value in observations.values())
    assert "private-" not in json.dumps(observations)


@pytest.mark.parametrize("target", ["aca", "functions"])
async def test_cli_writes_incomplete_report_and_nonzero_exit(tmp_path, monkeypatch, target):
    path, data = deployed_manifest(tmp_path, target=target)
    output = tmp_path / "report.json"
    async with server() as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        code = await asyncio.to_thread(smoke.main, [
            "--deployment", str(path), "--auth-mode", "entra", "--output", str(output)])
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code != 0 and not report["passed"]
    assert report["resource_id"] == data["outputs"]["app_id"]
    assert report["checks"]["teams_client"] == "not_run"


async def test_only_fully_scoped_complete_evidence_and_live_checks_pass_cli(tmp_path, monkeypatch):
    path, data = deployed_manifest(tmp_path)
    evidence = write_evidence(tmp_path, data)
    output = tmp_path / "report.json"
    async with server() as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        code = await asyncio.to_thread(smoke.main, [
            "--deployment", str(path), "--auth-mode", "entra", "--output", str(output),
            "--evidence", str(evidence)])
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0 and report["passed"]


@pytest.mark.parametrize("change", [
    {"status": "planned"}, {"status": "failed"}, {"discovery_complete": False},
    {"endpoint": "https://private.invalid/"},
    {"endpoint": "http://127.0.0.1:8000"},
    {"endpoint": "https://other.environment.southeastasia.azurecontainerapps.io"},
    {"endpoint": "https://smoke-app.environment.southeastasia.azurecontainerapps.io.evil.invalid"},
    {"endpoint": "https://smoke-app.environment.southeastasia.azurecontainerapps.io/private-path"},
    {"endpoint": "https://smoke-app.environment.southeastasia.azurecontainerapps.io:444"},
    {"endpoint": "https://user:private-token@smoke-app.azurewebsites.net"},
    {"app_name": "other"}, {"owned_resource_ids": []},
])
async def test_cli_rejects_unowned_unsafe_or_incomplete_deployment_before_network(
        tmp_path, monkeypatch, change):
    path, data = deployed_manifest(tmp_path)
    for name, value in change.items():
        if name in ("endpoint", "app_name"):
            data["outputs"][name] = value
        else:
            data[name] = value
    path.write_text(json.dumps(data), encoding="utf-8")

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid deployment reached the network")

    monkeypatch.setattr(smoke, "_probe_origin", forbidden)
    output = tmp_path / "report.json"
    code = await asyncio.to_thread(smoke.main, [
        "--deployment", str(path), "--auth-mode", "entra", "--output", str(output)])
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code != 0 and not report["passed"] and report["issues"]
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize("field,value", [
    ("format", "other"), ("resource_id", "private-resource"),
    ("artifact_digest", "c" * 64), ("config_digest", "d" * 64), ("auth_mode", "key"),
    ("authorization", "private-token"), ("body", "private-body"),
])
async def test_mismatched_or_body_bearing_evidence_is_rejected(
        tmp_path, monkeypatch, field, value):
    path, data = deployed_manifest(tmp_path)
    evidence = write_evidence(tmp_path, data, **{field: value})
    async with server() as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra", evidence_path=evidence)
    assert not report["passed"] and report["evidence_import"] == "failed"
    assert report["checks"]["complete_answer"] == "not_run"
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize("dimension,want", [(None, "not_run"), (1536, "failed"), (3072, "passed")])
async def test_embedding_requires_observed_3072_dimensions(
        tmp_path, monkeypatch, dimension, want):
    path, data = deployed_manifest(tmp_path)
    evidence = write_evidence(tmp_path, data, embedding_dimensions=dimension)
    async with server() as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra", evidence_path=evidence)
    assert report["checks"]["embedding"] == want
    assert report["embedding_dimensions"] == dimension
    assert report["passed"] is (want == "passed")


async def test_missing_evidence_proof_never_infers_history_or_teams(tmp_path, monkeypatch):
    path, data = deployed_manifest(tmp_path)
    evidence = write_evidence(tmp_path, data, checks={
        "complete_answer": "passed", "ingress_ack": "passed",
        "model": "passed", "search": "passed"})
    async with server() as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra", evidence_path=evidence)
    assert report["checks"]["history_recovery"] == report["checks"]["teams_client"] == "not_run"
    assert report["checks"]["embedding"] == "not_run"
    assert not report["passed"]


def test_output_cannot_overwrite_deployment_or_evidence(tmp_path):
    path, data = deployed_manifest(tmp_path)
    before = path.read_bytes()
    assert smoke.main(["--deployment", str(path), "--auth-mode", "entra", "--output", str(path)]) != 0
    assert path.read_bytes() == before


def test_invalid_manifest_is_reported_without_echoing_secrets(tmp_path, capsys):
    path = tmp_path / "deployment.json"
    path.write_text('{"private-token":"private-body"}', encoding="utf-8")
    output = tmp_path / "report.json"
    assert smoke.main(["--deployment", str(path), "--auth-mode", "entra", "--output", str(output)]) != 0
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err + output.read_text(encoding="utf-8")


def test_module_entry_point_has_the_required_arguments():
    result = subprocess.run(
        [sys.executable, "-m", "advisor_deploy.smoke", "--help"],
        capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0
    for argument in ("--deployment", "--auth-mode", "--output", "--evidence"):
        assert argument in result.stdout


async def test_functions_regional_hostname_cannot_target_a_different_app(tmp_path, monkeypatch):
    path, data = deployed_manifest(tmp_path, target="functions")
    data["outputs"]["endpoint"] = (
        "https://smoke-app-other-abcdef1234567890.southeastasia-01.azurewebsites.net")
    path.write_text(json.dumps(data), encoding="utf-8")

    async def forbidden(*args, **kwargs):
        pytest.fail("a different app hostname reached HTTP")

    monkeypatch.setattr(smoke, "_probe_origin", forbidden)
    report = await smoke.run_smoke(path, auth_mode="entra")
    assert not report["passed"] and report["issues"]


@pytest.mark.parametrize("proof", [
    {"embedding_dimensions": True}, {"embedding_dimensions": "3072"},
    {"checks": {"embedding": True}}, {"checks": {"history_recovery": "private-body"}},
    {"checks": {"arbitrary_body": "passed"}},
])
async def test_evidence_does_not_coerce_or_echo_ambiguous_proof(tmp_path, monkeypatch, proof):
    path, data = deployed_manifest(tmp_path)
    evidence = write_evidence(tmp_path, data, **proof)
    async with server() as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra", evidence_path=evidence)
    assert report["evidence_import"] == "failed" and not report["passed"]
    assert "private-" not in json.dumps(report)


async def test_live_failure_overrides_imported_passed_evidence(tmp_path, monkeypatch):
    path, data = deployed_manifest(tmp_path)
    evidence = write_evidence(tmp_path, data)
    async with server(health_status=503, bot_status=202) as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra", evidence_path=evidence)
    assert report["checks"]["health"] == report["checks"]["bot_auth"] == "failed"
    assert not report["passed"]


async def test_missing_dimension_field_is_not_configured_dimension_proof(tmp_path, monkeypatch):
    path, data = deployed_manifest(tmp_path)
    evidence = write_evidence(tmp_path, data)
    document = json.loads(evidence.read_text(encoding="utf-8"))
    del document["embedding_dimensions"]
    evidence.write_text(json.dumps(document), encoding="utf-8")
    async with server() as (origin, _):
        route_to_loopback(monkeypatch, origin, data["outputs"]["endpoint"])
        report = await smoke.run_smoke(path, auth_mode="entra", evidence_path=evidence)
    assert report["checks"]["embedding"] == "not_run"
    assert report["embedding_dimensions"] is None
    assert not report["passed"]


@pytest.mark.parametrize("mode", ["key", "entra"])
@pytest.mark.parametrize("present", [(), ("openai_auth",), ("search_auth",)])
async def test_both_deployed_auth_modes_are_required_before_probes_or_evidence(
        tmp_path, monkeypatch, mode, present):
    path, data = deployed_manifest(tmp_path, mode=mode)
    data["summary"] = {name: mode for name in present}
    path.write_text(json.dumps(data), encoding="utf-8")
    evidence = write_evidence(tmp_path, data, auth_mode=mode)

    async def no_probe(*args, **kwargs):
        pytest.fail("unverified deployment auth mode reached HTTP")

    def no_import(*args, **kwargs):
        pytest.fail("unverified deployment auth mode imported acceptance evidence")

    monkeypatch.setattr(smoke, "_probe_origin", no_probe)
    monkeypatch.setattr(smoke, "_import_evidence", no_import)
    report = await smoke.run_smoke(path, auth_mode=mode, evidence_path=evidence)
    assert report["issues"] == ["deployment_auth_mode_unverified"]
    assert report["evidence_import"] == "not_run"
    assert all(status == "not_run" for status in report["checks"].values())
    assert not report["passed"]


async def test_explicit_deployed_auth_mode_mismatch_has_a_safe_reason(tmp_path, monkeypatch):
    path, data = deployed_manifest(tmp_path, mode="entra")
    data["summary"]["search_auth"] = "key"
    path.write_text(json.dumps(data), encoding="utf-8")

    async def no_probe(*args, **kwargs):
        pytest.fail("mismatched deployment auth mode reached HTTP")

    monkeypatch.setattr(smoke, "_probe_origin", no_probe)
    report = await smoke.run_smoke(path, auth_mode="entra")
    assert report["issues"] == ["deployment_auth_mode_mismatch"]
    assert not report["passed"]

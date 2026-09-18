import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
import ssl
import time

from aiohttp import ClientSession, TCPConnector, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import jwt
import pytest

from advisor_deploy.bench.plan import RunPlan
from advisor_deploy.bench.receiver import ReplyReceiver
from advisor_shared.entra_api import EntraApiValidator
from test_bench_runner import cloud_plan_values


@pytest.fixture(autouse=True)
def deployed_driver_identity(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", cloud_plan_values()["auth"]["driver_client_id"])


@pytest.fixture
def tls(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "local-cert.pem", tmp_path / "local-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=str(cert_path))
    return key, server, client


@asynccontextmanager
async def https_server(app, tls):
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls[1])
        await site.start()
        yield f"https://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def make_plan(origin="https://observer.example", **changes):
    values = cloud_plan_values()
    values["callback_url"] = origin + "/bench/replies"
    values["deployment"]["callback_url"] = values["callback_url"]
    values["auth"]["operator_client_ids"] = [values["auth"]["driver_client_id"]]
    values.update(changes)
    return RunPlan.model_validate(values)


def signer(plan, key, **changes):
    async def provider(client_id, scope):
        now = int(time.time())
        claims = dict(
            iss=f"https://login.microsoftonline.com/{plan.auth.tenant_id}/v2.0",
            tid=plan.auth.tenant_id, aud=scope.removeprefix("api://").removesuffix("/.default"),
            azp=client_id, oid=plan.auth.operator_principal_ids[0], ver="2.0",
            roles=["Benchmark.Invoke"], iat=now - 1, nbf=now - 1, exp=now + 120)
        return jwt.encode(claims | changes, key, algorithm="RS256",
                          headers={"kid": "control-test"})
    return provider


def receiver(plan, key):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="control-test", alg="RS256")

    async def keys(token):
        return jwt.PyJWK.from_dict(public)

    return ReplyReceiver(
        operator_validator=EntraApiValidator(
            tenant_id=plan.auth.tenant_id, audience=plan.auth.audience,
            allowed_client_ids=plan.auth.operator_client_ids,
            allowed_principal_ids=plan.auth.operator_principal_ids, key_provider=keys),
        callback_validator=EntraApiValidator(
            tenant_id=plan.auth.tenant_id, audience=plan.auth.audience,
            allowed_client_ids=plan.auth.target_client_ids,
            allowed_principal_ids=plan.auth.target_principal_ids, key_provider=keys))


async def test_start_and_status_use_driver_identity_fixed_routes_and_real_auth(tls):
    from advisor_deploy.bench.control import request_control

    initial = make_plan()
    observer = receiver(initial, tls[0])
    async with https_server(observer.application(), tls) as origin:
        plan = make_plan(origin)
        assert plan.auth.driver_client_id != plan.auth.audience
        async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
            provider = signer(plan, tls[0])
            assert await request_control(plan, "status", token_provider=provider, session=session) == (
                {"status": "waiting"})
            assert await request_control(plan, "start", token_provider=provider, session=session) == (
                {"status": "starting"})
            assert observer.start_requested.is_set()
            assert await request_control(plan, "status", token_provider=provider, session=session) == (
                {"status": "starting"})


@pytest.mark.parametrize("deployed_client_id", [
    None, "not-a-guid", "00000000-0000-0000-0000-000000000099",
])
async def test_deployed_identity_must_match_plan_before_control_submission(
    tls, monkeypatch, deployed_client_id,
):
    from advisor_deploy.bench.control import ControlError, request_control

    if deployed_client_id is None:
        monkeypatch.delenv("AZURE_CLIENT_ID")
    else:
        monkeypatch.setenv("AZURE_CLIENT_ID", deployed_client_id)
    observer = receiver(make_plan(), tls[0])
    async with https_server(observer.application(), tls) as origin:
        plan = make_plan(origin)
        async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
            with pytest.raises(ControlError, match="deployed_driver_identity_mismatch"):
                await request_control(plan, "start", token_provider=signer(plan, tls[0]),
                                      session=session)
    assert not observer.start_requested.is_set()


@pytest.mark.parametrize("change", ["client", "principal", "audience", "role"])
async def test_operator_allowlist_and_application_token_requirements_fail_closed(tls, change):
    from advisor_deploy.bench.control import ControlError, request_control

    plan = make_plan()
    observer = receiver(plan, tls[0])
    async with https_server(observer.application(), tls) as origin:
        plan = make_plan(origin)
        claims = {}
        if change == "client":
            values = plan.model_dump()
            values["auth"]["operator_client_ids"] = [plan.auth.audience]
            plan = RunPlan.model_validate(values)
        elif change == "principal":
            claims["oid"] = "00000000-0000-0000-0000-000000000099"
        elif change == "audience":
            claims["aud"] = "api://" + plan.auth.audience
        else:
            claims["roles"] = []
        async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
            with pytest.raises(ControlError):
                await request_control(plan, "start", token_provider=signer(plan, tls[0], **claims),
                                      session=session)
        assert not observer.start_requested.is_set()


async def test_redirect_is_not_followed_and_response_body_is_not_exposed(tls):
    from advisor_deploy.bench.control import ControlError, request_control

    leaked_requests = []
    destination = web.Application()

    async def capture(request):
        leaked_requests.append(request.path)
        return web.json_response({"status": "starting"})

    destination.router.add_route("*", "/capture", capture)
    async with https_server(destination, tls) as other:
        app = web.Application()

        async def redirect(request):
            return web.Response(status=307, headers={"Location": other + "/capture"},
                                text="private-upstream-error")

        app.router.add_post("/bench/control/start", redirect)
        async with https_server(app, tls) as origin:
            plan = make_plan(origin)
            async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
                with pytest.raises(ControlError) as error:
                    await request_control(plan, "start", token_provider=signer(plan, tls[0]),
                                          session=session)
            assert "private-upstream-error" not in str(error.value)
    assert leaked_requests == []


@pytest.mark.parametrize("body", [
    b"x" * 5000,
    b'{"status":"private-token-value"}',
    b'{"status":"waiting","token":"private-token-value"}',
    b'{"status":"waiting","status":"running"}',
])
async def test_control_response_is_bounded_strict_and_safe(tls, body):
    from advisor_deploy.bench.control import ControlError, request_control

    app = web.Application()

    async def response(request):
        return web.Response(body=body, content_type="application/json")

    app.router.add_get("/bench/control/status", response)
    async with https_server(app, tls) as origin:
        plan = make_plan(origin)
        async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
            with pytest.raises(ControlError) as error:
                await request_control(plan, "status", token_provider=signer(plan, tls[0]),
                                      session=session)
        assert "private-token-value" not in str(error.value)


async def test_unverified_local_claim_checks_do_not_replace_receiver_signature_validation(tls):
    from advisor_deploy.bench.control import ControlError, request_control

    observer = receiver(make_plan(), tls[0])
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with https_server(observer.application(), tls) as origin:
        plan = make_plan(origin)
        async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
            with pytest.raises(ControlError, match="control_http_401"):
                await request_control(plan, "start", token_provider=signer(plan, other_key),
                                      session=session)
    assert not observer.start_requested.is_set()


async def test_api_registration_cannot_be_selected_as_driver_managed_identity(tls):
    from advisor_deploy.bench.control import ControlError, request_control

    values = make_plan().model_dump()
    values["auth"]["driver_client_id"] = values["auth"]["audience"]
    values["auth"]["operator_client_ids"] = [values["auth"]["audience"]]
    plan = RunPlan.model_validate(values)
    observer = receiver(plan, tls[0])
    async with https_server(observer.application(), tls) as origin:
        values["callback_url"] = origin + "/bench/replies"
        values["deployment"]["callback_url"] = values["callback_url"]
        plan = RunPlan.model_validate(values)
        async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
            with pytest.raises(ControlError, match="driver_identity_must_be_distinct"):
                await request_control(plan, "start", token_provider=signer(plan, tls[0]),
                                      session=session)
    assert not observer.start_requested.is_set()


async def test_complete_chunked_response_is_bounded_not_just_first_json_chunk(tls):
    from advisor_deploy.bench.control import ControlError, request_control

    app = web.Application()

    async def streamed(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(b'{"status":"waiting"}')
        await asyncio.sleep(.01)
        await response.write(b" " * 4097)
        await response.write_eof()
        return response

    app.router.add_get("/bench/control/status", streamed)
    async with https_server(app, tls) as origin:
        plan = make_plan(origin)
        async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
            with pytest.raises(ControlError, match="control_response_too_large"):
                await request_control(plan, "status", token_provider=signer(plan, tls[0]),
                                      session=session)


async def test_http_and_token_timeouts_are_bounded_and_cancel_pending_work(tls, monkeypatch):
    import advisor_deploy.bench.control as control

    app = web.Application()

    async def delayed(request):
        await asyncio.sleep(.3)
        return web.json_response({"status": "waiting"})

    app.router.add_get("/bench/control/status", delayed)
    monkeypatch.setattr(control, "HTTP_TIMEOUT_SECONDS", .02)
    monkeypatch.setattr(control, "TOKEN_TIMEOUT_SECONDS", .02)
    cancelled = asyncio.Event()

    async def blocked_token(client_id, scope):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    async with https_server(app, tls) as origin:
        plan = make_plan(origin)
        async with ClientSession(connector=TCPConnector(ssl=tls[2])) as session:
            with pytest.raises(control.ControlError, match="control_unavailable"):
                await control.request_control(plan, "status", token_provider=signer(plan, tls[0]),
                                              session=session)
            with pytest.raises(control.ControlError, match="operator_token_unavailable"):
                await control.request_control(plan, "status", token_provider=blocked_token,
                                              session=session)
    assert cancelled.is_set()


def cold_files(tmp_path, **changes):
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    destination = results / "zero-evidence.json"
    plan = make_plan(profile="cold", zero_evidence_path=str(destination), **changes)
    plan_path = tmp_path / "run.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    return plan, plan_path, destination


def proof(plan, **changes):
    return dict(
        target_id=plan.expected_target_id, group="app",
        observed_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        replicas=0, source="arm", evidence_id="supplied-control-plane-record") | changes


def test_write_zero_replaces_only_configured_file_without_modifying_observations(tmp_path):
    from advisor_deploy.bench.control import write_zero
    from advisor_deploy.bench.runner import supplied_zero_evidence

    plan, plan_path, destination = cold_files(tmp_path)
    destination.write_text("[]")
    other = destination.parent / "keep.json"
    other.write_text("unchanged")
    raw = json.dumps([proof(plan)], indent=2).encode()
    assert write_zero(plan, raw, plan_path=plan_path) == 1
    assert destination.read_bytes() == raw
    assert other.read_text() == "unchanged"
    assert {path.name for path in destination.parent.iterdir()} == {"zero-evidence.json", "keep.json"}
    observed = supplied_zero_evidence(
        destination, target_id=plan.expected_target_id,
        since=(datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat())
    assert observed[0].model_dump() == json.loads(raw)[0]
    assert write_zero(plan, b"[]", plan_path=plan_path) == 0


@pytest.mark.parametrize("change", [
    {"target_id": "another-target"}, {"group": "http"}, {"replicas": 1},
    {"replicas": False}, {"source": "health"}, {"observed_at": "2026-01-01T00:00:00"},
    {"observed_at": "9999-01-01T00:00:00Z"}, {"observed_at": "2000-01-01T00:00:00Z"},
    {"token": "private-input"},
])
def test_invalid_zero_observations_leave_previous_file_unchanged(tmp_path, change):
    from advisor_deploy.bench.control import ControlError, write_zero

    plan, plan_path, destination = cold_files(tmp_path)
    destination.write_bytes(b"[]")
    with pytest.raises(ControlError) as error:
        write_zero(plan, json.dumps([proof(plan, **change)]).encode(), plan_path=plan_path)
    assert destination.read_bytes() == b"[]"
    assert "private-input" not in str(error.value)


def test_zero_input_rejects_duplicates_oversize_and_duplicate_groups(tmp_path):
    from advisor_deploy.bench.control import ControlError, write_zero

    plan, plan_path, destination = cold_files(tmp_path)
    item = json.dumps(proof(plan))
    for raw in (
        b" " * (16 * 1024 + 1),
        ('[' + item[:-1] + ',"replicas":0}]').encode(),
        json.dumps([proof(plan), proof(plan)]).encode(),
        b'{"not":"a list"}',
    ):
        with pytest.raises(ControlError):
            write_zero(plan, raw, plan_path=plan_path)
    assert not destination.exists()


@pytest.mark.parametrize("relative", ["outside.json", "results/../outside.json", "../outside.json",
                                     "results/nested/zero.json"])
def test_zero_destination_cannot_escape_or_change_the_fixed_results_directory(tmp_path, relative):
    from advisor_deploy.bench.control import ControlError, write_zero

    plan, plan_path, destination = cold_files(tmp_path)
    plan = RunPlan.model_validate(plan.model_dump() | {"zero_evidence_path": relative})
    with pytest.raises(ControlError):
        write_zero(plan, json.dumps([proof(plan)]).encode(), plan_path=plan_path)
    assert not destination.exists()


def test_zero_destination_rejects_hard_links(tmp_path):
    from advisor_deploy.bench.control import ControlError, write_zero

    plan, plan_path, destination = cold_files(tmp_path)
    protected = tmp_path / "unrelated.json"
    protected.write_text("unchanged")
    os.link(protected, destination)
    with pytest.raises(ControlError):
        write_zero(plan, json.dumps([proof(plan)]).encode(), plan_path=plan_path)
    assert protected.read_text() == "unchanged"


def test_zero_destination_rejects_symbolic_links(tmp_path):
    from advisor_deploy.bench.control import ControlError, write_zero

    plan, plan_path, destination = cold_files(tmp_path)
    protected = tmp_path / "unrelated.json"
    protected.write_text("unchanged")
    try:
        destination.symlink_to(protected)
    except OSError:
        pytest.skip("host does not permit creating test symlinks")
    with pytest.raises(ControlError):
        write_zero(plan, json.dumps([proof(plan)]).encode(), plan_path=plan_path)
    assert protected.read_text() == "unchanged"


def test_flex_proofs_preserve_distinct_http_and_worker_observations(tmp_path):
    from advisor_deploy.bench.control import write_zero

    plan, plan_path, destination = cold_files(tmp_path)
    values = plan.model_dump()
    values["deployment"].update(host="functions", host_mode="flex-on-demand")
    plan = RunPlan.model_validate(values)
    supplied = [
        proof(plan, group="http", evidence_id="supplied-http-record"),
        proof(plan, group="worker", source="azure_monitor", evidence_id="supplied-worker-record"),
    ]
    raw = json.dumps(supplied).encode()
    assert write_zero(plan, raw, plan_path=plan_path) == 2
    assert json.loads(destination.read_bytes()) == supplied


def test_failed_atomic_replace_preserves_old_file_and_removes_temporary_file(tmp_path, monkeypatch):
    from advisor_deploy.bench.control import ControlError, write_zero
    import advisor_deploy.bench.control as control

    plan, plan_path, destination = cold_files(tmp_path)
    destination.write_bytes(b"[]")

    def fail_replace(source, target):
        raise OSError("private-filesystem-details")

    monkeypatch.setattr(control.os, "replace", fail_replace)
    with pytest.raises(ControlError) as error:
        write_zero(plan, json.dumps([proof(plan)]).encode(), plan_path=plan_path)
    assert "private-filesystem-details" not in str(error.value)
    assert destination.read_bytes() == b"[]"
    assert list(destination.parent.iterdir()) == [destination]


def test_cli_proof_write_and_errors_never_echo_supplied_bodies(tmp_path, capsys):
    from advisor_deploy.bench.control import main

    plan, plan_path, destination = cold_files(tmp_path)
    assert main(["--plan", str(plan_path), "write-zero", "--json",
                 json.dumps([proof(plan)])]) == 0
    assert json.loads(capsys.readouterr().out) == {"written": 1}
    assert destination.exists()
    assert main(["--plan", str(plan_path), "write-zero", "--json",
                 '[{"token":"private-input"}]']) == 2
    captured = capsys.readouterr()
    assert "private-input" not in captured.out + captured.err
    assert "error" in json.loads(captured.out)
    assert main(["--plan", str(plan_path), "start", "--token", "private-input"]) == 2
    captured = capsys.readouterr()
    assert "private-input" not in captured.out + captured.err

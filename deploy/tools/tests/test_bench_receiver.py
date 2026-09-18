import asyncio
from contextlib import asynccontextmanager
import json
import time

from aiohttp import ClientSession, web
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest

from advisor_shared.entra_api import EntraApiValidator


TENANT = "00000000-0000-0000-0000-000000000001"
AUDIENCE = "00000000-0000-0000-0000-000000000002"
OPERATOR = "00000000-0000-0000-0000-000000000003"
TARGET = "00000000-0000-0000-0000-000000000004"


@asynccontextmanager
async def running_server(app):
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.fixture
def auth():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="local-boundary", alg="RS256")

    async def key_provider(token):
        return jwt.PyJWK.from_dict(public)

    def validator(identity):
        return EntraApiValidator(
            tenant_id=TENANT, audience=AUDIENCE, allowed_client_ids=[identity],
            allowed_principal_ids=[identity], key_provider=key_provider)

    def token(identity, **changes):
        now = int(time.time())
        claims = dict(iss=f"https://login.microsoftonline.com/{TENANT}/v2.0",
                      aud=AUDIENCE, ver="2.0", tid=TENANT, azp=identity, oid=identity,
                      roles=["Benchmark.Invoke"], iat=now - 1, nbf=now - 1,
                      exp=now + 120)
        return jwt.encode(claims | changes, key, algorithm="RS256",
                          headers={"kid": "local-boundary"})

    return validator, token


def reply(**changes):
    body = dict(run_id="run-1", request_id="request-1", job_id="job-1",
                event="final", outcome="success") | changes
    if body["event"] == "final" and body["outcome"] in {"success", "degraded"}:
        body.setdefault("reply", {"type": "message", "format": "markdown",
                                    "text": "A complete local rendered answer."})
    return body


def make_receiver(auth):
    from advisor_deploy.bench.receiver import ReplyReceiver

    validator, _ = auth
    receiver = ReplyReceiver(
        operator_validator=validator(OPERATOR), callback_validator=validator(TARGET))
    receiver.begin_run("run-1")
    return receiver


async def post(session, url, body, token):
    async with session.post(url, json=body,
                            headers={"Authorization": f"Bearer {token}"}) as response:
        body = (await response.json() if response.content_type == "application/json"
                else {"error": await response.text()})
        return response.status, body


async def test_authenticated_final_before_ack_and_duplicate_are_counted_once(auth):
    receiver = make_receiver(auth)
    entry = receiver.register("run-1", "request-1", "kb-01")
    _, token = auth
    async with running_server(receiver.application()) as url, ClientSession() as client:
        status, _ = await post(client, url + "/bench/replies", reply(), token(TARGET))
        assert status == 200
        sample = await entry.future
        await asyncio.sleep(.01)
        receiver.acknowledge("run-1", "request-1", "job-1")
        assert sample.duration_ms is not None
        processing_ms = entry.observer_processing_ms
        status, body = await post(client, url + "/bench/replies", reply(), token(TARGET))
        assert (status, body["disposition"]) == (200, "duplicate")
        assert receiver.samples("run-1")[0].finished_s == sample.finished_s
        assert receiver.samples("run-1")[0].ack_ms >= sample.duration_ms
        assert entry.observer_processing_ms == processing_ms


async def test_typing_never_finishes_and_deadline_late_reply_is_not_success(auth):
    receiver = make_receiver(auth)
    entry = receiver.register("run-1", "request-1", "kb-01")
    _, token = auth
    async with running_server(receiver.application()) as url, ClientSession() as client:
        assert (await post(client, url + "/bench/replies",
                           reply(event="typing", outcome=None), token(TARGET)))[0] == 200
        assert not entry.future.done()
        receiver.censor("run-1", "request-1")
        status, body = await post(client, url + "/bench/replies", reply(), token(TARGET))
        assert (status, body["disposition"]) == (200, "late")
        assert receiver.samples("run-1")[0].status == "censored"
        assert receiver.samples("run-1")[0].finished_s is None


async def test_callback_and_operator_allowlists_are_separate_and_fail_closed(auth):
    receiver = make_receiver(auth)
    entry = receiver.register("run-1", "request-1", "kb-01")
    _, token = auth
    async with running_server(receiver.application()) as url, ClientSession() as client:
        for credential in (token(OPERATOR), token(TARGET, aud="https://api.botframework.com"),
                           token(TARGET, roles=[]), "not-a-token"):
            assert (await post(client, url + "/bench/replies", reply(), credential))[0] == 401
        assert (await post(client, url + "/bench/control/start", {}, token(TARGET)))[0] == 401
        assert (await post(client, url + "/bench/control/start", {}, token(OPERATOR)))[0] == 202
        assert receiver.start_requested.is_set()
        assert not entry.future.done()
        assert (await post(client, url + "/bench/control/start",
                           {"target_url": "https://elsewhere.invalid"}, token(OPERATOR)))[0] == 400


async def test_unknown_request_after_receiver_restart_and_wrong_job_are_rejected(auth):
    receiver = make_receiver(auth)
    receiver.register("run-1", "request-1", "kb-01")
    receiver.acknowledge("run-1", "request-1", "expected-job")
    _, token = auth
    async with running_server(receiver.application()) as url, ClientSession() as client:
        assert (await post(client, url + "/bench/replies", reply(), token(TARGET)))[0] == 409
        assert (await post(client, url + "/bench/replies",
                           reply(request_id="lost-on-restart"), token(TARGET)))[0] == 404
        assert (await post(client, url + "/bench/replies",
                           reply(text="private"), token(TARGET)))[0] == 400


async def test_receiver_deadline_not_monitor_scheduling_decides_late_callback(auth):
    receiver = make_receiver(auth)
    entry = receiver.register("run-1", "request-1", "kb-01", deadline_seconds=.001)
    _, token = auth
    await asyncio.sleep(.03)
    async with running_server(receiver.application()) as url, ClientSession() as client:
        status, body = await post(client, url + "/bench/replies", reply(), token(TARGET))
        assert (status, body["disposition"]) == (200, "late")
        assert (await entry.future).status == "censored"


async def test_auth_service_outage_is_503_without_anonymous_fallback(auth):
    from advisor_deploy.bench.receiver import ReplyReceiver

    validator, token = auth

    async def unavailable(bearer):
        raise jwt.PyJWKClientConnectionError("never persisted")

    receiver = ReplyReceiver(
        operator_validator=validator(OPERATOR),
        callback_validator=EntraApiValidator(
            tenant_id=TENANT, audience=AUDIENCE, allowed_client_ids=[TARGET],
            allowed_principal_ids=[TARGET], key_provider=unavailable))
    async with running_server(receiver.application()) as url, ClientSession() as client:
        assert (await post(client, url + "/bench/replies", reply(), token(TARGET)))[0] == 503


async def test_malformed_callback_json_is_bad_request_not_job_conflict(auth):
    receiver = make_receiver(auth)
    _, token = auth
    async with running_server(receiver.application()) as url, ClientSession() as client:
        async with client.post(url + "/bench/replies", data="{broken",
                               headers={"Authorization": "Bearer " + token(TARGET),
                                        "Content-Type": "application/json"}) as response:
            assert response.status == 400


async def test_plan_v2_guid_audience_works_through_real_signed_control_and_callback_http(auth):
    from advisor_deploy.bench.__main__ import configured_receiver
    from advisor_deploy.bench.plan import AuthPlan

    validator, token = auth
    plan = AuthPlan(
        tenant_id=TENANT, audience=AUDIENCE, scope=f"api://{AUDIENCE}/.default",
        driver_client_id=OPERATOR, operator_client_ids=[OPERATOR],
        operator_principal_ids=[OPERATOR], target_client_ids=[TARGET],
        target_principal_ids=[TARGET])
    receiver = configured_receiver(plan, key_provider=validator(TARGET)._key_provider)
    receiver.begin_run("run-1")
    receiver.register("run-1", "request-1", "kb-01")
    async with running_server(receiver.application()) as url, ClientSession() as client:
        for path, identity, body in (
            ("/bench/control/start", OPERATOR, {}),
            ("/bench/replies", TARGET, reply()),
        ):
            wrong = await post(client, url + path, body, token(identity, aud=f"api://{AUDIENCE}"))
            assert wrong[0] == 401
            accepted = await post(client, url + path, body, token(identity))
            assert accepted[0] == (202 if path.endswith("start") else 200)


async def test_empty_success_message_is_rejected_and_rendered_body_is_never_retained(auth, caplog):
    from dataclasses import asdict

    receiver = make_receiver(auth)
    receiver.register("run-1", "request-1", "kb-01")
    _, token = auth
    private = "unique-transient-rendered-body-DO-NOT-PERSIST"
    async with running_server(receiver.application()) as url, ClientSession() as client:
        for message in (None, {"type": "message", "format": "markdown", "text": "  "}):
            status, _ = await post(client, url + "/bench/replies",
                                   reply(reply=message), token(TARGET))
            assert status == 400
        status, _ = await post(client, url + "/bench/replies", reply(reply={
            "type": "message", "format": "markdown", "text": private}), token(TARGET))
        assert status == 200
    assert private not in repr(receiver.entries)
    assert private not in json.dumps([asdict(s) for s in receiver.samples("run-1")])
    assert private not in caplog.text


async def test_full_body_clock_excludes_jwks_delay_even_when_auth_crosses_deadline(auth):
    from advisor_deploy.bench.receiver import ReplyReceiver

    validator, token = auth
    original = validator(TARGET)

    async def delayed_key(bearer):
        await asyncio.sleep(1.2)
        return await original._key_provider(bearer)

    receiver = ReplyReceiver(
        operator_validator=validator(OPERATOR),
        callback_validator=EntraApiValidator(
            tenant_id=TENANT, audience=AUDIENCE, allowed_client_ids=[TARGET],
            allowed_principal_ids=[TARGET], key_provider=delayed_key),
        auth_timeout_seconds=2.)
    bearer = token(TARGET)
    async with running_server(receiver.application()) as url, ClientSession() as client:
        receiver.begin_run("run-1")
        entry = receiver.register("run-1", "request-1", "kb-01", deadline_seconds=1.)
        waiter = asyncio.create_task(receiver.wait_for_terminal("run-1", "request-1"))
        status, _ = await post(client, url + "/bench/replies", reply(), bearer)
        assert status == 200
        sample = await waiter
        assert sample.status == "success"
        assert sample.duration_ms < 1000
        assert entry.observer_processing_ms >= 1000


async def test_oversized_callback_body_is_bounded_and_never_completes(auth):
    receiver = make_receiver(auth)
    entry = receiver.register("run-1", "request-1", "kb-01")
    _, token = auth
    async with running_server(receiver.application()) as url, ClientSession() as client:
        async with client.post(url + "/bench/replies", data=b"x" * (256 * 1024 + 1),
                               headers={"Authorization": "Bearer " + token(TARGET)}) as response:
            assert response.status == 413
    assert not entry.future.done()


async def test_rejected_duplicate_cannot_clear_another_callbacks_bounded_auth_grace(auth):
    from advisor_deploy.bench.receiver import ReplyReceiver

    validator, token = auth
    key_entered = asyncio.Event()
    original = validator(TARGET)

    async def delayed_key(bearer):
        key_entered.set()
        await asyncio.sleep(1.2)
        return await original._key_provider(bearer)

    receiver = ReplyReceiver(
        operator_validator=validator(OPERATOR),
        callback_validator=EntraApiValidator(
            tenant_id=TENANT, audience=AUDIENCE, allowed_client_ids=[TARGET],
            allowed_principal_ids=[TARGET], key_provider=delayed_key),
        auth_timeout_seconds=2.)
    bearer = token(TARGET)
    async with running_server(receiver.application()) as url, ClientSession() as client:
        receiver.begin_run("run-1")
        receiver.register("run-1", "request-1", "kb-01", deadline_seconds=1.)
        waiter = asyncio.create_task(receiver.wait_for_terminal("run-1", "request-1"))
        valid = asyncio.create_task(post(client, url + "/bench/replies", reply(), bearer))
        await key_entered.wait()
        assert (await post(client, url + "/bench/replies", reply(), "not-a-token"))[0] == 401
        assert (await valid)[0] == 200
        assert (await waiter).status == "success"

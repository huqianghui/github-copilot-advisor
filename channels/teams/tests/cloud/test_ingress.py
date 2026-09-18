import json
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest
from microsoft_agents.activity import InvokeResponse
from microsoft_agents.hosting.core import ActivityHandler, AgentAuthConfiguration
from microsoft_agents.hosting.core.authorization.jwt import JwtTokenValidator
from microsoft_agents.hosting.aiohttp import CloudAdapter

from test_executor import APP_ID, ConnectorBoundary, payload_for

TENANT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_token(key, **changes):
    now = int(time.time())
    claims = {"iss": "https://api.botframework.com", "aud": APP_ID,
              "exp": now + 600, "nbf": now - 10, "iat": now - 10}
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "offline-key"})


@pytest.fixture
def auth_case(signing_key, monkeypatch):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public.update(kid="offline-key", alg="RS256")
    calls = []

    async def key_boundary(uri, header):
        calls.append(uri)
        return jwt.PyJWK.from_dict(public)

    monkeypatch.setattr(JwtTokenValidator._jwk_client_manager, "get_signing_key", key_boundary)
    config = AgentAuthConfiguration(
        client_id=APP_ID, tenant_id=TENANT, anonymous_allowed=False, validate_issuer=True)
    return SimpleNamespace(config=config, key=signing_key, calls=calls)


@pytest.fixture
def ingress_case(auth_case):
    class Jobs:
        def __init__(self):
            self.accepted = {}
            self.error = None

        async def submit(self, payload):
            if self.error:
                raise self.error
            duplicate = payload.job_id in self.accepted
            self.accepted[payload.job_id] = payload
            return SimpleNamespace(duplicate=duplicate, job_id=payload.job_id, sequence=1)

    class Dispatcher:
        def __init__(self):
            self.notifications = []
            self.error = None

        async def notify(self, partition, trace):
            if self.error:
                raise self.error
            self.notifications.append((partition, trace))

    class ProtocolAgent(ActivityHandler):
        async def on_invoke_activity(self, context):
            return InvokeResponse(status=200, body={"protocol": "ok"})

        async def on_message_activity(self, context):
            await context.send_activity("synchronous answer")

    return SimpleNamespace(
        auth=auth_case, jobs=Jobs(), dispatcher=Dispatcher(),
        adapter=CloudAdapter(channel_service_client_factory=ConnectorBoundary()),
        agent=ProtocolAgent())


def ingress(case):
    from teams_adapter.cloud.ingress import CloudIngress
    return CloudIngress(
        case.jobs, case.dispatcher, bot_app_id=APP_ID, auth_config=case.auth.config,
        protocol_adapter=case.adapter, protocol_agent=case.agent)


async def test_real_sdk_verifies_signed_bot_jwt(auth_case):
    from teams_adapter.cloud.auth import validate_bot_request
    claims = await validate_bot_request(
        {"Authorization": f"Bearer {signed_token(auth_case.key)}"}, auth_case.config)
    assert claims.get_app_id() == APP_ID
    assert auth_case.calls == ["https://login.botframework.com/v1/.well-known/keys"]


@pytest.mark.parametrize("changes", [
    {"aud": "wrong-app"}, {"iss": "https://untrusted.invalid"},
    {"exp": 1}, {"nbf": 9999999999}, {"exp": None},
])
async def test_bad_signed_claims_are_rejected(auth_case, changes):
    from teams_adapter.cloud.auth import AuthRejected, validate_bot_request
    with pytest.raises(AuthRejected):
        await validate_bot_request(
            {"Authorization": f"Bearer {signed_token(auth_case.key, **changes)}"},
            auth_case.config)


@pytest.mark.parametrize("error_type", [
    jwt.PyJWKClientConnectionError, jwt.PyJWKSetError, jwt.PyJWKError,
])
async def test_key_infrastructure_failure_is_not_unauthorized(auth_case, monkeypatch, error_type):
    from teams_adapter.cloud.auth import AuthUnavailable, validate_bot_request

    async def unavailable(*args):
        raise error_type("private-key-service-url")

    monkeypatch.setattr(JwtTokenValidator._jwk_client_manager, "get_signing_key", unavailable)
    with pytest.raises(AuthUnavailable):
        await validate_bot_request(
            {"Authorization": f"Bearer {signed_token(auth_case.key)}"}, auth_case.config)


@pytest.mark.parametrize("setting", ["ANONYMOUS_ALLOWED", "VALIDATE_ISSUER"])
async def test_unsafe_auth_configuration_is_rejected(auth_case, setting):
    from teams_adapter.cloud.auth import AuthUnavailable, validate_bot_request
    setattr(auth_case.config, setting, setting == "ANONYMOUS_ALLOWED")
    with pytest.raises(AuthUnavailable):
        await validate_bot_request({}, auth_case.config)


async def test_missing_jwt_never_enqueues(ingress_case):
    result = await ingress(ingress_case).handle_http(
        "POST", {}, payload_for().activity)
    assert result.status == 401
    assert ingress_case.jobs.accepted == {}
    assert ingress_case.auth.calls == []


@pytest.mark.parametrize("kind", ["groupChat", "channel"])
async def test_unaddressed_group_message_is_accepted_without_persistence_or_wake(
        ingress_case, kind):
    activity = payload_for("private-unaddressed-content", kind=kind).activity
    activity["entities"] = []
    response = await post(ingress_case, activity)
    assert response.status == 202 and response.body is None
    assert ingress_case.jobs.accepted == {}
    assert ingress_case.dispatcher.notifications == []
    assert ingress_case.adapter._channel_service_client_factory.identities == []


@pytest.mark.parametrize(("failure", "reason"), [
    ("json", "invalid_json"), ("activity", "invalid_activity"),
    ("conflict", "admission_conflict"),
])
async def test_admission_rejections_log_only_safe_reason_and_status(
        ingress_case, failure, reason, caplog):
    from advisor_agent.durable.jobs import JobConflict

    body = payload_for("private-admission-body").activity
    if failure == "json":
        body = b'{"private-admission-body": invalid'
    elif failure == "activity":
        body["text"] = {"private-admission-body": "private-value"}
    else:
        ingress_case.jobs.error = JobConflict("private-admission-body")
    response = await post(ingress_case, body)
    assert response.status == 400
    diagnostic = next(record.telemetry for record in caplog.records
                      if record.msg == "teams_ingress_rejected")
    assert diagnostic["reason"] == reason
    assert diagnostic["http_status"] == 400
    assert "private-admission-body" not in caplog.text
    assert "private-value" not in caplog.text


async def post(case, activity=None):
    return await ingress(case).handle_http(
        "POST", {"Authorization": f"Bearer {signed_token(case.auth.key)}"},
        payload_for().activity if activity is None else activity)


async def test_accepts_after_durable_submit_and_notification_and_deduplicates(ingress_case):
    first = await post(ingress_case)
    second = await post(ingress_case)
    assert first.status == second.status == 202
    assert first.body["job_id"] == second.body["job_id"]
    assert second.body["duplicate"]
    assert len(ingress_case.jobs.accepted) == 1
    assert len(ingress_case.dispatcher.notifications) == 2


@pytest.mark.parametrize("boundary", ["jobs", "dispatcher"])
async def test_storage_or_notification_failure_is_503(ingress_case, boundary, caplog):
    getattr(ingress_case, boundary).error = TimeoutError("private-storage-secret")
    result = await post(ingress_case)
    assert result.status == 503
    assert "private-storage-secret" not in caplog.text


@pytest.mark.parametrize("invalid", [
    "tenant", "sender", "role", "id", "service_url", "untrusted_host"])
async def test_invalid_identity_or_bot_source_is_not_admitted(ingress_case, invalid):
    activity = payload_for().activity
    if invalid == "tenant":
        activity["conversation"]["tenantId"] = "conflict"
    elif invalid == "sender":
        del activity["from"]["id"]
    elif invalid == "role":
        activity["from"]["role"] = "bot"
    elif invalid == "id":
        del activity["id"]
    elif invalid == "service_url":
        activity["serviceUrl"] = "https://user:secret@api.botframework.com"
    else:
        activity["serviceUrl"] = "https://untrusted.invalid/bot"
    assert (await post(ingress_case, activity)).status == 400
    assert ingress_case.jobs.accepted == {}


async def test_verified_service_url_claim_must_match_activity_destination(ingress_case):
    bearer = signed_token(ingress_case.auth.key, serviceurl="https://smba.trafficmanager.net")
    result = await ingress(ingress_case).handle_http(
        "POST", {"Authorization": f"Bearer {bearer}"}, payload_for().activity)
    assert result.status == 401
    assert ingress_case.jobs.accepted == {}


async def test_wrong_signature_and_malformed_token_never_enqueue(ingress_case):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bearer in (signed_token(other_key), "not-a-jwt"):
        result = await ingress(ingress_case).handle_http(
            "POST", {"Authorization": f"Bearer {bearer}"}, payload_for().activity)
        assert result.status == 401
    assert ingress_case.jobs.accepted == {}


@pytest.mark.parametrize("mode", ["invoke", "expectReplies"])
async def test_protocol_activities_use_real_synchronous_sdk_pipeline(ingress_case, mode):
    activity = payload_for().activity
    if mode == "invoke":
        activity["type"] = "invoke"
        activity["name"] = "test/invoke"
    else:
        activity["deliveryMode"] = mode
    result = await post(ingress_case, activity)
    assert result.status == 200
    if mode == "invoke":
        assert result.body == {"protocol": "ok"}
    else:
        assert result.body["activities"][0]["text"] == "synchronous answer"
    assert ingress_case.jobs.accepted == {}


async def test_storage_sdk_submission_survives_failed_notify_without_duplicate_jobs(
        ingress_case):
    import importlib.util
    from pathlib import Path
    import sys
    from azure.storage.blob.aio import ContainerClient
    from azure.storage.queue.aio import QueueClient
    from azure.core.credentials import AzureNamedKeyCredential
    from advisor_agent.durable.blob import BlobGateway
    from advisor_agent.durable.jobs import JobStore
    from advisor_agent.durable.queue import OutboxDispatcher, SessionQueue

    path = Path(__file__).parents[4] / "agent" / "tests" / "cloud" / "conftest.py"
    name = "teams_storage_contract_transport"
    spec = importlib.util.spec_from_file_location(name, path)
    transport_module = importlib.util.module_from_spec(spec)
    sys.modules[name] = transport_module
    spec.loader.exec_module(transport_module)
    blob_transport = transport_module.ContractTransport()
    queue_transport = transport_module.QueueTransport()
    gateway = BlobGateway(ContainerClient(
        "https://offline.blob.core.windows.net", "advisor-jobs",
        transport=blob_transport, retry_total=0))
    queue = SessionQueue(QueueClient(
        "https://offline.queue.core.windows.net", "advisor-incoming",
        credential=AzureNamedKeyCredential("offline", "b2ZmbGluZQ=="),
        transport=queue_transport, retry_total=0))
    jobs = JobStore(gateway, bot_id=APP_ID)
    ingress_case.jobs = jobs
    ingress_case.dispatcher = OutboxDispatcher(jobs.inbox, queue)
    queue_transport.failures.append(("send", 503, "ServerBusy"))
    try:
        assert (await post(ingress_case)).status == 503
        response = await post(ingress_case)
        assert response.status == 202 and response.body["duplicate"]
        record = await jobs.read(response.body["job_id"])
        assert len(await jobs.inbox.pending(record.partition_key)) == 1
        assert len(queue_transport.messages) == 1
    finally:
        await queue.aclose()
        await gateway.aclose()

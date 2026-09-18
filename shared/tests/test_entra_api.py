import json
import time

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest

from advisor_shared.entra_api import AuthRejected, AuthUnavailable, EntraApiValidator


TENANT = "00000000-0000-0000-0000-000000000001"
AUDIENCE = "00000000-0000-0000-0000-000000000002"
CLIENT = "00000000-0000-0000-0000-000000000003"
PRINCIPAL = "00000000-0000-0000-0000-000000000004"
OTHER = "00000000-0000-0000-0000-000000000005"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(signing_key):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public.update(kid="test-key", alg="RS256")

    async def key_provider(token):
        return jwt.PyJWK.from_dict(public)

    return EntraApiValidator(
        tenant_id=TENANT, audience=AUDIENCE,
        allowed_client_ids=[CLIENT], allowed_principal_ids=[PRINCIPAL],
        key_provider=key_provider, leeway_seconds=0)


def token(signing_key, **changes):
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": AUDIENCE, "tid": TENANT, "oid": PRINCIPAL, "azp": CLIENT,
        "roles": ["Benchmark.Invoke"], "iat": now - 10, "nbf": now - 10,
        "exp": now + 120,
    }
    claims.update(changes)
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})


async def test_valid_workload_identity_is_returned_without_raw_token(verifier, signing_key):
    bearer = token(signing_key)
    principal = await verifier.verify(bearer)
    assert principal.object_id == PRINCIPAL
    assert principal.application_id == CLIENT
    assert principal.roles == ("Benchmark.Invoke",)
    assert bearer not in repr(principal)


@pytest.mark.parametrize("claims", [
    {"aud": "https://management.azure.com/"},
    {"aud": OTHER}, {"tid": OTHER}, {"oid": OTHER}, {"azp": OTHER},
    {"iss": "https://api.botframework.com"},
    {"iss": f"https://login.microsoftonline.com/{OTHER}/v2.0"},
    {"roles": []}, {"roles": "Benchmark.Invoke"}, {"roles": ["Benchmark.Run"]},
    {"exp": 1}, {"iat": 9999999999}, {"nbf": 9999999999},
    {"oid": None}, {"azp": None}, {"exp": True},
])
async def test_wrong_identity_scope_role_or_time_is_rejected(verifier, signing_key, claims):
    with pytest.raises(AuthRejected):
        await verifier.verify(token(signing_key, **claims))


async def test_wrong_signature_is_rejected(verifier):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthRejected):
        await verifier.verify(token(other_key))


async def test_key_service_outage_is_not_invalid_credentials(signing_key, caplog):
    async def unavailable(bearer):
        raise jwt.PyJWKClientConnectionError("sensitive-upstream-error")

    verifier = EntraApiValidator(
        tenant_id=TENANT, audience=AUDIENCE, allowed_client_ids=[CLIENT],
        allowed_principal_ids=[PRINCIPAL], key_provider=unavailable)
    bearer = token(signing_key)
    with pytest.raises(AuthUnavailable):
        await verifier.verify(bearer)
    assert "sensitive-upstream-error" not in caplog.text
    assert bearer not in caplog.text


@pytest.mark.parametrize("changes", [
    {"tenant_id": "https://untrusted.example"},
    {"audience": ""}, {"allowed_client_ids": []}, {"allowed_principal_ids": []},
    {"allowed_client_ids": ["invalid"]}, {"leeway_seconds": float("inf")},
    {"leeway_seconds": -1}, {"required_role": ""},
])
def test_invalid_configuration_fails_closed(changes):
    settings = dict(
        tenant_id=TENANT, audience=AUDIENCE, allowed_client_ids=[CLIENT],
        allowed_principal_ids=[PRINCIPAL])
    settings.update(changes)
    with pytest.raises(ValueError):
        EntraApiValidator(**settings)


async def test_malformed_bearer_does_not_trigger_key_network_lookup():
    calls = []

    async def key_provider(bearer):
        calls.append(bearer)
        raise AssertionError("malformed token reached the key service")

    verifier = EntraApiValidator(
        tenant_id=TENANT, audience=AUDIENCE, allowed_client_ids=[CLIENT],
        allowed_principal_ids=[PRINCIPAL], key_provider=key_provider)
    with pytest.raises(AuthRejected):
        await verifier.verify("not-a-jwt")
    assert calls == []

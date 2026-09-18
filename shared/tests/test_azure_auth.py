"""Credential selection is explicit, lazy, shared and owned by one lifetime."""
from time import time

from azure.core.credentials import AccessToken, AzureKeyCredential, AzureNamedKeyCredential
import pytest

from advisor_shared import azure_auth
from advisor_shared.azure_auth import AzureAuthSettings, AzureCredentials


def environment(**overrides):
    return {
        "AZURE_OPENAI_ENDPOINT": "https://model.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "private-openai-key",
        "AZURE_SEARCH_ENDPOINT": "https://knowledge.search.windows.net",
        "AZURE_SEARCH_API_KEY": "private-search-key",
        **overrides,
    }


class RecordingCredential:
    def __init__(self, failure=None):
        self.scopes = []
        self.close_count = 0
        self.failure = failure

    async def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes)
        if self.failure:
            raise self.failure
        return AccessToken("fake-entra-token", int(time()) + 3600)

    async def close(self):
        self.close_count += 1


@pytest.mark.parametrize("name", [
    "AZURE_OPENAI_AUTH_MODE", "AZURE_SEARCH_AUTH_MODE", "ADVISOR_STORAGE_AUTH_MODE",
])
@pytest.mark.parametrize("mode", ["automatic", "", "KEY"])
def test_invalid_auth_mode_is_an_error(name, mode):
    with pytest.raises(ValueError, match=name):
        AzureAuthSettings.from_env(environment(**{name: mode}))


@pytest.mark.parametrize("client_id", [None, "", "   "])
def test_managed_identity_requires_explicit_client_id(client_id):
    env = environment(AZURE_OPENAI_AUTH_MODE="entra",
                      ADVISOR_ENTRA_CREDENTIAL="managed_identity")
    if client_id is not None:
        env["AZURE_CLIENT_ID"] = client_id
    with pytest.raises(ValueError, match="AZURE_CLIENT_ID"):
        AzureAuthSettings.from_env(env)


def test_unknown_entra_source_is_an_error():
    with pytest.raises(ValueError, match="ADVISOR_ENTRA_CREDENTIAL"):
        AzureAuthSettings.from_env(environment(ADVISOR_ENTRA_CREDENTIAL="default"))


@pytest.mark.parametrize("name", [
    "AZURE_OPENAI_ENDPOINT", "AZURE_SEARCH_ENDPOINT",
    "ADVISOR_BLOB_SERVICE_URL", "ADVISOR_QUEUE_SERVICE_URL",
])
@pytest.mark.parametrize("value", [
    "not-a-url", "", "http://account.example", "https:///missing-host",
    "https://user:private-password@account.example",
    "https://account.example/?sig=private-sas",
    "https://account.example/#private-fragment",
    "https://account.example:invalid", "https://account.example/\nprivate",
    "https://invalid<host", "https://account.example/\x00private",
])
def test_service_urls_must_be_https_without_embedded_credentials(name, value):
    with pytest.raises(ValueError, match=name) as error:
        AzureAuthSettings.from_env(environment(**{name: value}))
    assert "private-password" not in str(error.value)
    assert "private-sas" not in str(error.value)
    assert "private-fragment" not in str(error.value)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:12345", "http://[::1]:12345", "http://localhost:12345",
])
def test_local_loopback_endpoints_remain_available_for_offline_development(url):
    settings = AzureAuthSettings.from_env(environment(AZURE_OPENAI_ENDPOINT=url))
    assert settings.openai_endpoint == url


@pytest.mark.parametrize("name", ["AZURE_OPENAI_API_KEY", "AZURE_SEARCH_API_KEY"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_configured_key_services_require_nonempty_keys(name, value):
    env = environment()
    if value is None:
        del env[name]
    else:
        env[name] = value
    with pytest.raises(ValueError, match=name):
        AzureAuthSettings.from_env(env)


@pytest.mark.parametrize("missing", [
    "ADVISOR_STORAGE_ACCOUNT_NAME", "ADVISOR_STORAGE_ACCOUNT_KEY",
])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_storage_key_mode_requires_account_name_and_key(missing, value):
    env = environment(
        ADVISOR_STORAGE_AUTH_MODE="key", ADVISOR_STORAGE_ACCOUNT_NAME="storage",
        ADVISOR_STORAGE_ACCOUNT_KEY="private-storage-key")
    if value is None:
        del env[missing]
    else:
        env[missing] = value
    with pytest.raises(ValueError, match=missing):
        AzureAuthSettings.from_env(env)


async def test_local_defaults_keep_keys_and_do_not_construct_storage_identity(monkeypatch):
    def unexpected_identity(**kwargs):
        pytest.fail("memory/key configuration must not create an Entra credential")
    monkeypatch.setattr(azure_auth, "AzureCliCredential", unexpected_identity)
    settings = AzureAuthSettings.from_env(environment())
    credentials = AzureCredentials(settings)
    assert credentials.openai_auth()["api_key"] == "private-openai-key"
    search = credentials.search_credential()
    assert isinstance(search, AzureKeyCredential)
    assert search.key == "private-search-key"
    assert settings.storage_auth_mode == "entra"
    await credentials.aclose()
    await credentials.aclose()
    assert "private-openai-key" not in repr(settings)
    assert "private-search-key" not in repr(settings)
    assert "private-openai-key" not in repr(credentials)


async def test_storage_key_credential_preserves_account_name_and_key():
    credentials = AzureCredentials(AzureAuthSettings.from_env(environment(
        ADVISOR_STORAGE_AUTH_MODE="key", ADVISOR_STORAGE_ACCOUNT_NAME="storage",
        ADVISOR_STORAGE_ACCOUNT_KEY="private-storage-key")))
    storage = credentials.storage_credential()
    assert isinstance(storage, AzureNamedKeyCredential)
    assert storage.named_key.name == "storage"
    assert storage.named_key.key == "private-storage-key"
    assert "private-storage-key" not in repr(credentials.settings)
    await credentials.aclose()


@pytest.mark.parametrize("source", ["azure_cli", "managed_identity"])
async def test_entra_credential_is_selected_once_and_shared_across_services(
        monkeypatch, source):
    created = []
    credential = RecordingCredential()

    def cli(**kwargs):
        created.append(("azure_cli", kwargs))
        return credential

    def managed_identity(**kwargs):
        created.append(("managed_identity", kwargs))
        return credential

    monkeypatch.setattr(azure_auth, "AzureCliCredential", cli)
    monkeypatch.setattr(azure_auth, "ManagedIdentityCredential", managed_identity)
    credentials = AzureCredentials(AzureAuthSettings.from_env(environment(
        AZURE_OPENAI_AUTH_MODE="entra", AZURE_SEARCH_AUTH_MODE="entra",
        ADVISOR_ENTRA_CREDENTIAL=source, AZURE_CLIENT_ID="selected-uami")))
    assert created == []
    auth = credentials.openai_auth()
    assert auth.get("api_key", "") == ""
    assert await auth["azure_ad_token_provider"]() == "fake-entra-token"
    assert await credentials.openai_auth()["azure_ad_token_provider"]() == "fake-entra-token"
    assert credentials.search_credential() is credential
    assert credentials.storage_credential() is credential
    expected_options = {"client_id": "selected-uami"} if source == "managed_identity" else {}
    assert created == [(source, expected_options)]
    assert credential.scopes == [("https://cognitiveservices.azure.com/.default",)]
    await credentials.aclose()
    await credentials.aclose()
    assert credential.close_count == 1


@pytest.mark.parametrize("name,method", [
    ("AZURE_OPENAI_AUTH_MODE", "openai_auth"),
    ("AZURE_SEARCH_AUTH_MODE", "search_credential"),
    ("ADVISOR_STORAGE_AUTH_MODE", "storage_credential"),
])
async def test_each_service_can_choose_entra_independently(monkeypatch, name, method):
    credential = RecordingCredential()
    monkeypatch.setattr(azure_auth, "AzureCliCredential", lambda: credential)
    credentials = AzureCredentials(AzureAuthSettings.from_env(environment(**{name: "entra"})))
    selected = getattr(credentials, method)()
    if method == "openai_auth":
        assert await selected["azure_ad_token_provider"]() == "fake-entra-token"
    else:
        assert selected is credential
    await credentials.aclose()


async def test_entra_does_not_require_legacy_keys(monkeypatch):
    env = environment(AZURE_OPENAI_AUTH_MODE="entra", AZURE_SEARCH_AUTH_MODE="entra")
    del env["AZURE_OPENAI_API_KEY"]
    del env["AZURE_SEARCH_API_KEY"]
    credential = RecordingCredential()
    monkeypatch.setattr(azure_auth, "AzureCliCredential", lambda: credential)
    credentials = AzureCredentials(AzureAuthSettings.from_env(env))
    assert await credentials.openai_auth()["azure_ad_token_provider"]() == "fake-entra-token"
    assert credentials.search_credential() is credential
    await credentials.aclose()


async def test_closed_credentials_cannot_create_or_reuse_clients_or_cached_tokens(monkeypatch):
    credential = RecordingCredential()
    monkeypatch.setattr(azure_auth, "AzureCliCredential", lambda: credential)
    credentials = AzureCredentials(AzureAuthSettings.from_env(environment(
        AZURE_OPENAI_AUTH_MODE="entra")))
    provider = credentials.openai_auth()["azure_ad_token_provider"]
    await provider()
    await credentials.aclose()
    for method in (credentials.openai_auth, credentials.search_credential,
                   credentials.storage_credential):
        with pytest.raises(RuntimeError, match="closed"):
            method()
    with pytest.raises(RuntimeError, match="closed"):
        await provider()


async def test_unused_services_can_be_configured_later_but_keys_never_fallback():
    credentials = AzureCredentials(AzureAuthSettings.from_env({}))
    with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
        credentials.openai_auth()
    with pytest.raises(ValueError, match="AZURE_SEARCH_API_KEY"):
        credentials.search_credential()
    await credentials.aclose()

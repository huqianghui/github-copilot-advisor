"""Azure OpenAI client 的超时/重试策略防线。

这些常量是从真机实测里量出来的(连接耗时中位数 ~8s、最大 10.9s,SDK 默认的
connect=5.0 会在连接本来能建立时提前放弃)。默认值静默回退不会让任何现有测试
变红 —— 只会让 Teams 用户重新开始收到道歉文案。所以在这里钉死。
"""
import asyncio
import json
from time import time
from unittest.mock import AsyncMock, call

from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport
from azure.search.documents.aio import SearchClient
import httpx2
import pytest

from advisor_agent import factory
from advisor_agent.factory import (
    _channel_id_provider,
    _is_group_provider,
    build_advisor,
    build_openai_client,
)
from advisor_agent.maf_backend import MAFBackend
from advisor_agent.run_context import current_run
from advisor_shared import azure_auth
from advisor_shared.azure_auth import AzureAuthSettings, AzureCredentials
from advisor_shared.messages import AdvisorRequest


@pytest.fixture
def azure_env(monkeypatch):
    for name in (
        "AZURE_OPENAI_AUTH_MODE", "AZURE_SEARCH_AUTH_MODE", "AZURE_OPENAI_AD_TOKEN",
        "ADVISOR_STORAGE_AUTH_MODE", "ADVISOR_ENTRA_CREDENTIAL", "AZURE_CLIENT_ID",
        "ADVISOR_STORAGE_ACCOUNT_NAME", "ADVISOR_STORAGE_ACCOUNT_KEY",
        "ADVISOR_BLOB_SERVICE_URL", "ADVISOR_QUEUE_SERVICE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://x.search.windows.net")
    monkeypatch.setenv("AZURE_SEARCH_API_KEY", "k")


def _assert_hardened(client, label: str) -> None:
    # 读 _client.timeout(而不是只读 client.timeout):这是真正交给 httpx2 去
    # 建连接的那份配置。传错类型时(例如 legacy httpx.Timeout)顶层 client
    # .timeout 看着是对的,底层却被当成标量吞掉 —— 只断言顶层抓不到那个变异。
    for timeout in (client.timeout, client._client.timeout):
        assert timeout.connect == 15.0, f"{label}: connect 超时退回默认了"
        # read/write/pool 是 LLM 生成时间,必须保持 SDK 默认 600 不被误伤。
        assert (timeout.read, timeout.write, timeout.pool) == (600, 600, 600)
    # max_retries 是"重试次数"不是"总尝试次数"(openai/_base_client.py:
    # `for retries_taken in range(max_retries + 1)`),1 → 总共 2 次尝试。
    assert client.max_retries == 1, f"{label}: 重试次数退回 SDK 默认 2 了"


def test_build_openai_client_applies_measured_timeout(azure_env):
    _assert_hardened(build_openai_client("2024-10-21"), "出厂口")


def test_build_advisor_hardens_both_chat_and_embed_clients(azure_env):
    """接线防线:两个 client 打的是同一个 endpoint、同一条坏网络。

    chat client 由 MAFBackend 自建过一版,漏掉加固 —— 而真机冒烟的
    ConnectTimeout 打的正是 chat completions 端点(known-issues 5.1)。
    只测出厂口杀不掉"factory 忘了把 client 注进去"这个变异。
    """
    core = build_advisor("teams")
    _assert_hardened(core.backend._client, "chat client")
    _assert_hardened(core.backend._tools._combined.kb.embed, "embed client")


def test_factory_wires_web_budget_independently_of_openai(azure_env, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_BUDGET_SECONDS", "9")
    monkeypatch.setenv("WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS", "3")
    core = build_advisor("teams")
    web = core.backend._tools._web
    assert web.budget == 9
    assert web.timeout == 3
    _assert_hardened(core.backend._client, "chat remains unchanged")


@pytest.mark.parametrize("name", [
    "WEB_SEARCH_BUDGET_SECONDS", "WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_invalid_web_budget_fails_at_startup(azure_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        build_advisor("teams")


def test_maf_backend_prefers_injected_client(azure_env):
    sentinel = object()
    backend = MAFBackend(tools=None, channel_id_provider=lambda: "19:c",
                         is_group_provider=lambda: False, client=sentinel)
    assert backend._client is sentinel


def test_factory_providers_require_a_bound_request():
    for provider in (_channel_id_provider, _is_group_provider):
        with pytest.raises(RuntimeError, match="request context is not bound"):
            provider()


async def test_factory_tool_context_isolated_during_interleaved_turns(
        azure_env, monkeypatch, tmp_path):
    config = tmp_path / "channels.yaml"
    config.write_text(
        "defaults: {}\n"
        "channels:\n"
        "  - channel_id: '19:group'\n"
        "    enterprise_slug: group-enterprise\n"
        "    github_org: group-org\n"
        "    org_token_env: GROUP_TOKEN\n"
        "    contacts: []\n"
        "  - channel_id: '19:private'\n"
        "    enterprise_slug: private-enterprise\n"
        "    github_org: private-org\n"
        "    org_token_env: PRIVATE_TOKEN\n"
        "    contacts: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ESCALATION_CONFIG", str(config))
    monkeypatch.setenv("GROUP_TOKEN", "test-group-token")
    monkeypatch.setenv("PRIVATE_TOKEN", "test-private-token")
    core = build_advisor("teams")
    assert isinstance(core.backend, MAFBackend)
    backend = core.backend
    tools = backend._tools
    diagnostic_client = AsyncMock(return_value={})
    usage_client = AsyncMock(return_value={"source": "private"})
    monkeypatch.setattr(tools._diagnostics, "run", diagnostic_client)
    monkeypatch.setattr(tools._usage, "lookup", usage_client)
    escalation = AsyncMock(wraps=tools.escalate_to_human)
    diagnostics = AsyncMock(wraps=tools.network_diagnostics)
    usage = AsyncMock(wraps=tools.copilot_usage_lookup)
    monkeypatch.setattr(tools, "escalate_to_human", escalation)
    monkeypatch.setattr(tools, "network_diagnostics", diagnostics)
    monkeypatch.setattr(tools, "copilot_usage_lookup", usage)
    group_started, private_started = asyncio.Event(), asyncio.Event()
    group_dispatched = asyncio.Event()
    results = {}

    async def fake_loop(messages):
        label = messages[-1]["content"]
        if label == "group":
            group_started.set()
            await private_started.wait()
        else:
            private_started.set()
            await group_dispatched.wait()
        current_run.get().citations_seen.append(
            {"title": label, "url": f"https://example.test/{label}"})
        await backend._dispatch("escalate_to_human", {"reason": label})
        await backend._dispatch("network_diagnostics", {})
        results[label] = json.loads(await backend._dispatch(
            "copilot_usage_lookup",
            {"question_type": "user_usage", "username": "alice"}))
        if label == "group":
            group_dispatched.set()
        return f"{label}: https://example.test/{label}"

    monkeypatch.setattr(backend, "_run_tool_loop", fake_loop)

    def request(label, is_group):
        return AdvisorRequest(
            text=label, conversation_key=label,
            channel_id=f"19:{label}", user_id=label, user_name=label,
            is_group=is_group,
        )

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        group = tasks.create_task(core.handle(request("group", True)))
        await group_started.wait()
        private = tasks.create_task(core.handle(request("private", False)))
    assert escalation.await_args_list == [
        call("19:group", "group"), call("19:private", "private")]
    assert diagnostics.await_args_list == [
        call("19:group"), call("19:private")]
    assert usage.await_args_list == [
        call("19:group", True, "user_usage", "alice"),
        call("19:private", False, "user_usage", "alice")]
    assert diagnostic_client.await_args_list == [
        call(enterprise_slug="group-enterprise"),
        call(enterprise_slug="private-enterprise")]
    assert results["group"]["status"] == "privacy_blocked"
    assert results["private"]["status"] == "ok"
    usage_client.assert_awaited_once_with(
        "user_usage", "private-org", "test-private-token", "alice")
    assert [c.title for c in group.result().citations] == ["group"]
    assert [c.title for c in private.result().citations] == ["private"]


class _Identity:
    def __init__(self, failure=None):
        self.failure = failure
        self.close_count = 0

    async def get_token(self, *scopes, **kwargs):
        if self.failure:
            raise self.failure
        return AccessToken("fake-selected-identity-token", int(time()) + 3600)

    async def close(self):
        self.close_count += 1


def _openai_transport(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(200, json={
            "object": "list", "model": "test-embedding",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.125]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    transport = httpx2.MockTransport(handle)
    monkeypatch.setattr(httpx2.AsyncClient, "_init_transport",
                        lambda self, **kwargs: transport)
    monkeypatch.setattr(httpx2.AsyncClient, "_get_proxy_map",
                        lambda *args, **kwargs: {})
    return requests


class _SearchResponse(AsyncHttpResponse):
    def __init__(self, request):
        super().__init__(request, None)
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.content_type = "application/json"

    def body(self):
        return b"0"


class _SearchTransport(AsyncHttpTransport):
    def __init__(self):
        self.requests = []
        self.closed = False

    async def open(self):
        pass

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def send(self, request, **kwargs):
        self.requests.append(request)
        return _SearchResponse(request)


def _search_transport(monkeypatch):
    transport = _SearchTransport()
    monkeypatch.setattr(
        factory, "SearchClient",
        lambda *args, **kwargs: SearchClient(*args, transport=transport, **kwargs))
    return transport


async def test_openai_explicit_key_mode_ignores_ambient_ad_token_even_on_copy(
        azure_env, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "fake-ambient-token")
    requests = _openai_transport(monkeypatch)
    client = build_openai_client("2024-10-21")
    copied = client.with_options(max_retries=0)
    try:
        await copied.embeddings.create(model="test-embedding", input="q")
        assert requests[0].headers["api-key"] == "k"
        assert "authorization" not in requests[0].headers
    finally:
        await copied.close()
        await client.close()


@pytest.mark.parametrize("source", ["azure_cli", "managed_identity"])
async def test_openai_entra_request_uses_only_selected_identity(
        azure_env, monkeypatch, source):
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "entra")
    monkeypatch.setenv("ADVISOR_ENTRA_CREDENTIAL", source)
    monkeypatch.setenv("AZURE_CLIENT_ID", "selected-uami")
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "fake-ambient-token")
    selected = []
    identity = _Identity()

    def credential(**kwargs):
        selected.append(kwargs)
        return identity

    monkeypatch.setattr(azure_auth, "AzureCliCredential", credential)
    monkeypatch.setattr(azure_auth, "ManagedIdentityCredential", credential)
    requests = _openai_transport(monkeypatch)
    client = build_openai_client("2024-10-21")
    try:
        _assert_hardened(client, "entra")
        await client.embeddings.create(model="test-embedding", input="q")
        assert requests[0].headers["authorization"] == "Bearer fake-selected-identity-token"
        assert "api-key" not in requests[0].headers
        assert selected == ([{"client_id": "selected-uami"}]
                            if source == "managed_identity" else [{}])
    finally:
        await client.close()
        await client.close()
    assert identity.close_count == 1


async def test_failed_entra_token_never_sends_a_request_with_legacy_key(
        azure_env, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "entra")
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "fake-ambient-token")
    identity = _Identity(ClientAuthenticationError("fake identity rejected"))
    monkeypatch.setattr(azure_auth, "AzureCliCredential", lambda: identity)
    requests = _openai_transport(monkeypatch)
    client = build_openai_client("2024-10-21")
    try:
        with pytest.raises(ClientAuthenticationError):
            await client.embeddings.create(model="test-embedding", input="q")
        assert requests == []
    finally:
        await client.close()
        await client.close()
    assert identity.close_count == 1


@pytest.mark.parametrize("mode", ["key", "entra"])
async def test_advisor_search_uses_explicit_auth_mode(azure_env, monkeypatch, mode):
    monkeypatch.setenv("AZURE_SEARCH_AUTH_MODE", mode)
    identity = _Identity()
    monkeypatch.setattr(azure_auth, "AzureCliCredential", lambda: identity)
    transport = _search_transport(monkeypatch)
    core = build_advisor("teams")
    try:
        assert await core.backend._tools._combined.kb.search_client.get_document_count() == 0
        headers = transport.requests[0].headers
        if mode == "key":
            assert headers["api-key"] == "k"
            assert "Authorization" not in headers
        else:
            assert headers["Authorization"] == "Bearer fake-selected-identity-token"
            assert "api-key" not in headers
    finally:
        await core.aclose()
        await core.aclose()
    assert transport.closed
    assert identity.close_count == (1 if mode == "entra" else 0)
    assert core.backend._client.is_closed()
    assert core.backend._tools._combined.kb.embed.is_closed()


async def test_search_entra_failure_never_sends_legacy_key(azure_env, monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_AUTH_MODE", "entra")
    identity = _Identity(ClientAuthenticationError("fake identity rejected"))
    monkeypatch.setattr(azure_auth, "AzureCliCredential", lambda: identity)
    transport = _search_transport(monkeypatch)
    core = build_advisor("teams")
    try:
        with pytest.raises(ClientAuthenticationError):
            await core.backend._tools._combined.kb.search_client.get_document_count()
        assert transport.requests == []
    finally:
        await core.aclose()


@pytest.mark.parametrize("external_owner", [False, True])
async def test_advisor_reuses_identity_preserves_versions_and_closes_only_owned_credentials(
        azure_env, monkeypatch, external_owner):
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "entra")
    monkeypatch.setenv("AZURE_SEARCH_AUTH_MODE", "entra")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
    identities = []

    def credential():
        identity = _Identity()
        identities.append(identity)
        return identity

    monkeypatch.setattr(azure_auth, "AzureCliCredential", credential)
    requests = _openai_transport(monkeypatch)
    transport = _search_transport(monkeypatch)
    owner = AzureCredentials(AzureAuthSettings.from_env({
        "AZURE_OPENAI_AUTH_MODE": "entra", "AZURE_SEARCH_AUTH_MODE": "entra",
        "AZURE_OPENAI_ENDPOINT": "https://injected.openai.azure.com",
        "AZURE_SEARCH_ENDPOINT": "https://injected.search.windows.net",
    })) if external_owner else None
    core = build_advisor("teams", credentials=owner)
    try:
        chat = core.backend._client
        embed = core.backend._tools._combined.kb.embed
        _assert_hardened(chat, "chat")
        _assert_hardened(embed, "embedding")
        await embed.embeddings.create(model="test-embedding", input="q")
        await chat.embeddings.create(model="test-embedding", input="q")
        await core.backend._tools._combined.kb.search_client.get_document_count()
        assert [request.url.params["api-version"] for request in requests] == [
            "2024-10-21", "2025-04-01-preview"]
        assert len(identities) == 1
        if external_owner:
            assert requests[0].url.host == "injected.openai.azure.com"
            assert transport.requests[0].url.startswith("https://injected.search.windows.net")
    finally:
        await core.aclose()
        await core.aclose()
    assert identities[0].close_count == (0 if external_owner else 1)
    if owner is not None:
        await owner.aclose()
        assert identities[0].close_count == 1


async def test_advisor_closes_credentials_and_other_clients_when_one_close_fails(
        azure_env, monkeypatch):
    monkeypatch.setenv("AZURE_SEARCH_AUTH_MODE", "entra")
    identity = _Identity()
    monkeypatch.setattr(azure_auth, "AzureCliCredential", lambda: identity)
    transport = _search_transport(monkeypatch)
    core = build_advisor("teams")
    close = core.backend._client.close

    async def failed_close():
        await close()
        raise RuntimeError("fake close failure")

    monkeypatch.setattr(core.backend._client, "close", failed_close)
    with pytest.raises(RuntimeError, match="fake close failure"):
        await core.aclose()
    await core.aclose()
    assert identity.close_count == 1
    assert transport.closed
    assert core.backend._tools._combined.kb.embed.is_closed()


async def test_standalone_client_does_not_close_injected_credentials(azure_env, monkeypatch):
    identity = _Identity()
    monkeypatch.setattr(azure_auth, "AzureCliCredential", lambda: identity)
    owner = AzureCredentials(AzureAuthSettings.from_env({
        "AZURE_OPENAI_AUTH_MODE": "entra",
        "AZURE_OPENAI_ENDPOINT": "https://injected.openai.azure.com",
    }))
    client = build_openai_client("2024-10-21", credentials=owner)
    await client.close()
    await client.close()
    assert identity.close_count == 0
    await owner.aclose()
    assert identity.close_count == 1

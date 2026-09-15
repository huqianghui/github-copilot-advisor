"""Azure OpenAI client 的超时/重试策略防线。

这些常量是从真机实测里量出来的(连接耗时中位数 ~8s、最大 10.9s,SDK 默认的
connect=5.0 会在连接本来能建立时提前放弃)。默认值静默回退不会让任何现有测试
变红 —— 只会让 Teams 用户重新开始收到道歉文案。所以在这里钉死。
"""
import asyncio
import json
from unittest.mock import AsyncMock, call

import pytest

from advisor_agent.factory import (
    _channel_id_provider,
    _is_group_provider,
    build_advisor,
    build_openai_client,
)
from advisor_agent.maf_backend import MAFBackend
from advisor_agent.run_context import current_run
from advisor_shared.messages import AdvisorRequest


@pytest.fixture
def azure_env(monkeypatch):
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

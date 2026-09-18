"""生产装配:渠道 adapter 只调用 build_advisor(),不接触内部组件。"""
from collections.abc import Callable
from contextlib import AsyncExitStack
import os
from pathlib import Path
from typing import Any

from azure.search.documents.aio import SearchClient
from openai import AsyncAzureOpenAI, Timeout
from openai.lib.azure import API_KEY_SENTINEL

from advisor_agent.backend import AgentBackend
from advisor_agent.core import AdvisorCore
from advisor_agent.diagnostics import NetworkDiagnostics
from advisor_agent.escalation import EscalationConfig
from advisor_agent.llm_diagnostics import DiagnosticAsyncHttpClient
from advisor_agent.maf_backend import MAFBackend
from advisor_agent.run_context import get_request_context
from advisor_agent.search.combined import (
    GITHUB_SEARCH_BUDGET_SECONDS, SEARCH_BUDGET_SECONDS, CombinedSearch,
)
from advisor_agent.search.github_live import GitHubLiveSearchClient
from advisor_agent.search.knowledge import KnowledgeSearchClient
from advisor_agent.search.web import (
    BraveProvider, TavilyProvider, WebSearchChain, validate_seconds,
)
from advisor_agent.sessions import InMemorySessionStore, SessionStore
from advisor_agent.tools import AdvisorTools
from advisor_agent.usage import CopilotUsageClient
from advisor_shared.azure_auth import AzureAuthSettings, AzureCredentials
from advisor_shared.events import AdvisorEvent

# openai SDK 的默认 connect 超时是 5s。实测本环境到 Azure OpenAI endpoint 的
# 成功连接耗时(13 次采样)中位数约 8s、最大 10.9s,只有 2 次落在 5s 内 ——
# 也就是说 bot 在连接本来能建立的时候就提前放弃了。15s 覆盖实测最大值并留余量。
# read/write/pool 保持 SDK 默认 600:那是 LLM 生成时间,与连接无关,不该动。
#
# 用 openai 重新导出的 Timeout 而不是 httpx.Timeout:openai 3.x 内部已经换成
# 打包的 httpx2。传 legacy httpx.Timeout 不会报错,但会被 httpx2.Timeout 当成
# 标量 timeout 吞掉 —— connect/read/write/pool 全被设成那个对象本身而不是 15.0,
# 连 repr(client._client.timeout) 都抛 TypeError: unhashable type: 'Timeout'。
# openai.Timeout 永远指向 SDK 当前实际使用的那个类型,不随内部换壳而失效。
_TIMEOUT = Timeout(connect=15.0, read=600, write=600, pool=600)

# 重试只保留 openai SDK 这一层:它区分可重试的状态码(429/5xx/连接错误)、
# 遵守 Retry-After、做指数退避。core.py 那层是 `except Exception` 盲重试,
# 且重跑的是整个 tool loop(会重复执行 search_solutions / web_search),
# 既贵又有重复副作用 —— 已收敛为不重试。
#
# max_retries 是"重试次数"不是"总尝试次数":openai/_base_client.py 的
# `for retries_taken in range(max_retries + 1)`,所以 1 → 总共 2 次尝试。
# 最坏路径 = core 1 次 × SDK 2 次尝试 × 15s connect = 30s。
_MAX_RETRIES = 1


class _ExplicitAzureOpenAI(AsyncAzureOpenAI):
    def __init__(self, **kwargs: Any):
        if kwargs.get("azure_ad_token_provider") is not None:
            # The SDK's sentinel satisfies its base constructor without emitting
            # an api-key header or inferring either Azure/OpenAI environment key.
            kwargs["api_key"] = API_KEY_SENTINEL
        super().__init__(**kwargs)
        # The SDK otherwise infers AZURE_OPENAI_AD_TOKEN ahead of BOTH explicit
        # key auth and our selected identity provider, including with_options().
        self._azure_ad_token = None


class _CredentialHttpClient(DiagnosticAsyncHttpClient):
    def __init__(self, owner: AzureCredentials | None):
        super().__init__(timeout=_TIMEOUT)
        self._credential_owner = owner

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            if self._credential_owner is not None:
                await self._credential_owner.aclose()


class AzureAdvisor(AdvisorCore):
    """An advisor with explicit shutdown of its factory-owned Azure resources."""

    def __init__(self, backend: AgentBackend, *, channel_name: str,
                 azure_clients: tuple[AsyncAzureOpenAI | SearchClient, ...],
                 credential_owner: AzureCredentials | None):
        super().__init__(backend, InMemorySessionStore(), channel_name=channel_name)
        self._azure_clients = azure_clients
        self._credential_owner = credential_owner
        self._azure_closed = False

    async def aclose(self) -> None:
        if self._azure_closed:
            return
        self._azure_closed = True
        async with AsyncExitStack() as stack:
            if self._credential_owner is not None:
                stack.push_async_callback(self._credential_owner.aclose)
            for client in self._azure_clients:
                stack.push_async_callback(client.close)


class AdvisorComponents:
    """Own heavy clients once; create independent, non-owning turn cores."""

    def __init__(self, owner: AzureAdvisor):
        self._owner = owner

    def new_core(
        self, sessions: SessionStore, *,
        event_sink: Callable[[AdvisorEvent], None] | None = None,
    ) -> AdvisorCore:
        if self._owner._azure_closed:
            raise RuntimeError("Advisor components are closed")
        return AdvisorCore(
            self._owner.backend, sessions,
            planner=self._owner.planner, evaluator=self._owner.evaluator,
            event_sink=self._owner.event_sink if event_sink is None else event_sink,
            channel_name=self._owner.channel_name)

    async def aclose(self) -> None:
        await self._owner.aclose()


def _endpoint(value: str | None, name: str) -> str:
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


def build_openai_client(
        api_version: str, *, credentials: AzureCredentials | None = None,
) -> AsyncAzureOpenAI:
    """Azure OpenAI client 的唯一出厂口 —— 超时/重试策略只在这里定义一次。

    embedding 与 chat 走同一个 endpoint、同一条坏网络,两边必须用同一套策略;
    历史上 chat client 藏在 MAFBackend 内部自建,漏掉了这里的加固。
    client.close() 同时释放自建身份;外部注入的 credentials 仍由调用方关闭。
    """
    owned = credentials is None
    credentials = credentials or AzureCredentials(AzureAuthSettings.from_env(os.environ))
    endpoint = _endpoint(credentials.settings.openai_endpoint, "AZURE_OPENAI_ENDPOINT")
    auth = credentials.openai_auth()
    return _ExplicitAzureOpenAI(
        azure_endpoint=endpoint,
        **auth,
        api_version=api_version,
        timeout=_TIMEOUT,
        max_retries=_MAX_RETRIES,
        http_client=_CredentialHttpClient(credentials if owned else None),
    )


def _channel_id_provider() -> str:
    return get_request_context().channel_id


def _is_group_provider() -> bool:
    return get_request_context().is_group


def build_advisor(
        channel_name: str = "generic", *, credentials: AzureCredentials | None = None,
) -> AzureAdvisor:
    """Share one identity; injected credentials remain owned by the caller."""
    kb_budget = validate_seconds(
        os.environ.get("KB_SEARCH_BUDGET_SECONDS", str(SEARCH_BUDGET_SECONDS)),
        "KB_SEARCH_BUDGET_SECONDS")
    live_budget = validate_seconds(
        os.environ.get("GITHUB_SEARCH_BUDGET_SECONDS", str(GITHUB_SEARCH_BUDGET_SECONDS)),
        "GITHUB_SEARCH_BUDGET_SECONDS")
    web_budget = validate_seconds(
        os.environ.get("WEB_SEARCH_BUDGET_SECONDS", "12"),
        "WEB_SEARCH_BUDGET_SECONDS")
    web_timeout = validate_seconds(
        os.environ.get("WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS", "6"),
        "WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS")
    embedding_deployment = os.environ.get(
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
    if not embedding_deployment.strip():
        raise ValueError("AZURE_OPENAI_EMBEDDING_DEPLOYMENT must be configured")
    owned = credentials is None
    credentials = credentials or AzureCredentials(AzureAuthSettings.from_env(os.environ))
    search_endpoint = _endpoint(credentials.settings.search_endpoint, "AZURE_SEARCH_ENDPOINT")
    search_auth = credentials.search_credential()
    # embedding 的 api_version 保持钉死,不跟随 AZURE_OPENAI_API_VERSION ——
    # 那个变量是给 chat 调 preview 版本用的,不该拖着 embeddings 一起漂。
    embed_client = build_openai_client("2024-10-21", credentials=credentials)
    search_client = SearchClient(
        search_endpoint,
        os.environ.get("AZURE_SEARCH_INDEX", "copilot-qa"),
        search_auth,
    )
    combined = CombinedSearch(
        KnowledgeSearchClient(search_client, embed_client, embed_model=embedding_deployment),
        GitHubLiveSearchClient(),
        budget_seconds=kb_budget,
        live_budget_seconds=live_budget,
    )
    providers = []
    if os.environ.get("TAVILY_API_KEY"):
        providers.append(TavilyProvider())
    if os.environ.get("BRAVE_API_KEY"):
        providers.append(BraveProvider())
    web = WebSearchChain(providers, timeout_seconds=web_timeout,
                         budget_seconds=web_budget)
    escalation = EscalationConfig.load(
        Path(os.environ.get("ESCALATION_CONFIG",
                            Path(__file__).parent.parent.parent
                            / "escalation.yaml")))
    diagnostics = NetworkDiagnostics(
        Path(os.environ.get("DIAGNOSTICS_CONFIG",
                            Path(__file__).parent.parent.parent
                            / "diagnostics.yaml")))
    usage = CopilotUsageClient()
    tools = AdvisorTools(combined, web, escalation, diagnostics, usage)
    # chat client 显式注入:真机冒烟的 ConnectTimeout 打的就是 chat completions
    # 端点(known-issues 5.1),它必须拿到加固后的超时,不能用 SDK 默认的 5s。
    chat_client = build_openai_client(
        os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        credentials=credentials)
    backend = MAFBackend(
        tools, _channel_id_provider, _is_group_provider, client=chat_client)
    return AzureAdvisor(
        backend, channel_name=channel_name,
        azure_clients=(embed_client, search_client, chat_client),
        credential_owner=credentials if owned else None)


def build_components(
    channel_name: str = "generic", *, credentials: AzureCredentials | None = None,
) -> AdvisorComponents:
    return AdvisorComponents(build_advisor(channel_name, credentials=credentials))

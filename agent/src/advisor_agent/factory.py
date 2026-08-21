"""生产装配:渠道 adapter 只调用 build_advisor(),不接触内部组件。"""
import os
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient
from openai import AsyncAzureOpenAI

from advisor_agent.core import AdvisorCore
from advisor_agent.escalation import EscalationConfig
from advisor_agent.maf_backend import MAFBackend
from advisor_agent.search.combined import CombinedSearch
from advisor_agent.search.github_live import GitHubLiveSearchClient
from advisor_agent.search.knowledge import KnowledgeSearchClient
from advisor_agent.search.web import BraveProvider, TavilyProvider, WebSearchChain
from advisor_agent.sessions import InMemorySessionStore
from advisor_agent.tools import AdvisorTools

_channel_id_holder: dict[str, str] = {"value": ""}


def _channel_id_provider() -> str:
    return _channel_id_holder["value"]


def set_current_channel_id(channel_id: str) -> None:
    """渠道 adapter 在每次 handle 前调用(单 worker 内串行时安全;
    多并发部署改为 contextvars,接口不变)。"""
    _channel_id_holder["value"] = channel_id


def build_advisor(channel_name: str = "generic") -> AdvisorCore:
    embed_client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-10-21",
    )
    search_client = SearchClient(
        os.environ["AZURE_SEARCH_ENDPOINT"],
        os.environ.get("AZURE_SEARCH_INDEX", "copilot-qa"),
        AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"]),
    )
    combined = CombinedSearch(
        KnowledgeSearchClient(search_client, embed_client),
        GitHubLiveSearchClient(),
    )
    providers = []
    if os.environ.get("TAVILY_API_KEY"):
        providers.append(TavilyProvider())
    if os.environ.get("BRAVE_API_KEY"):
        providers.append(BraveProvider())
    web = WebSearchChain(providers)
    escalation = EscalationConfig.load(
        Path(os.environ.get("ESCALATION_CONFIG",
                            Path(__file__).parent.parent.parent
                            / "escalation.yaml")))
    tools = AdvisorTools(combined, web, escalation)
    backend = MAFBackend(tools, _channel_id_provider)
    return AdvisorCore(backend, InMemorySessionStore(),
                       channel_name=channel_name)

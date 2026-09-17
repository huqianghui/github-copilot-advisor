import httpx
import pytest
import respx

from advisor_agent.search.models import SearchResult
from advisor_agent.search.web import (
    BraveProvider,
    TavilyProvider,
    WebSearchChain,
)


class StubProvider:
    def __init__(self, name, results=(), error=None):
        self.name = name
        self._results, self._error = list(results), error
        self.called = False
        self.queries = []

    async def search(self, query, top, *, client=None):
        self.called = True
        self.queries.append(query)
        if self._error:
            raise self._error
        return self._results


def r(title):
    return SearchResult(title=title, content="c",
                        url=f"https://docs.github.com/en/copilot/{title}",
                        origin="web", score=1.0)


async def test_first_provider_success_no_failover():
    a, b = StubProvider("a", [r("x")]), StubProvider("b", [r("y")])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert [x.title for x in results] == ["x"]
    assert failovers == 0 and b.called is False


async def test_failover_on_error_then_success():
    a = StubProvider("a", error=RuntimeError("quota"))
    b = StubProvider("b", [r("y")])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert [x.title for x in results] == ["y"]
    assert failovers == 1


async def test_empty_results_also_failover():
    a, b = StubProvider("a", []), StubProvider("b", [r("y")])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert [x.title for x in results] == ["y"] and failovers == 1


async def test_all_fail_returns_empty_and_count():
    a = StubProvider("a", error=RuntimeError("x"))
    b = StubProvider("b", [])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert results == [] and failovers == 2


async def test_default_search_targets_trusted_sources_and_filters_leaked_results():
    provider = StubProvider("a", [
        r("third party").model_copy(update={"url": "https://example.com/help"}),
        r("official"),
    ])
    results, failovers = await WebSearchChain([provider]).search(
        '"Invalid string length"')
    assert [item.title for item in results] == ["official"]
    assert failovers == 0
    query = provider.queries[0]
    assert "GitHub Copilot" in query and '"Invalid string length"' in query
    for site in ("docs.github.com/en/copilot", "github.blog/changelog",
                 "code.visualstudio.com/updates",
                 "github.com/microsoft/vscode/issues",
                 "github.com/orgs/community/discussions",
                 "github.com/orgs/githubcopilotfaq",
                 "github.com/githubcopilotfaq"):
        assert f"site:{site}" in query


async def test_untrusted_or_blank_content_does_not_stop_trusted_failover():
    a = StubProvider("a", [
        r("untrusted").model_copy(update={"url": "https://example.com/a"}),
        r("empty").model_copy(update={"content": "  "}),
    ])
    b = StubProvider("b", [r("answer")])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert [item.title for item in results] == ["answer"]
    assert failovers == 1


async def test_general_search_keeps_other_sources_but_ranks_trusted_first():
    provider = StubProvider("a", [
        r("other").model_copy(update={"url": "https://example.com/help"}),
        r("official"),
    ])
    results, failovers = await WebSearchChain([provider]).search(
        "Invalid string length", scope="general")
    assert [item.title for item in results] == ["official", "other"]
    assert failovers == 0
    assert "GitHub Copilot" in provider.queries[0]
    assert "site:" not in provider.queries[0]


@pytest.mark.parametrize("scope", ["invalid"])
async def test_invalid_scope_is_not_silently_treated_as_general(scope):
    provider = StubProvider("a")
    with pytest.raises(ValueError, match="scope"):
        await WebSearchChain([provider]).search("q", scope=scope)
    assert not provider.called


@pytest.mark.parametrize(("url", "title", "trusted"), [
    ("https://docs.github.com/en/copilot/how-tos", "Docs", True),
    ("https://docs.github.com/zh/copilot", "Docs", True),
    ("https://github.blog/changelog/2026-01-01-github-copilot-update/",
     "Release", True),
    ("https://github.blog/changelog/label/copilot/", "Updates", True),
    ("https://code.visualstudio.com/updates/v1_110", "Release", True),
    ("https://github.com/microsoft/vscode/issues/292795", "Issue", True),
    ("https://github.com/orgs/community/discussions/189180",
     "Copilot Invalid string length", True),
    ("https://github.com/orgs/community/discussions/189180",
     "Copilot\u804a\u5929\u62a5\u9519", True),
    ("https://github.com/orgs/community/discussions/categories/copilot-conversations",
     "Category", True),
    ("https://github.com/orgs/githubcopilotfaq/discussions/42", "FAQ", True),
    ("https://github.com/githubcopilotfaq/faq/discussions/42", "FAQ", True),
    ("https://example.com/docs.github.com/en/copilot", "Copilot", False),
    ("https://docs.github.com.example.com/en/copilot", "Copilot", False),
    ("https://docs.github.com/en/actions", "Copilot", False),
    ("https://github.com/microsoft/vscode/issues-elsewhere", "Copilot", False),
    ("https://github.com/orgs/community/discussions/123", "Actions runner", False),
    ("https://github.com/orgs/community/discussions/categories/actions",
     "Copilot", False),
    ("https://github.com/unrelated/project/issues/1", "Copilot", False),
    ("https://github.blog/changelog/2026-01-01-actions-update", "Actions", False),
    ("https://github.com/githubcopilotfaq-unrelated/faq", "Copilot", False),
    ("https://github.com/microsoft/vscode/issues/../pull/42", "Copilot", False),
    ("https://github.com/microsoft/vscode/issues/%2e%2e/pull/42", "Copilot", False),
    ("https://user:password@github.com/microsoft/vscode/issues/1", "Copilot", False),
    ("not a url", "Copilot", False),
])
async def test_trusted_results_require_matching_host_path_and_source(url, title,
                                                                   trusted):
    result = r(title).model_copy(update={"url": url})
    results, _ = await WebSearchChain([StubProvider("a", [result])]).search("q")
    assert bool(results) is trusted


@respx.mock
async def test_tavily_provider_parses_response():
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json={"results": [
            {"title": "Copilot 1.97 release", "content": "notes...",
             "url": "https://blog/x", "score": 0.9},
        ]}))
    results = await TavilyProvider(api_key="k").search("copilot update", top=3)
    assert results[0].origin == "web"
    assert results[0].title == "Copilot 1.97 release"


@respx.mock
async def test_brave_provider_parses_response():
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json={"web": {"results": [
            {"title": "t", "description": "d", "url": "https://b/x"},
        ]}}))
    results = await BraveProvider(api_key="k").search("q", top=3)
    assert results[0].url == "https://b/x"

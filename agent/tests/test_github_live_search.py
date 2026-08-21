import httpx
import pytest
import respx

from advisor_agent.search.github_live import (
    DEFAULT_LIVE_REPOS,
    GitHubLiveSearchClient,
)

API = "https://api.github.com"


def gh_item(number=1, title="Copilot slow"):
    return {
        "number": number, "title": title,
        "body": "Details about slowness " * 5,
        "html_url": f"https://github.com/microsoft/vscode/issues/{number}",
        "score": 12.3,
    }


@respx.mock
async def test_search_builds_repo_scoped_query():
    route = respx.get(f"{API}/search/issues").mock(
        return_value=httpx.Response(200, json={"items": [gh_item()]})
    )
    client = GitHubLiveSearchClient(token="t", repos=["a/b", "c/d"])
    results = await client.search("copilot timeout")
    q = route.calls[0].request.url.params["q"]
    assert "repo:a/b" in q and "repo:c/d" in q and "state:open" in q
    assert results[0].origin == "github-live"
    assert results[0].url.endswith("/issues/1")


@respx.mock
async def test_http_error_propagates():
    respx.get(f"{API}/search/issues").mock(
        return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await GitHubLiveSearchClient(token="t").search("q")


def test_default_repos_match_spec():
    assert "microsoft/copilot-intellij-feedback" in DEFAULT_LIVE_REPOS
    assert len(DEFAULT_LIVE_REPOS) == 5

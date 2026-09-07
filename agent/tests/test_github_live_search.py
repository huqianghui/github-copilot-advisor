import httpx
import pytest
import respx

from advisor_agent.search.github_live import (
    DEFAULT_LIVE_REPOS,
    GitHubLiveSearchClient,
)

API = "https://api.github.com"

# GitHub 的真实契约(实测拟合,见 github_live.py 注释)。测试里独立重算一遍,
# 不 import 实现的私有 helper —— 否则实现改错了测试会跟着一起错。
TERM_BUDGET = 256


def budget_cost(terms: str) -> int:
    """检索词占的预算:空格记 3 个字符,其余各记 1 个。"""
    return len(terms) + 2 * terms.count(" ")


def terms_of(q: str) -> str:
    """从发出的 query 里剥掉限定符,只留检索词。"""
    return " ".join(tok for tok in q.split() if ":" not in tok)


def gh_item(number=1, title="Copilot slow"):
    return {
        "number": number, "title": title,
        "body": "Details about slowness " * 5,
        "html_url": f"https://github.com/microsoft/vscode/issues/{number}",
        "score": 12.3,
    }


def mock_search():
    return respx.get(f"{API}/search/issues").mock(
        return_value=httpx.Response(200, json={"items": [gh_item()]})
    )


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


@respx.mock
async def test_query_includes_is_issue_qualifier():
    """回归:GitHub 现在强制 query 带 is:issue,否则**每一次调用**都 422
    ("Query must include 'is:issue' or 'is:pull-request'")。

    这条契约以前没人测,因为本文件所有用例都 mock 掉了 GitHub 的响应 ——
    mock 让我们对真实 API 的拒绝完全失明。
    """
    route = mock_search()
    await GitHubLiveSearchClient(token="t").search("copilot timeout")
    tokens = route.calls[0].request.url.params["q"].split()
    assert "is:issue" in tokens
    # spec 7.2:本工具查的是"还在讨论中的 issue",不查 PR,也不两个都带
    assert "is:pull-request" not in tokens


@respx.mock
async def test_short_query_is_not_truncated():
    route = mock_search()
    await GitHubLiveSearchClient(token="t").search("copilot chat 502 timeout")
    q = route.calls[0].request.url.params["q"]
    assert terms_of(q) == "copilot chat 502 timeout"


@respx.mock
async def test_long_query_truncated_within_budget_at_word_boundary():
    route = mock_search()
    words = [f"diagnose{i:03}" for i in range(30)]
    await GitHubLiveSearchClient(token="t").search(" ".join(words))

    sent = terms_of(route.calls[0].request.url.params["q"])
    kept = sent.split()
    assert budget_cost(sent) <= TERM_BUDGET, budget_cost(sent)
    assert 0 < len(kept) < len(words)          # 确实截了,但没截光
    # 只在词边界下刀:残留的每个 token 都必须是完整原词,不能是半个
    assert all(w in words for w in kept), sent
    assert kept == words[:len(kept)]           # 保序、从头保留


@respx.mock
async def test_qualifiers_do_not_count_against_term_budget():
    """限定符不吃检索词的预算 —— 用完整 5 个 repo 测(品种 4:只用 1 个 repo
    时"计入/不计入"两种实现可能给出同样的结果)。"""
    route = mock_search()
    terms = " ".join(f"diagnose{i:03}" for i in range(18))
    assert budget_cost(terms) <= TERM_BUDGET   # 检索词本身在预算内

    await GitHubLiveSearchClient(token="t",
                                 repos=DEFAULT_LIVE_REPOS).search(terms)
    q = route.calls[0].request.url.params["q"]
    assert terms_of(q) == terms                # 一个词都没被限定符挤掉
    assert len(q) > TERM_BUDGET                # 总长早超 256,但那不该管事


@respx.mock
async def test_term_budget_is_independent_of_repo_count():
    """加长限定符块不能挤掉检索词 —— "预算 = 总长 - 限定符" 的实现会在这里露馅。"""
    route = mock_search()
    terms = " ".join(f"diagnose{i:03}" for i in range(18))
    await GitHubLiveSearchClient(token="t", repos=["a/b"]).search(terms)
    await GitHubLiveSearchClient(token="t",
                                 repos=DEFAULT_LIVE_REPOS).search(terms)
    one, five = (terms_of(c.request.url.params["q"]) for c in route.calls)
    assert one == five == terms


@respx.mock
async def test_spaceless_long_query_keeps_search_terms():
    """中文报错整段不含空格:没有词边界可切,但也绝不能把检索词切成空 ——
    只剩限定符的 query 会把仓库里所有 open issue 当成"相关结果"捞回来。"""
    route = mock_search()
    cjk = "复制代理请求失败无法连接到服务器请检查网络设置并重试" * 20
    await GitHubLiveSearchClient(token="t").search(cjk)

    sent = terms_of(route.calls[0].request.url.params["q"])
    assert sent and cjk.startswith(sent)
    assert budget_cost(sent) <= TERM_BUDGET


@respx.mock
async def test_truncation_is_logged(caplog):
    """截断必须在日志里留痕,否则生产上看不出这件事发生过。"""
    mock_search()
    with caplog.at_level("WARNING", logger="advisor_agent.search.github_live"):
        await GitHubLiveSearchClient(token="t").search(
            " ".join(f"diagnose{i:03}" for i in range(30)))
    assert any("截断" in r.getMessage() for r in caplog.records), caplog.text

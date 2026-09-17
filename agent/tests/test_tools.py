import asyncio
import json
from pathlib import Path

import pytest

from advisor_agent.escalation import EscalationConfig
from advisor_agent.run_context import new_run
from advisor_agent.search.models import SearchResult
from advisor_agent.search.retrieval import WebRetrievalResult, WebScopeResult
from advisor_agent.search.web import WebSearchChain
from advisor_agent.tools import AdvisorTools
from test_web_search import StubProvider

ESCALATION_YAML = """
defaults:
  support_ticket_url: https://support.github.com/
  contacts:
    - role: CSA
      name: 默认CSA
      email: csa@example.com
channels:
  - channel_id: "19:abc"
    github_org: acme
    org_token_env: ORG_TOKEN_TEST
    contacts:
      - role: CSAM
        name: 李四
        email: l@x.com
        teams_user_id: "29:1a2b"
        in_channel: true
      - role: CSA
        name: 王五
        email: w@x.com
        in_channel: false
"""


def r(title, origin):
    return SearchResult(title=title, content="c", url=f"https://x/{title}",
                        origin=origin, score=2.0)


class StubCombined:
    def __init__(self, payload):
        self.payload = payload

    async def search_solutions(self, query, product_area=None):
        return self.payload


class StubWeb:
    def __init__(self, results, failovers):
        self._out = (results, failovers)

    async def retrieve(self, query, top=5):
        results, failovers = self._out
        status = "success" if results else "empty"
        return WebRetrievalResult(status, results, [
            WebScopeResult("general", status, results, failovers=failovers)])


class StubDiagnostics:
    def __init__(self, payload=None):
        self.payload = payload or {
            "probes": [], "github_status": {"indicator": "unknown",
                                             "incidents": []},
            "verdict": "partial", "self_test_commands": [],
            "allowlist_doc": "",
        }

    async def run(self, enterprise_slug=None):
        return self.payload


class StubUsage:
    def __init__(self, result=None):
        self.result = result or {}

    async def lookup(self, question_type, org, token, username=None):
        return self.result


class RaisingUsage:
    def __init__(self, exc):
        self.exc = exc

    async def lookup(self, question_type, org, token, username=None):
        raise self.exc


def make_tools(tmp_path, combined_payload=None, web=(list(), 0),
               diagnostics=None, usage_result=None, usage=None):
    p = tmp_path / "e.yaml"
    p.write_text(ESCALATION_YAML, encoding="utf-8")
    payload = combined_payload or {"no_results": True, "results": []}
    return AdvisorTools(StubCombined(payload), StubWeb(web[0], web[1]),
                        EscalationConfig.load(p),
                        diagnostics or StubDiagnostics(),
                        usage or StubUsage(usage_result))


async def test_search_solutions_sets_kb_hit_stage_and_citations(tmp_path):
    run = new_run()
    payload = {"no_results": False,
               "results": [r("a", "kb").model_dump(),
                           r("b", "github-live").model_dump()]}
    tools = make_tools(tmp_path, combined_payload=payload)
    out = json.loads(await tools.search_solutions("q"))
    assert out["no_results"] is False
    assert run.stage == "kb_hit"
    assert "search_solutions" in run.tool_latencies_ms
    assert {c["title"] for c in run.citations_seen} == {"a", "b"}


async def test_search_solutions_live_only_sets_live_hit(tmp_path):
    run = new_run()
    payload = {"no_results": False,
               "results": [r("b", "github-live").model_dump()]}
    tools = make_tools(tmp_path, combined_payload=payload)
    await tools.search_solutions("q")
    assert run.stage == "live_hit"


async def test_repeated_tool_calls_accumulate_and_keep_each_timing(
        tmp_path, monkeypatch):
    from advisor_shared import telemetry

    now = [0.0]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: now[0])

    class DelayedCombined:
        async def search_solutions(self, query, product_area=None):
            now[0] += 0.25
            return {"no_results": True, "results": []}

    run = new_run()
    tools = make_tools(tmp_path)
    tools._combined = DelayedCombined()
    with telemetry.trace_scope() as trace:
        await tools.search_solutions("first")
        await tools.search_solutions("second")
    assert run.tool_latencies_ms["search_solutions"] == 500
    assert [s.duration_ms for s in trace.timings
            if s.name == "tool.search_solutions"] == [250, 250]


async def test_failed_tool_call_still_records_latency(tmp_path):
    from advisor_shared.telemetry import trace_scope

    class FailingCombined:
        async def search_solutions(self, query, product_area=None):
            raise RuntimeError("private-error")

    run = new_run()
    tools = make_tools(tmp_path)
    tools._combined = FailingCombined()
    with trace_scope() as trace, pytest.raises(RuntimeError):
        await tools.search_solutions("q")
    assert "search_solutions" in run.tool_latencies_ms
    assert trace.timings[-1].name == "tool.search_solutions"
    assert trace.timings[-1].status == "error"


async def test_web_search_sets_stage_and_failover(tmp_path):
    run = new_run()
    tools = make_tools(tmp_path, web=([r("w", "web")], 2))
    out = json.loads(await tools.web_search("q"))
    assert out["results"][0]["origin"] == "web"
    assert run.stage == "web" and run.failover_count == 2


async def test_web_search_returns_scope_confidence_and_empty_status(tmp_path):
    new_run()
    tools = make_tools(tmp_path)
    tools._web = WebSearchChain([StubProvider("a", [
        SearchResult(title="Docs", content="answer",
                     url="https://docs.github.com/en/copilot",
                     origin="web", score=0),
    ])])
    out = json.loads(await tools.web_search("q"))
    assert out["status"] == "success"
    assert out["no_results"] is False
    assert out["results"][0]["source_confidence"] == "high"
    assert {s["scope"] for s in out["scopes"]} == {"trusted", "general"}
    assert "sufficient" not in out  # Source reputation is not answer sufficiency.


async def test_repeated_web_search_reuses_results_without_duplicate_side_effects(tmp_path):
    run = new_run()
    provider = StubProvider("a", [r("other", "web")])
    tools = make_tools(tmp_path)
    tools._web = WebSearchChain([provider])
    first = await tools.web_search("q")
    citations = list(run.citations_seen)
    failovers = run.failover_count
    second = await tools.web_search("changed query")
    assert first == second
    assert len(provider.queries) == 2
    assert run.citations_seen == citations
    assert len(citations) == 1
    assert run.failover_count == failovers
    assert run.web_search_started is True


@pytest.mark.parametrize("trusted_has_results", [True, False])
async def test_one_call_includes_general_even_when_trusted_has_results(
        tmp_path, trusted_has_results):
    from test_web_retrieval import Provider

    new_run()
    provider = Provider(trusted=[
        SearchResult(title="Docs", content="related but incomplete",
                     url="https://docs.github.com/en/copilot",
                     origin="web", score=0),
    ] if trusted_has_results else [], general=[r("other", "web")])
    tools = make_tools(tmp_path)
    tools._web = WebSearchChain([provider])
    out = json.loads(await tools.web_search("q"))
    assert out["status"] == "success"
    assert sorted(provider.calls) == ["general", "trusted"]
    assert any(r["title"] == "other" for r in out["results"])


async def test_web_search_allowance_resets_for_each_run(tmp_path):
    new_run()
    tools = make_tools(tmp_path)
    provider = StubProvider("a")
    tools._web = WebSearchChain([provider])
    await tools.web_search("q")
    new_run()
    await tools.web_search("q")
    assert len(provider.queries) == 4


@pytest.mark.parametrize(("error", "status", "no_results"), [
    (TimeoutError("private"), "timeout", None),
    (RuntimeError("private"), "error", None),
    (None, "empty", True),
])
async def test_web_failure_is_explicit_and_cached(tmp_path, error, status, no_results):
    new_run()
    provider = StubProvider("a", error=error)
    tools = make_tools(tmp_path)
    tools._web = WebSearchChain([provider])
    first = await tools.web_search("q")
    second = await tools.web_search("retry")
    assert first == second
    assert len(provider.queries) == 2
    payload = json.loads(first)
    assert payload["status"] == status
    assert payload["no_results"] is no_results
    assert payload["retry_allowed"] is False
    assert "private" not in first


async def test_concurrent_web_calls_share_one_logical_execution(tmp_path):
    from test_web_retrieval import Provider, result

    new_run()
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(Provider):
        async def search(self, query, top, *, client=None):
            entered.set()
            await release.wait()
            return await super().search(query, top, client=client)

    provider = Blocking(general=[result("answer")])
    tools = make_tools(tmp_path)
    tools._web = WebSearchChain([provider])
    async with asyncio.timeout(3):
        first = asyncio.create_task(tools.web_search("first"))
        await entered.wait()
        second = asyncio.create_task(tools.web_search("second"))
        release.set()
        assert await first == await second
    assert sorted(provider.calls) == ["general", "trusted"]
    assert provider.clients[0] is provider.clients[1]


async def test_cancelled_web_call_cannot_restart_in_same_turn(tmp_path):
    new_run()
    entered = asyncio.Event()
    calls = []

    class Waiting:
        name = "waiting"

        async def search(self, query, top, *, client=None):
            calls.append(query)
            entered.set()
            await asyncio.Event().wait()

    tools = make_tools(tmp_path)
    tools._web = WebSearchChain([Waiting()])
    async with asyncio.timeout(3):
        task = asyncio.create_task(tools.web_search("first"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        out = json.loads(await tools.web_search("retry"))
    assert out["status"] == "already_attempted"
    assert out["retry_allowed"] is False
    assert len(calls) == 2


async def test_escalate_adds_mention_only_for_in_channel(tmp_path):
    run = new_run()
    tools = make_tools(tmp_path)
    out = json.loads(await tools.escalate_to_human("19:abc", "用户仍未解决"))
    assert run.stage == "escalated"
    assert len(run.mentions) == 1
    assert run.mentions[0].platform_user_id == "29:1a2b"
    roles = {c["role"] for c in out["contacts"]}
    assert roles == {"CSAM", "CSA"}


async def test_escalate_unknown_channel_uses_defaults(tmp_path):
    run = new_run()
    tools = make_tools(tmp_path)
    out = json.loads(await tools.escalate_to_human("19:zzz", "reason"))
    assert run.mentions == []
    assert out["contacts"][0]["name"] == "默认CSA"


def test_web_search_docstring_states_precondition(tmp_path):
    tools = make_tools(tmp_path)
    assert "search_solutions" in tools.web_search.__doc__


async def test_network_diagnostics_records_latency_and_returns_payload(
        tmp_path):
    run = new_run()
    payload = {"verdict": "github_ok_check_egress", "probes": [],
               "github_status": {"indicator": "none", "incidents": []},
               "self_test_commands": ["curl ..."],
               "allowlist_doc": "https://x/allowlist"}
    tools = make_tools(tmp_path, diagnostics=StubDiagnostics(payload))
    out = json.loads(await tools.network_diagnostics("19:abc"))
    assert out["verdict"] == "github_ok_check_egress"
    assert "network_diagnostics" in run.tool_latencies_ms


async def test_usage_not_configured_returns_guidance(tmp_path):
    run = new_run()
    tools = make_tools(tmp_path)
    out = json.loads(await tools.copilot_usage_lookup(
        "19:zzz", False, "billing_mode", None))   # defaults 无 org 配置
    assert out["status"] == "not_configured"
    assert "PAT" in out["guidance"]
    # 权限项必须跟着端点走:AI credits 用量端点要 Administration read。
    # 指引里写 billing read 会让客户建一个在新端点上 403 的 token,
    # 而那个 403 在下面的 except 里被吞成一句含糊的"权限不足"。
    assert "Administration" in out["guidance"]
    assert "billing read" not in out["guidance"]
    # early return 也必须记账:没配置不等于没被调用。
    assert "copilot_usage_lookup" in run.tool_latencies_ms


async def test_usage_privacy_blocked_in_group(tmp_path, monkeypatch):
    monkeypatch.setenv("ORG_TOKEN_TEST", "tok")
    run = new_run()
    tools = make_tools(tmp_path)
    out = json.loads(await tools.copilot_usage_lookup(
        "19:abc", True, "user_usage", "alice"))   # 群聊查个人 → 拦截
    assert out["status"] == "privacy_blocked"
    # 隐私门禁这条最不能漏:它一旦不记账,遥测里就完全看不到"有人在群里问过
    # 个人用量、被挡下了",运营会误判成没人问过(spec 10.2)。
    assert "copilot_usage_lookup" in run.tool_latencies_ms


async def test_usage_ok_path_calls_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORG_TOKEN_TEST", "tok")
    run = new_run()
    tools = make_tools(tmp_path, usage_result={"plan_type": "business"})
    out = json.loads(await tools.copilot_usage_lookup(
        "19:abc", True, "billing_mode", None))    # 群聊查汇总 → 允许
    assert out["status"] == "ok"
    assert out["data"]["plan_type"] == "business"
    assert "copilot_usage_lookup" in run.tool_latencies_ms


async def test_usage_client_error_still_records_latency(tmp_path, monkeypatch):
    """下游异常被 except 吞成 status=error,记账仍要发生。"""
    monkeypatch.setenv("ORG_TOKEN_TEST", "tok")
    run = new_run()
    tools = make_tools(tmp_path, usage=RaisingUsage(RuntimeError("boom")))
    out = json.loads(await tools.copilot_usage_lookup(
        "19:abc", True, "billing_mode", None))
    assert out["status"] == "error"
    assert "copilot_usage_lookup" in run.tool_latencies_ms


async def test_usage_cancelled_mid_lookup_still_records_latency(
        tmp_path, monkeypatch):
    """CancelledError 是 BaseException,不被 except Exception 接住,会穿出去。

    真实场景:Teams 请求超时/用户断开时任务被取消。工具**已经被调用过**,
    遥测必须留下痕迹 —— 这条钉住记账走 finally 而不是 else。
    """
    monkeypatch.setenv("ORG_TOKEN_TEST", "tok")
    run = new_run()
    tools = make_tools(tmp_path, usage=RaisingUsage(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await tools.copilot_usage_lookup("19:abc", True, "billing_mode", None)
    assert "copilot_usage_lookup" in run.tool_latencies_ms

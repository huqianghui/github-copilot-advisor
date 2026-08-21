import json
from pathlib import Path

from advisor_agent.escalation import EscalationConfig
from advisor_agent.run_context import new_run
from advisor_agent.search.models import SearchResult
from advisor_agent.tools import AdvisorTools

ESCALATION_YAML = """
defaults:
  support_ticket_url: https://support.github.com/
  contacts:
    - role: CSA
      name: 默认CSA
      email: csa@example.com
channels:
  - channel_id: "19:abc"
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

    async def search(self, query, top=5):
        return self._out


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


def make_tools(tmp_path, combined_payload=None, web=(list(), 0),
               diagnostics=None):
    p = tmp_path / "e.yaml"
    p.write_text(ESCALATION_YAML, encoding="utf-8")
    payload = combined_payload or {"no_results": True, "results": []}
    return AdvisorTools(StubCombined(payload), StubWeb(web[0], web[1]),
                        EscalationConfig.load(p),
                        diagnostics or StubDiagnostics())


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


async def test_web_search_sets_stage_and_failover(tmp_path):
    run = new_run()
    tools = make_tools(tmp_path, web=([r("w", "web")], 2))
    out = json.loads(await tools.web_search("q"))
    assert out["results"][0]["origin"] == "web"
    assert run.stage == "web" and run.failover_count == 2


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

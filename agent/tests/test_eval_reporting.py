"""Exercise report persistence through real, offline pytest sessions."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
TESTS_ROOT = Path(__file__).parent

OFFLINE_ADVISOR = """
import pytest
from advisor_agent.core import AdvisorCore
from advisor_agent.run_context import current_run
from advisor_agent.sessions import InMemorySessionStore

class Backend:
    async def run(self, user_text, history, images=None):
        run = current_run.get()
        run.stage = "kb_hit"
        run.tool_latencies_ms["search_solutions"] = 12
        return f"Reply: {user_text}"

class Planner:
    async def plan(self, request):
        if request.text == "crash":
            raise RuntimeError("request failed")
        return request

@pytest.fixture(autouse=True)
def offline_advisor(monkeypatch, request):
    from advisor_agent import factory
    for key in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
                "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY"):
        monkeypatch.setenv(key, "offline-test")
    monkeypatch.setattr(
        factory, "build_advisor",
        lambda channel_name: AdvisorCore(
            Backend(), InMemorySessionStore(), planner=Planner(),
            channel_name=channel_name))
    case = request.node.callspec.params["case"]
    if case["id"] == "setup-error":
        raise RuntimeError("setup failed")
    yield
    if case["id"] == "teardown-error":
        raise RuntimeError("teardown failed")
"""


def prepare_eval(pytester: pytest.Pytester, cases: list[dict],
                 extra_conftest: str = OFFLINE_ADVISOR) -> None:
    pytester.makeini("[pytest]\nasyncio_mode = auto\n"
                     "markers = integration: real services\n")
    pytester.makeconftest(
        (TESTS_ROOT / "conftest.py").read_text(encoding="utf-8")
        + "\n" + extra_conftest)
    pytester.makepyfile(test_eval_behavior=(
        TESTS_ROOT / "test_eval_behavior.py").read_text(encoding="utf-8"))
    pytester.makefile(".yaml", eval_cases=json.dumps({"cases": cases}))


def read_report(pytester: pytest.Pytester) -> tuple[Path, dict]:
    paths = list((pytester.path / "output").glob("*.json"))
    assert len(paths) == 1, "Each evaluation session must persist one report"
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def test_report_keeps_turns_outcomes_and_errors_without_image_bytes(pytester):
    cases = [
        {"id": "multi", "multi_turn": ["first \u4e2d\u6587", "second"],
         "images": ["screenshot.png"], "expected_stage_in": ["kb_hit"]},
        {"id": "failed", "text": "answer", "expected_stage_in": ["kb_hit"],
         "expect_answer_contains_any": ["required-keyword"]},
        {"id": "skipped", "text": "image", "images": ["missing.png"],
         "expected_stage_in": ["kb_hit"]},
        {"id": "setup-error", "text": "setup", "expected_stage_in": ["kb_hit"]},
        {"id": "teardown-error", "text": "teardown",
         "expected_stage_in": ["kb_hit"]},
        {"id": "request-error", "text": "crash", "expected_stage_in": ["kb_hit"]},
    ]
    prepare_eval(pytester, cases)
    (pytester.path / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\nprivate-image")

    result = pytester.runpytest_subprocess(
        "-m", "integration", "test_eval_behavior.py", "-v")
    result.assert_outcomes(passed=2, failed=2, skipped=1, errors=2)
    path, report = read_report(pytester)

    assert re.fullmatch(r"\d{8}T\d{6}_\d{6}Z\.json", path.name)
    assert datetime.fromisoformat(report["finished_at"]) >= datetime.fromisoformat(
        report["started_at"])
    assert report["exit_code"] == 1
    assert report["summary"] == {
        "passed": 1, "failed": 2, "skipped": 1, "error": 2, "not_run": 0}
    entries = {case["id"]: case for case in report["cases"]}
    multi = entries["multi"]
    assert multi["outcome"] == "passed"
    assert multi["definition"]["expected_stage_in"] == ["kb_hit"]
    assert multi["duration_seconds"] >= 0
    first, second = multi["turns"]
    assert first["question"] == "first \u4e2d\u6587"
    assert first["response"]["markdown"] == "Reply: first \u4e2d\u6587"
    assert first["images"] == ["screenshot.png"]
    assert second["images"] == []
    assert second["question"] == "second"
    assert second["response"]["markdown"] == "Reply: second"
    assert len(first["events"]) == len(second["events"]) == 1
    assert first["events"][0]["stage"] == "kb_hit"
    assert first["events"][0]["tool_latencies_ms"] == {"search_solutions": 12}
    assert first["events"][0]["trace_id"] != second["events"][0]["trace_id"]
    first_timings = first["events"][0]["timings"]
    assert any(s["name"] == "agent.turn" for s in first_timings)
    assert all(s["span_id"] and s["duration_ms"] >= 0 for s in first_timings)
    assert first["duration_seconds"] >= 0
    raw = path.read_text(encoding="utf-8")
    assert "\u4e2d\u6587" in raw
    assert "private-image" not in raw and "iVBORw" not in raw
    assert entries["failed"]["turns"][0]["response"]["markdown"] == "Reply: answer"
    assert "required-keyword" in next(
        phase["details"] for phase in entries["failed"]["phases"]
        if phase["outcome"] == "failed")
    assert entries["skipped"]["turns"] == []
    assert "missing image fixture" in entries["skipped"]["phases"][1]["details"]
    assert "setup failed" in entries["setup-error"]["phases"][0]["details"]
    assert "teardown failed" in entries["teardown-error"]["phases"][-1]["details"]
    unfinished = entries["request-error"]["turns"][0]
    assert unfinished["question"] == "crash"
    assert unfinished["response"] is None
    assert unfinished["events"] == []
    result.stdout.fnmatch_lines(["*Eval report:*output*json*"])


def test_missing_environment_still_produces_a_skip_report(pytester):
    prepare_eval(pytester, [
        {"id": "no-env", "text": "hello", "expected_stage_in": ["kb_hit"]},
    ], extra_conftest="""
import pytest
@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
""")
    result = pytester.runpytest_subprocess("-m", "integration")
    result.assert_outcomes(skipped=1)
    _, report = read_report(pytester)
    assert report["exit_code"] == 0
    assert report["summary"]["skipped"] == 1
    case = report["cases"][0]
    assert case["id"] == "no-env" and case["turns"] == []
    assert "missing env" in case["phases"][0]["details"]


def test_repeated_filtered_runs_do_not_overwrite_reports(pytester):
    prepare_eval(pytester, [
        {"id": "selected", "text": "hello", "expected_stage_in": ["kb_hit"]},
        {"id": "excluded", "text": "no", "expected_stage_in": ["kb_hit"]},
    ])
    result = pytester.runpytest_subprocess("-m", "integration", "-k", "selected")
    result.assert_outcomes(passed=1, deselected=1)
    first_path, first = read_report(pytester)
    original_bytes = first_path.read_bytes()
    assert [case["id"] for case in first["cases"]] == ["selected"]

    result = pytester.runpytest_subprocess("-m", "integration", "-k", "selected")
    result.assert_outcomes(passed=1, deselected=1)
    assert len(list((pytester.path / "output").glob("*.json"))) == 2
    assert first_path.read_bytes() == original_bytes


def test_fail_fast_preserves_unrun_cases(pytester):
    prepare_eval(pytester, [
        {"id": "bad", "text": "answer", "expected_stage_in": ["escalated"]},
        {"id": "later", "text": "hello", "expected_stage_in": ["kb_hit"]},
    ])
    result = pytester.runpytest_subprocess("-m", "integration", "-x")
    result.assert_outcomes(failed=1)
    _, report = read_report(pytester)
    assert report["summary"]["failed"] == 1
    assert report["summary"]["not_run"] == 1
    assert report["cases"][1]["outcome"] == "not_run"
    assert report["cases"][1]["turns"] == []


def test_collection_errors_are_persisted(pytester):
    prepare_eval(pytester, [], extra_conftest="")
    pytester.makepyfile(test_eval_behavior="raise ValueError('invalid cases')")
    result = pytester.runpytest_subprocess("-m", "integration")
    result.assert_outcomes(errors=1)
    _, report = read_report(pytester)
    assert report["exit_code"] == 2
    assert report["cases"] == []
    assert "invalid cases" in report["collection_errors"][0]["details"]


def test_unrelated_tests_and_deselected_eval_do_not_create_reports(pytester):
    prepare_eval(pytester, [
        {"id": "excluded", "text": "hello", "expected_stage_in": ["kb_hit"]},
    ], extra_conftest="")
    pytester.makepyfile(test_other="def test_other(): pass")
    result = pytester.runpytest_subprocess("-m", "not integration")
    result.assert_outcomes(passed=1, deselected=1)
    assert not (pytester.path / "output").exists()


def test_report_persists_search_attempts_separately_for_each_turn(pytester):
    prepare_eval(pytester, [
        {"id": "searches", "multi_turn": ["first", "second"],
         "expected_stage_in": ["web"]},
    ], extra_conftest=OFFLINE_ADVISOR + """
import httpx
from advisor_agent.search.combined import CombinedSearch
from advisor_agent.search.models import SearchResult
from advisor_agent.search.web import WebSearchChain

class EmptySearch:
    async def search(self, query, **kwargs):
        return []

class WebProvider:
    name = "brave"

    async def search(self, query, top, *, client=None):
        return [SearchResult(title="found", content="answer",
                             url="https://docs.github.com/en/copilot/help",
                             origin="web", score=1)]

class FailingProvider:
    name = "tavily"

    async def search(self, query, top, *, client=None):
        raise httpx.ConnectTimeout("private-connection-detail")

async def search_backend(self, user_text, history, images=None):
    await CombinedSearch(EmptySearch(), EmptySearch()).search_solutions(user_text)
    await WebSearchChain([FailingProvider(), WebProvider()]).retrieve(user_text)
    current_run.get().stage = "web"
    return f"Reply: {user_text}"

Backend.run = search_backend
""")
    result = pytester.runpytest_subprocess("-m", "integration")
    result.assert_outcomes(passed=1)
    path, report = read_report(pytester)
    first, second = report["cases"][0]["turns"]
    for turn in (first, second):
        attempts = turn["events"][0].get("search_attempts", [])
        assert len(attempts) == 6
        assert turn["events"][0]["error"] is None
        web_attempts = [a for a in attempts if a["source"] == "web"]
        assert {(a["scope"], a["provider"], a["status"]) for a in web_attempts} == {
            ("trusted", "tavily", "timeout"), ("general", "tavily", "timeout"),
            ("trusted", "brave", "success"), ("general", "brave", "success")}
        by_provider = {attempt["provider"]: attempt for attempt in attempts}
        assert by_provider["azure_ai_search"]["status"] == "empty"
        assert by_provider["azure_ai_search"]["result_count"] == 0
        assert by_provider["github"]["status"] == "empty"
        assert by_provider["tavily"]["status"] == "timeout"
        assert by_provider["brave"]["status"] == "success"
        assert by_provider["brave"]["result_count"] == 1
    assert "private-connection-detail" not in path.read_text(encoding="utf-8")

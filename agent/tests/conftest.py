"""Persist behavior evaluation results without affecting other test suites."""
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest


@dataclass
class EvalCaseResult:
    id: str
    nodeid: str
    definition: dict
    outcome: str = "not_run"
    duration_seconds: float = 0.0
    turns: list[dict] = field(default_factory=list)
    phases: list[dict] = field(default_factory=list)


class EvalReporter:
    def __init__(self):
        self.started_at = datetime.now(timezone.utc)
        self.cases: dict[str, EvalCaseResult] = {}
        self.collection_errors: list[dict] = []
        self.output_path: Path | None = None

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        for item in session.items:
            if item.path != Path(__file__).with_name("test_eval_behavior.py"):
                continue
            definition = item.callspec.params["case"]
            self.cases[item.nodeid] = EvalCaseResult(
                id=definition["id"], nodeid=item.nodeid, definition=definition)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if (report.failed
                and Path(report.nodeid).name == "test_eval_behavior.py"):
            self.collection_errors.append({
                "nodeid": report.nodeid, "details": report.longreprtext})

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item):
        report = yield
        if item.nodeid in self.cases:
            case = self.cases[item.nodeid]
            case.duration_seconds += report.duration
            case.phases.append({
                "phase": report.when,
                "outcome": report.outcome,
                "duration_seconds": report.duration,
                "details": report.longreprtext if report.longrepr else None,
            })
            if report.failed:
                case.outcome = "failed" if report.when == "call" else "error"
            elif case.outcome not in ("failed", "error"):
                if report.skipped:
                    case.outcome = "skipped"
                elif report.when == "call":
                    case.outcome = "passed"
        return report

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, exitstatus: int) -> None:
        if not self.cases and not self.collection_errors:
            return
        summary = dict.fromkeys(
            ("passed", "failed", "skipped", "error", "not_run"), 0)
        for case in self.cases.values():
            summary[case.outcome] += 1
        data = {
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": int(exitstatus),
            "summary": summary,
            "collection_errors": self.collection_errors,
            "cases": [asdict(case) for case in self.cases.values()],
        }
        output = Path(__file__).parent / "output"
        output.mkdir(exist_ok=True)
        self.output_path = output / self.started_at.strftime(
            "%Y%m%dT%H%M%S_%fZ.json")
        with self.output_path.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.output_path is not None:
            terminalreporter.write_sep("-", f"Eval report: {self.output_path}")


_REPORTER = pytest.StashKey[EvalReporter]()


def pytest_configure(config: pytest.Config) -> None:
    reporter = EvalReporter()
    config.stash[_REPORTER] = reporter
    config.pluginmanager.register(reporter, "advisor-eval-reporter")


@pytest.fixture
def eval_turns(request: pytest.FixtureRequest) -> list[dict]:
    return request.config.stash[_REPORTER].cases[request.node.nodeid].turns

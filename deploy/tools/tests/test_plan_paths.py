import json
import os

import pytest

from advisor_deploy.remote_acr import _owned_path, evidence_path
from advisor_deploy.render import load_plan
from test_deploy_workflows import workflow_case


def test_relative_plan_paths_normalize_before_cloud_evidence_scope_checks(workflow_case):
    case = workflow_case
    _, original, _ = case.plans["aca"]
    data = dict(original)
    path = case.out / "plans" / "aca.json"
    path.parent.mkdir()
    for name in ("env_file", "artifact", "manifest"):
        data[name] = os.path.relpath(data[name], path.parent)
    data["overrides"] = {"ACA_BUILD_MODE": "acr"}
    path.write_text(json.dumps(data), encoding="utf-8")
    plan = load_plan(path, case.repo, "aca")
    assert ".." not in plan.artifact_path.parts
    assert ".." not in plan.manifest_path.parts
    assert plan.manifest_path == case.out / "aca-ownership.json"
    assert _owned_path(evidence_path(plan.manifest_path), case.repo)


def test_manifest_escape_is_rejected_after_normalizing_relative_components(workflow_case):
    case = workflow_case
    _, original, _ = case.plans["aca"]
    data = dict(original)
    path = case.out / "plans" / "escape.json"
    path.parent.mkdir()
    data["manifest"] = "../../../outside.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_output_scope"):
        load_plan(path, case.repo, "aca")


@pytest.mark.parametrize("escape", [False, True])
def test_observer_plan_normalizes_before_ledger_reads_or_scope_checks(tmp_path, monkeypatch, escape):
    import advisor_deploy.benchmark_deploy as observer

    repo = tmp_path / "repo"
    path = repo / "deploy" / "out" / "plans" / "observer.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "format": "advisor-bench-deploy-v1",
        "run_id": "11111111-2222-4333-8444-555555555555",
        "environment_name": "experiment",
        "aca_manifest": "../../../outside.json" if escape else "../aca.json",
        "functions_manifest": "../functions.json", "api_manifest": "../api.json",
        "state_file": "../observer.json", "build_mode": "acr",
    }), encoding="utf-8")

    class LedgerReached(Exception):
        pass

    def read_ledger(candidate):
        assert candidate == repo / "deploy" / "out" / "aca.json"
        raise LedgerReached()

    monkeypatch.setattr(observer, "load_manifest", read_ledger)
    if escape:
        with pytest.raises(ValueError, match="benchmark_plan_path_scope"):
            observer._prepare(path, repo, None)
    else:
        with pytest.raises(LedgerReached):
            observer._prepare(path, repo, None)

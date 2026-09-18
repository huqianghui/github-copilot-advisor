"""Explicit empty Flex bootstrap never substitutes for regional quota admission."""

import json
from pathlib import Path

import pytest

from test_deploy_workflows import workflow_case
from test_flex_capacity import RuntimeMetadata, capacity_plan, python_flex_record


def test_empty_bootstrap_records_unknown_quota_and_never_publishes_or_probes_model(workflow_case):
    case = workflow_case
    case.reset("functions")
    path, data, _ = case.plans["functions"]
    del data["capacity_evidence"]
    path.write_text(json.dumps(data))
    result, rejected = case.invoke("functions/Deploy.ps1", "-ParametersFile", path)
    assert result.returncode != 0 and rejected["error"]["code"] == "flex_capacity_evidence_unconfirmed"
    case.log_path.unlink()
    result, preview = case.invoke("functions/Deploy.ps1", "-ParametersFile", path, "-BootstrapOnly")
    assert result.returncode == 0, result.stdout + result.stderr
    assert preview["phase"] == "bootstrap_only"
    assert preview["regional_capacity"] == "unknown_deferred"
    assert preview["planned_stages"] == ["empty-infrastructure"]
    result, report = case.invoke("functions/Deploy.ps1", "-ParametersFile", path, "-BootstrapOnly",
                                "-Mode", "Apply", "-Confirmation", preview["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    ledger = json.loads(Path(data["manifest"]).read_text())
    assert report["status"] == ledger["status"] == "infrastructure_ready"
    assert report["app_id"] == ledger["outputs"]["app_id"]
    record = json.loads(Path(data["manifest"]).with_suffix(".bootstrap.json").read_text())
    assert record["regional_capacity"] == "unknown_deferred"
    assert record["code_publication"] == record["dependency_data_plane"] == "not_run"
    assert record["app_id"] == report["app_id"]
    assert ledger["application_validation"] == "not_run"
    assert ledger["discovery_complete"] and not ledger["pending_role_assignment_ids"]
    calls = [item["arguments"] for item in case.calls()]
    assert any(args[:3] == ["deployment", "sub", "validate"] for args in calls)
    assert any(args[:3] == ["deployment", "sub", "what-if"] for args in calls)
    assert any(args[:2] == ["functionapp", "list-flexconsumption-runtimes"] for args in calls)
    assert not any("config-zip" in args for args in calls)
    assert not any(".openai.azure.com" in arg or ".search.windows.net" in arg or "/bench/" in arg
                   for args in calls for arg in args)
    result, rejected = case.invoke("functions/Deploy.ps1", "-ParametersFile", path,
                                   "-Mode", "Apply", "-Confirmation", preview["confirmation"])
    assert result.returncode != 0 and rejected["error"]["code"] == "flex_capacity_evidence_unconfirmed"


def test_bootstrap_approval_cannot_authorize_full_publish_even_with_confirmed_quota(workflow_case):
    case = workflow_case
    case.reset("functions")
    path, _, capacity_hash = case.plans["functions"]
    result, bootstrap = case.invoke("functions/Deploy.ps1", "-ParametersFile", path, "-BootstrapOnly")
    assert result.returncode == 0, result.stdout + result.stderr
    result, full = case.invoke("functions/Deploy.ps1", "-ParametersFile", path, "-CapacityEvidenceHash", capacity_hash)
    assert result.returncode == 0, result.stdout + result.stderr
    assert full["phase"] == "full" and full["confirmation"] != bootstrap["confirmation"]
    result, rejected = case.invoke("functions/Deploy.ps1", "-ParametersFile", path,
                                   "-CapacityEvidenceHash", capacity_hash, "-Mode", "Apply",
                                   "-Confirmation", bootstrap["confirmation"])
    assert result.returncode != 0 and rejected["error"]["code"] == "apply_confirmation_required"
    assert not any("create" in call["arguments"] or "config-zip" in call["arguments"] for call in case.calls())


@pytest.mark.parametrize("guard", ["memory", "permissions"])
def test_bootstrap_keeps_smallest_profile_and_arm_permission_gates(workflow_case, guard):
    case = workflow_case
    case.reset("functions", permissions_denied=guard == "permissions")
    path, data, _ = case.plans["functions"]
    if guard == "memory":
        data["overrides"] = {"FUNCTIONS_INSTANCE_MEMORY_MB": "2048"}
        path.write_text(json.dumps(data))
    result, report = case.invoke("functions/Deploy.ps1", "-ParametersFile", path, "-BootstrapOnly")
    assert result.returncode != 0
    expected = "flex_bootstrap_requires_empty_512mb_profile" if guard == "memory" else "arm_permissions_unverified"
    assert report["error"]["code"] == expected
    assert not any("create" in call["arguments"] for call in case.calls())


def test_bootstrap_defers_only_capacity_evidence_not_runtime_support():
    from advisor_deploy.operations import require_capacity
    plan, _ = capacity_plan(0)
    plan.capacity_evidence = None
    require_capacity(plan, RuntimeMetadata(), None, bootstrap_only=True)
    row = python_flex_record("3.12")
    row["sku"]["instanceMemoryMB"] = [{"size": "2048", "isDefault": True}]
    with pytest.raises(ValueError, match="flex_runtime_memory_unsupported"):
        require_capacity(plan, RuntimeMetadata([row]), None, bootstrap_only=True)

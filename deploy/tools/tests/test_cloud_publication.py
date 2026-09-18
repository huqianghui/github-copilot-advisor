"""Executable deployment callers for the explicit ACR build strategy."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_deploy_workflows import workflow_case
from test_remote_acr import RemoteCli, context_case, PUBLISHED


def test_aca_cloud_apply_bootstraps_without_docker_and_records_remote_proof(workflow_case):
    case = workflow_case
    path, data, _ = case.plans["aca"]
    data["overrides"] = {"ACA_BUILD_MODE": "acr"}
    path.write_text(json.dumps(data))
    case.reset(docker_down=True)
    result, preview = case.invoke("aca/Deploy.ps1", "-ParametersFile", path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(call["arguments"][:2] == ["acr", "run"] for call in case.calls())
    result, report = case.invoke("aca/Deploy.ps1", "-ParametersFile", path,
                                "-Mode", "Apply", "-Confirmation", preview["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["status"] == "deployed"
    assert not any(call["tool"] == "docker" for call in case.calls())
    ledger = json.loads(Path(data["manifest"]).read_text())
    proof = json.loads(Path(data["manifest"]).with_suffix(".acr.json").read_text())
    assert proof["image_reference"] == ledger["image_reference"]
    assert proof["binding"]["artifact_sha256"] == ledger["artifact_digest"]
    assert proof["linux_imports_verified"] is True
    assert proof["run_id"] and proof["base_run_id"]


@pytest.mark.parametrize("changed_source", [False, True])
def test_cloud_observer_publish_accepts_unbuilt_context_and_records_exact_run(tmp_path, monkeypatch, changed_source):
    from advisor_deploy import benchmark_deploy as deployment
    from advisor_deploy.manifest import group_id, new_manifest
    case = context_case(tmp_path, "bench")
    template = tmp_path / "deploy" / "bench" / "infra" / "main.bicep"
    template.parent.mkdir(parents=True)
    template.write_text("targetScope = 'resourceGroup'\n")
    principal = str(uuid4())
    ledger = new_manifest("aca", str(uuid4()), "fixture", "a" * 64, "b" * 64)
    context = {"files": case.files, "image_id": None, "base_image_digest": None, "linux_imports_verified": False,
               "lock_sha256": "a" * 64, "source_sha256": {"uv.lock": "a" * 64}}
    run = SimpleNamespace(model_dump=lambda: {"approved": "plan"})
    graph = {"client_id": str(uuid4()), "service_principal_id": str(uuid4()), "role_id": str(uuid4()),
             "grants": [{"id": str(i), "principal_id": p} for i, p in enumerate([principal, str(uuid4()), str(uuid4())])]}
    selected = {"registry": {"id": group_id("aca") + "/providers/Microsoft.ContainerRegistry/registries/owned",
                             "name": "owned", "properties": {"loginServer": "owned.azurecr.io"}},
                "environment": {"id": "owned-environment"}, "storage": {"id": "owned-storage"}}
    state = {"phase": "bootstrapped", "identity_principal_id": principal, "image_reference": ""}
    paths = {"context": case.context, "state_file": case.evidence.with_name("observer.json")}
    monkeypatch.setattr(deployment, "_prepare", lambda *args: (
        {"environment_name": "fixture", "build_mode": "acr"}, paths, ledger, {}, {"graph": graph}, state, selected))
    context_checks = []

    def verify_context(*args, **kwargs):
        context_checks.append(True)
        if changed_source and len(context_checks) == 3:
            raise ValueError("benchmark_context_source_or_lock_mismatch")
        return context, run

    monkeypatch.setattr(deployment, "_verify_context", verify_context)

    class Commands(RemoteCli):
        def rest(self, *args, **kwargs):
            if args[2] == "arm_permissions":
                return {"value": [{"actions": ["*"], "notActions": []}]}
            return {"value": [{"id": g["id"], "principalId": g["principal_id"],
                               "resourceId": graph["service_principal_id"], "appRoleId": graph["role_id"]}
                              for g in graph["grants"]]}

    cli = Commands(tmp_path, mode="WhatIf")
    private = SimpleNamespace(directory=tmp_path / "never-upload-private")
    preview = deployment.deploy_benchmark(tmp_path / "plan.json", tmp_path, cli, private, "Publish", None)
    assert not cli.calls
    cli.mode = "Apply"
    if changed_source:
        with pytest.raises(ValueError, match="source_or_lock"):
            deployment.deploy_benchmark(tmp_path / "plan.json", tmp_path, cli, private, "Publish", preview["confirmation"])
        assert not paths["state_file"].exists()
        return
    result = deployment.deploy_benchmark(tmp_path / "plan.json", tmp_path, cli, private, "Publish", preview["confirmation"])
    assert result["image_reference"] == "owned.azurecr.io/advisor-bench@" + PUBLISHED
    assert state["image_reference"] == result["image_reference"]
    assert state["image_id"] == "sha256:" + "c" * 64
    proof = json.loads(paths["state_file"].with_suffix(".acr.json").read_text())
    assert proof["binding"]["context_sha256"] == state["context_digest"]
    assert context["linux_imports_verified"] is False, "do not forge local-image verification metadata"


@pytest.mark.parametrize("proof_missing", [False, True])
def test_cloud_control_requires_remote_proof_not_a_local_image_flag(tmp_path, monkeypatch, proof_missing):
    from advisor_deploy import benchmark_deploy as deployment
    from advisor_deploy.manifest import group_id, new_manifest
    from advisor_deploy.remote_acr import publish_image
    from advisor_deploy.render import digest

    case = context_case(tmp_path, "bench")
    context = {"files": case.files, "image_id": None, "linux_imports_verified": False,
               "base_image_digest": None, "source_sha256": {"uv.lock": "a" * 64}, "lock_sha256": "a" * 64}
    run = SimpleNamespace(model_dump=lambda: {"profile": "cloud-smoke"})
    binding = {"context_sha256": digest(context), "run_plan_sha256": digest(run.model_dump()),
               "source_sha256": digest(context["source_sha256"]), "lock_sha256": context["lock_sha256"]}
    paths = {"context": case.context, "state_file": case.evidence.with_name("observer.json")}
    proof_path = paths["state_file"].with_suffix(".acr.json")
    ledger = new_manifest("aca", str(uuid4()), "fixture", "a" * 64, "b" * 64)

    class Commands(RemoteCli):
        def rest(self, *args, **kwargs):
            return {"value": [{"actions": ["*"], "notActions": []}]}

    cli = Commands(tmp_path)
    proof = publish_image(cli, context=case.context, files=case.files, binding=binding,
                          registry="owned", kind="bench", evidence_path=proof_path)
    if proof_missing:
        proof_path.unlink()
    state = {
        "phase": "app_deployed", "context_digest": digest(context), "run_plan_digest": digest(run.model_dump()),
        "image_id": proof["image_id"], "image_reference": proof["image_reference"],
        "planned_app_id": group_id("aca") + "/providers/Microsoft.App/containerApps/ca-bench-fixture",
        "planned_app_name": "ca-bench-fixture", "identity_client_id": str(uuid4()),
    }
    selected = {"registry": {"name": "owned"}}
    monkeypatch.setattr(deployment, "_prepare", lambda *args: (
        {"build_mode": "acr"}, paths, ledger, {}, {}, state, selected))

    def source_check(_path, _repo, _state, _aca, _functions, _api, require_linux=True):
        if require_linux:
            raise ValueError("benchmark_linux_image_verification_required")
        return context, run

    monkeypatch.setattr(deployment, "_verify_context", source_check)
    monkeypatch.setattr(deployment, "verify_benchmark_app", lambda *args: None)
    cli.mode = "WhatIf"
    if proof_missing:
        with pytest.raises(ValueError, match="unreadable_json"):
            deployment.control_benchmark(tmp_path / "plan.json", tmp_path, cli, "start", None)
    else:
        result = deployment.control_benchmark(tmp_path / "plan.json", tmp_path, cli, "start", None)
        assert result["ok"] and result["control_invoked"] is False
        assert context["linux_imports_verified"] is False

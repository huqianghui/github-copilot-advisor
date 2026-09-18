"""Execute the real PowerShell entrypoints against isolated fake Azure/Docker CLIs."""

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from uuid import uuid4
import zipfile

import pytest

from advisor_deploy.package import PackageManifest
from advisor_deploy.render import digest


ROOT = Path(__file__).resolve().parents[3]
SUB = "18827cdf-a4e8-4316-a7ba-a4499139a3ec"


@pytest.fixture
def workflow_case(tmp_path):
    shell = shutil.which("pwsh")
    if not shell or not shutil.which("uv"):
        pytest.skip("PowerShell and uv are required for executable deployment contracts")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers=["shared","agent","channels/teams"]\n'
        '[[tool.uv.index]]\nname="corp"\nurl="https://packagefeedproxy.microsoft.io/pypi/simple/"\ndefault=true\n',
        encoding="utf-8",
    )
    wheels = {}
    for distribution, member, module in (
        ("advisor-shared", "shared", "advisor_shared"), ("advisor-agent", "agent", "advisor_agent"),
        ("advisor-teams", "channels/teams", "teams_adapter"),
    ):
        directory = repo / member
        source = directory / "src" / module
        source.mkdir(parents=True)
        project = f'[project]\nname="{distribution}"\nversion="0.1.0"\nrequires-python=">=3.11"\ndependencies=[]\n'
        metadata = f"Metadata-Version: 2.3\nName: {distribution}\nVersion: 0.1.0\nRequires-Python: >=3.11\n"
        if distribution == "advisor-teams":
            project += '[project.optional-dependencies]\nfunctions=["azure-functions>=1.21,<2"]\n'
            metadata += "Provides-Extra: functions\nRequires-Dist: azure-functions>=1.21,<2; extra == 'functions'\n"
        (directory / "pyproject.toml").write_text(project, encoding="utf-8")
        for name in ("__init__.py", "__main__.py"):
            (source / name).write_text('"""Isolated contract fixture, never a deployment runtime."""\n', encoding="utf-8")
        stem = distribution.replace("-", "_") + "-0.1.0"
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name in ("__init__.py", "__main__.py"):
                archive.writestr(module + "/" + name, (source / name).read_bytes())
            archive.writestr(stem + ".dist-info/METADATA", metadata)
            archive.writestr(stem + ".dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(stem + ".dist-info/RECORD", "")
        wheels["wheels/" + stem + "-py3-none-any.whl"] = buffer.getvalue()
    environment = {name: value for name, value in os.environ.items() if not name.startswith(("UV_INDEX", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX"))}
    cache = ROOT / "deploy" / "out" / ".uv-cache"
    locked = 'version=1\nrevision=3\nrequires-python=">=3.11"\n[manifest]\nmembers=["advisor-agent","advisor-shared","advisor-teams"]\n'
    for name, member in (("advisor-agent", "agent"), ("advisor-shared", "shared"), ("advisor-teams", "channels/teams")):
        locked += f'\n[[package]]\nname="{name}"\nversion="0.1.0"\nsource={{virtual="{member}"}}\n'
        if name == "advisor-teams":
            locked += (
                '[package.optional-dependencies]\nfunctions=[{name="azure-functions"}]\n'
                '[package.metadata]\nrequires-dist=[{name="azure-functions",marker="extra == \'functions\'",specifier=">=1.21,<2"}]\n'
                'provides-extras=["functions"]\n'
            )
    for block in (ROOT / "uv.lock").read_text(encoding="utf-8").split("[[package]]\n")[1:]:
        if tomllib.loads("[[package]]\n" + block)["package"][0]["name"] in {"azure-functions", "werkzeug", "markupsafe"}:
            locked += "\n[[package]]\n" + block
    (repo / "uv.lock").write_text(locked, encoding="utf-8")
    result = subprocess.run(["uv", "lock", "--check", "--offline", "--cache-dir", str(cache)], cwd=repo, env=environment,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    scripts = [
        "shared/Azure.ps1", "shared/Preflight.ps1", "shared/Ensure-BenchmarkIdentity.ps1",
        "aca/Deploy.ps1", "aca/Set-Mode.ps1", "functions/Deploy.ps1", "Cleanup.ps1",
    ]
    for relative in scripts:
        destination = repo / "deploy" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "deploy" / relative, destination)
    for target, names in (("aca", ("Dockerfile", "Dockerfile.dockerignore")), ("functions", ("function_app.py", "host.json"))):
        for name in names:
            shutil.copyfile(ROOT / "deploy" / target / name, repo / "deploy" / target / name)
        (repo / "deploy" / target / "infra").mkdir()
        shutil.copyfile(ROOT / "deploy" / target / "infra" / "main.bicep", repo / "deploy" / target / "infra" / "main.bicep")
    out = repo / "deploy" / "out"
    out.mkdir()
    approved = repo / "approved"
    approved.mkdir()
    for name, content in (("escalation.yaml", "contacts: []\n"), ("diagnostics.yaml", "checks: []\n")):
        (approved / name).write_text(content, encoding="utf-8")
    dependency_scope = f"/subscriptions/{SUB}/resourceGroups/rg-dependencies/providers/"
    env_file = repo / "fixture.env"
    env_file.write_text("\n".join([
        "GITHUB_TOKEN=fixture-secret",
        f"AZURE_OPENAI_RESOURCE_ID={dependency_scope}Microsoft.CognitiveServices/accounts/model",
        f"AZURE_SEARCH_RESOURCE_ID={dependency_scope}Microsoft.Search/searchServices/search",
        "AZURE_OPENAI_ENDPOINT=https://model.openai.azure.com",
        "AZURE_SEARCH_ENDPOINT=https://search.search.windows.net",
        "AZURE_OPENAI_CHAT_DEPLOYMENT=approved-chat",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large",
        "AZURE_SEARCH_INDEX=copilot-qa",
        "ESCALATION_CONFIG=approved/escalation.yaml",
        "DIAGNOSTICS_CONFIG=approved/diagnostics.yaml",
    ]) + "\n", encoding="utf-8")
    artifacts = {}
    for target in ("aca", "functions"):
        command = ["uv", "export", "--locked", "--offline", "--package", "advisor-teams", "--no-dev",
                   "--no-editable", "--no-emit-workspace", "--emit-index-url", "--no-header", "--no-annotate"]
        if target == "functions":
            command += ["--extra", "functions"]
        result = subprocess.run(command, cwd=repo, env=environment, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        requirements = result.stdout.rstrip() + "\n"
        for name, content in sorted(wheels.items()):
            requirements += f"./{name} --hash=sha256:{sha256(content).hexdigest()}\n"
        files = wheels | {
            "requirements.txt": requirements.encode(),
            "runtime-config/escalation.yaml": (approved / "escalation.yaml").read_bytes(),
            "runtime-config/diagnostics.yaml": (approved / "diagnostics.yaml").read_bytes(),
        }
        names = ("Dockerfile", "Dockerfile.dockerignore") if target == "aca" else ("function_app.py", "host.json")
        files.update({name: (repo / "deploy" / target / name).read_bytes() for name in names})
        manifest = PackageManifest.create(target=target, files=files, lock_sha256=sha256((repo / "uv.lock").read_bytes()).hexdigest(),
                                          dependency_index="https://packagefeedproxy.microsoft.io/pypi/simple/")
        files["package-manifest.json"] = json.dumps(manifest.to_dict()).encode()
        artifact = out / ("aca-context" if target == "aca" else "functions.zip")
        if target == "aca":
            for name, content in files.items():
                file = artifact / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(content)
        else:
            with zipfile.ZipFile(artifact, "w") as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
        artifacts[target] = artifact
    state_path, log_path = tmp_path / "state.json", tmp_path / "calls.jsonl"
    implementation = Path(__file__).with_name("deployment_fixture.py")
    for tool in ("az", "docker"):
        executable = tmp_path / (tool + ".cmd" if os.name == "nt" else tool)
        if os.name == "nt":
            executable.write_text(
                f'@set ADVISOR_STUB_TOOL={tool}\n'
                f'@set "ADVISOR_STUB_STATE={state_path}"\n@set "ADVISOR_STUB_LOG={log_path}"\n'
                f'@"{sys.executable}" "{implementation}" %*\n', encoding="utf-8")
        else:
            executable.write_text(
                f'#!/bin/sh\nexport ADVISOR_STUB_TOOL={tool}\n'
                f'export ADVISOR_STUB_STATE="{state_path}"\nexport ADVISOR_STUB_LOG="{log_path}"\n'
                f'exec "{sys.executable}" "{implementation}" "$@"\n', encoding="utf-8")
            executable.chmod(0o700)
    environment |= {
        "ADVISOR_AZ_COMMAND": str(implementation),
        "ADVISOR_DOCKER_COMMAND": str(tmp_path / ("docker.cmd" if os.name == "nt" else "docker")),
        "ADVISOR_PYTHON_COMMAND": sys.executable, "ADVISOR_STUB_STATE": str(state_path), "ADVISOR_STUB_LOG": str(log_path),
        "PATH": str(tmp_path) + os.pathsep + environment["PATH"],
    }
    docker_python = tmp_path / "fake_docker.py"
    docker_python.write_text(
        "import os,runpy\nos.environ['ADVISOR_STUB_TOOL']='docker'\n"
        f"runpy.run_path({str(implementation)!r},run_name='__main__')\n", encoding="utf-8")
    environment["ADVISOR_DOCKER_COMMAND"] = str(docker_python)
    run_id = str(uuid4())
    plans = {}
    for target in ("aca", "functions"):
        evidence = {"subscription_id": SUB, "location": "southeastasia", "kind": "flex-regional-memory",
                    "limit_mb": 512000, "used_mb": 0, "observed_at": datetime.now(timezone.utc).isoformat(),
                    "source": "Flex Consumption Quota diagnostic"}
        data = {"format": "advisor-plan-v1", "target": target, "run_id": run_id, "env_file": str(env_file),
                "artifact": str(artifacts[target]), "manifest": str(out / (target + "-ownership.json"))}
        if target == "functions":
            data["capacity_evidence"] = evidence
        plan_path = repo / (target + "-plan.json")
        plan_path.write_text(json.dumps(data), encoding="utf-8")
        plans[target] = (plan_path, data, digest(evidence))

    class Case:
        def reset(self, target="aca", **overrides):
            state_path.write_text(json.dumps({"target": target} | overrides), encoding="utf-8")
            log_path.unlink(missing_ok=True)

        def invoke(self, script, *arguments):
            result = subprocess.run([shell, "-NoProfile", "-File", str(repo / "deploy" / script), *map(str, arguments)],
                                    cwd=repo, env=environment, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=180, check=False)
            assert "fixture-secret" not in result.stdout + result.stderr
            assert not list(out.glob(".private-*")), "private parameter directory leaked"
            lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
            report = json.loads(lines[-1]) if lines else {}
            return result, report

        def calls(self):
            return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []

    case = Case()
    case.repo, case.out, case.plans, case.run_id = repo, out, plans, run_id
    case.state_path, case.log_path, case.env_file = state_path, log_path, env_file
    case.reset()
    return case


def test_default_whatif_is_nonmutating_subscription_scoped_and_private(workflow_case):
    case = workflow_case
    result, report = case.invoke("aca/Deploy.ps1", "-ParametersFile", case.plans["aca"][0])
    assert result.returncode == 0, result.stderr + result.stdout
    assert report["mode"] == "WhatIf" and report["scope"] == "subscription"
    calls = case.calls()
    assert not any(token in event["arguments"] for event in calls for token in ("create", "update", "delete", "push", "register"))
    deployments = [event for event in calls if event["arguments"][:2] == ["deployment", "sub"]]
    assert {event["arguments"][2] for event in deployments} == {"validate", "what-if"}
    assert all(event["secret_seen"] for event in deployments)
    assert all(not Path(event["private_file"]).exists() for event in deployments)
    assert not Path(case.plans["aca"][1]["manifest"]).exists()
    for event in calls:
        if event["arguments"][:2] != ["account", "show"]:
            assert SUB in event["arguments"]


@pytest.mark.parametrize(("change", "code"), [
    ({"subscription": "00000000-0000-0000-0000-000000000000"}, "active_subscription_mismatch"),
    ({"group_exists": True}, "resource_group_ownership_collision"),
    ({"provider_missing": True}, "provider_registration_required"),
    ({"permissions_denied": True}, "arm_permissions_unverified"),
    ({"dimensions": 1536}, "search_vector_dimensions_mismatch"),
])
def test_preflight_gates_fail_closed_without_cloud_mutation(workflow_case, change, code):
    case = workflow_case
    case.reset(**change)
    result, report = case.invoke("shared/Preflight.ps1", "-Target", "aca", "-ParametersFile", case.plans["aca"][0])
    assert result.returncode != 0 and report["error"]["code"] == code
    assert not any(token in event["arguments"] for event in case.calls() for token in ("create", "update", "delete", "register"))


def test_functions_requires_confirmed_applicable_capacity_not_generic_vm_quota(workflow_case):
    case = workflow_case
    case.reset("functions")
    result, report = case.invoke("functions/Deploy.ps1", "-ParametersFile", case.plans["functions"][0])
    assert result.returncode != 0 and report["error"]["code"] == "flex_capacity_evidence_unconfirmed"
    result, report = case.invoke("functions/Deploy.ps1", "-ParametersFile", case.plans["functions"][0],
                                "-CapacityEvidenceHash", case.plans["functions"][2])
    assert result.returncode == 0, result.stderr + result.stdout
    assert report["planned_stages"] == ["infrastructure", "OneDeploy-remote-build"]
    assert report["capacity_projection"] == {
        "scale_groups": ["http", "process_queue", "maintenance"],
        "memory_mb": 61440, "equivalent_cores": 30.0, "capacity_reserved": False,
    }


def test_stale_source_is_rejected_before_azure_calls(workflow_case):
    case = workflow_case
    source = case.repo / "agent" / "src" / "advisor_agent" / "__init__.py"
    source.write_text("changed_source = True\n", encoding="utf-8")
    result, report = case.invoke("aca/Deploy.ps1", "-ParametersFile", case.plans["aca"][0])
    assert result.returncode != 0 and report["error"]["code"] == "artifact_source_mismatch"
    assert case.calls() == []


def test_functions_apply_uses_onedeploy_then_safe_readback_and_manifest(workflow_case):
    case = workflow_case
    case.reset("functions")
    common = ("-ParametersFile", case.plans["functions"][0], "-CapacityEvidenceHash", case.plans["functions"][2])
    result, report = case.invoke("functions/Deploy.ps1", *common)
    assert result.returncode == 0, result.stdout + result.stderr
    result, applied = case.invoke("functions/Deploy.ps1", *common, "-Mode", "Apply", "-Confirmation", report["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert applied["application_validation"] == "not_run"
    publish = [event for event in case.calls() if "config-zip" in event["arguments"]]
    assert len(publish) == 1
    assert publish[0]["arguments"][publish[0]["arguments"].index("--build-remote") + 1] == "true"
    ownership = json.loads(Path(case.plans["functions"][1]["manifest"]).read_text(encoding="utf-8"))
    assert ownership["status"] == "deployed" and ownership["discovery_complete"]
    assert ownership["pending_role_assignment_ids"] == []
    assert "fixture-secret" not in json.dumps(ownership)


def test_apply_without_exact_confirmation_never_creates_a_group(workflow_case):
    case = workflow_case
    result, report = case.invoke("aca/Deploy.ps1", "-ParametersFile", case.plans["aca"][0], "-Mode", "Apply")
    assert result.returncode != 0 and report["error"]["code"] == "apply_confirmation_required"
    assert not any("create" in event["arguments"] for event in case.calls())


def test_unavailable_docker_blocks_aca_apply_before_bootstrap(workflow_case):
    case = workflow_case
    common = ("-ParametersFile", case.plans["aca"][0])
    result, preview = case.invoke("aca/Deploy.ps1", *common)
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(case.state_path.read_text(encoding="utf-8")) | {"docker_down": True}
    case.state_path.write_text(json.dumps(state), encoding="utf-8")
    start = len(case.calls())
    result, report = case.invoke("aca/Deploy.ps1", *common, "-Mode", "Apply",
                                "-Confirmation", preview["confirmation"])
    assert result.returncode != 0
    assert report["error"]["phase"] == "docker_readiness"
    assert not any(token in event["arguments"] for event in case.calls()[start:]
                   for token in ("create", "update", "push", "delete"))


def test_benchmark_default_does_not_create_directory_objects_and_permissions_block(workflow_case):
    case = workflow_case
    path = case.repo / "benchmark-plan.json"
    path.write_text(json.dumps({"format": "advisor-benchmark-plan-v1", "run_id": case.run_id,
                               "manifest": str(case.out / "benchmark.json")}), encoding="utf-8")
    result, report = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["mode"] == "WhatIf"
    assert all(event["arguments"][event["arguments"].index("--method") + 1] == "get"
               for event in case.calls() if "--method" in event["arguments"])
    case.reset(graph_denied=True)
    result, report = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path)
    assert result.returncode != 0 and report["error"]["code"] == "graph_application_permissions_missing"


def apply_functions(case):
    case.reset("functions")
    common = ("-ParametersFile", case.plans["functions"][0], "-CapacityEvidenceHash", case.plans["functions"][2])
    result, report = case.invoke("functions/Deploy.ps1", *common)
    assert result.returncode == 0, result.stdout + result.stderr
    result, report = case.invoke("functions/Deploy.ps1", *common, "-Mode", "Apply", "-Confirmation", report["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    return Path(case.plans["functions"][1]["manifest"])


def test_aca_apply_is_bootstrap_then_digest_pinned_app_and_mode_only_changes_min(workflow_case):
    case = workflow_case
    common = ("-ParametersFile", case.plans["aca"][0])
    result, report = case.invoke("aca/Deploy.ps1", *common)
    assert result.returncode == 0, result.stdout + result.stderr
    result, _ = case.invoke("aca/Deploy.ps1", *common, "-Mode", "Apply", "-Confirmation", report["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = case.calls()
    creates = [event for event in calls if event["arguments"][:3] == ["deployment", "sub", "create"]]
    assert [event["deploy_app"] for event in creates] == [False, True]
    assert any(event["tool"] == "docker" and event["arguments"][0] == "push" for event in calls)
    docker_calls = [event for event in calls if event["tool"] == "docker" and event["arguments"][0] != "context"]
    assert {event["docker_endpoint"] for event in docker_calls} == {"npipe:////./pipe/dockerDesktopLinuxEngine"}
    assert docker_calls[-1]["docker_config"].endswith("docker")
    ownership = Path(case.plans["aca"][1]["manifest"])
    data = json.loads(ownership.read_text(encoding="utf-8"))
    assert data["image_reference"] == "fixture.azurecr.io/advisor@sha256:" + "d" * 64
    before = len(calls)
    result, report = case.invoke("aca/Set-Mode.ps1", "-OutputsFile", ownership, "-Mode", "Resident")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("update" in event["arguments"] for event in case.calls()[before:])
    result, _ = case.invoke("aca/Set-Mode.ps1", "-OutputsFile", ownership, "-Mode", "Resident",
                            "-Apply", "-Confirmation", report["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    changes = [event["arguments"] for event in case.calls()[before:] if event["arguments"][:2] == ["containerapp", "update"]]
    assert len(changes) == 1 and changes[0][changes[0].index("--min-replicas") + 1] == "1"
    assert "--max-replicas" not in changes[0]


def test_cleanup_requires_exact_confirmation_and_deletes_only_recorded_scope(workflow_case):
    case = workflow_case
    ownership = apply_functions(case)
    data = json.loads(ownership.read_text(encoding="utf-8"))
    start = len(case.calls())
    result, preview = case.invoke("Cleanup.ps1", "-OutputsFile", ownership)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("delete" in event["arguments"] for event in case.calls()[start:])
    result, _ = case.invoke("Cleanup.ps1", "-OutputsFile", ownership, "-Mode", "Apply")
    assert result.returncode != 0
    assert not any("delete" in event["arguments"] for event in case.calls()[start:])
    result, _ = case.invoke("Cleanup.ps1", "-OutputsFile", ownership, "-Mode", "Apply",
                           "-Confirmation", preview["confirmation"], "-TrafficStopped", "-InFlightDecision", "abandon")
    assert result.returncode == 0, result.stdout + result.stderr
    deletes = [event["arguments"] for event in case.calls()[start:] if "delete" in event["arguments"]]
    assert {args[args.index("--ids") + 1] for args in deletes if "--ids" in args} == {role["id"] for role in data["role_assignments"]}
    assert sum(args[:2] == ["group", "delete"] for args in deletes) == 1
    result, repeated = case.invoke("Cleanup.ps1", "-OutputsFile", ownership)
    assert result.returncode == 0 and repeated["no_op"]


def test_cleanup_refuses_unrecorded_resources_in_owned_group(workflow_case):
    case = workflow_case
    ownership = apply_functions(case)
    state = json.loads(case.state_path.read_text(encoding="utf-8"))
    group = state["resources"][0]["id"].split("/providers/")[0]
    state["resources"].append({"id": group + "/providers/Microsoft.Storage/storageAccounts/unrelated",
                               "type": "Microsoft.Storage/storageAccounts", "name": "unrelated"})
    case.state_path.write_text(json.dumps(state), encoding="utf-8")
    start = len(case.calls())
    result, report = case.invoke("Cleanup.ps1", "-OutputsFile", ownership, "-Mode", "Apply",
                                "-Confirmation", "not-an-approval")
    assert result.returncode != 0 and report["error"]["code"] == "unrecorded_resource_collision"
    assert not any("delete" in event["arguments"] for event in case.calls()[start:])


def test_partial_arm_failure_records_receipts_and_never_claims_application_success(workflow_case):
    case = workflow_case
    case.reset("functions")
    common = ("-ParametersFile", case.plans["functions"][0], "-CapacityEvidenceHash", case.plans["functions"][2])
    result, report = case.invoke("functions/Deploy.ps1", *common)
    assert result.returncode == 0
    state = json.loads(case.state_path.read_text(encoding="utf-8")) | {"apply_fail": True}
    case.state_path.write_text(json.dumps(state), encoding="utf-8")
    result, _ = case.invoke("functions/Deploy.ps1", *common, "-Mode", "Apply", "-Confirmation", report["confirmation"])
    assert result.returncode != 0
    ownership = Path(case.plans["functions"][1]["manifest"])
    record = json.loads(ownership.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["owned_resource_ids"] and record["pending_role_assignment_ids"]
    assert record["application_validation"] == "not_run"
    assert json.loads(case.state_path.read_text(encoding="utf-8"))["owner"] == case.run_id
    result, blocked = case.invoke("Cleanup.ps1", "-OutputsFile", ownership)
    assert result.returncode != 0 and blocked["error"]["code"] == "ownership_discovery_incomplete"


def test_benchmark_register_is_explicit_owned_v2_single_role_and_idempotent(workflow_case):
    case = workflow_case
    path = case.repo / "benchmark-plan.json"
    ownership = case.out / "benchmark.json"
    path.write_text(json.dumps({"format": "advisor-benchmark-plan-v1", "run_id": case.run_id, "manifest": str(ownership)}), encoding="utf-8")
    result, preview = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path)
    assert result.returncode == 0
    result, report = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path, "-Mode", "Apply",
                                "-Confirmation", preview["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(case.state_path.read_text(encoding="utf-8"))
    app = state["applications"][0]
    assert app["signInAudience"] == "AzureADMyOrg"
    assert app["api"]["requestedAccessTokenVersion"] == 2
    assert [role["value"] for role in app["appRoles"]] == ["Benchmark.Invoke"]
    assert report["audience"] == app["appId"]
    assert report["scope"] == "api://" + app["appId"] + "/.default"
    result, _ = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path, "-Mode", "Apply",
                           "-Confirmation", preview["confirmation"])
    assert result.returncode == 0
    calls = case.calls()
    creates = [event for event in calls if "--method" in event["arguments"] and event["arguments"][event["arguments"].index("--method") + 1] == "post"]
    assert len(creates) == 2


def test_benchmark_never_adopts_an_unrelated_registration_with_same_name(workflow_case):
    case = workflow_case
    case.reset(applications=[{"id": str(uuid4()), "appId": str(uuid4()), "tags": []}])
    path = case.repo / "benchmark-plan.json"
    path.write_text(json.dumps({"format": "advisor-benchmark-plan-v1", "run_id": case.run_id,
                               "manifest": str(case.out / "benchmark.json")}), encoding="utf-8")
    result, report = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path, "-Mode", "Apply")
    assert result.returncode != 0 and report["error"]["code"] == "benchmark_name_collision"
    assert not any("post" in event["arguments"] for event in case.calls())


def test_enabling_benchmark_requires_the_owned_api_and_driver_grant_evidence(workflow_case):
    from dataset_fixture import add_approved_dataset
    case = workflow_case
    plan_path, plan, _ = case.plans["aca"]
    plan["overrides"] = {
        "BENCHMARK_ENABLED": "true", "BENCHMARK_AUDIENCE": "55555555-5555-5555-5555-555555555555",
        "BENCHMARK_API_CLIENT_ID": "55555555-5555-5555-5555-555555555555",
        "BENCHMARK_ALLOWED_PRINCIPAL_IDS": "22222222-2222-2222-2222-222222222222",
        "BENCHMARK_ALLOWED_CLIENT_IDS": "33333333-3333-3333-3333-333333333333",
        "BENCHMARK_RECEIVER_URL": "https://fixture.example.net",
        "BENCHMARK_RECEIVER_AUDIENCE": "55555555-5555-5555-5555-555555555555",
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    add_approved_dataset(case, "aca")
    result, report = case.invoke("aca/Deploy.ps1", "-ParametersFile", plan_path)
    assert result.returncode != 0 and report["error"]["code"] == "benchmark_registration_evidence_required"
    assert not any("create" in event["arguments"] for event in case.calls())


def test_benchmark_grants_only_three_owned_identities_and_tracks_exact_ids(workflow_case):
    from advisor_deploy.manifest import new_manifest, write_manifest
    case = workflow_case
    ownership = case.out / "benchmark.json"
    path = case.repo / "benchmark-plan.json"
    plan = {"format": "advisor-benchmark-plan-v1", "run_id": case.run_id, "manifest": str(ownership)}
    path.write_text(json.dumps(plan), encoding="utf-8")
    result, preview = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path)
    assert result.returncode == 0
    result, _ = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path,
                           "-Mode", "Apply", "-Confirmation", preview["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(case.state_path.read_text(encoding="utf-8"))
    state.update(group_exists=True, owner=case.run_id, resources=[], identities={})
    manifests = {}
    for target, suffix in (("aca", "aca"), ("functions", "flex")):
        group = f"/subscriptions/{SUB}/resourceGroups/rg-copilot-advisor-{suffix}-exp"
        record = new_manifest(target, case.run_id, "fixture", "a" * 64, "b" * 64)
        record["tenant_id"] = "4bc8a937-2b77-44c4-8790-dfc0068162f2"
        record["owned_resource_ids"] = [group]
        for label in ([target, "driver"] if target == "aca" else [target]):
            identifier = group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/" + label
            record["owned_resource_ids"].append(identifier)
            state["resources"].append({"id": identifier, "name": label, "type": "Microsoft.ManagedIdentity/userAssignedIdentities"})
            state["identities"][identifier] = {
                "id": identifier, "principalId": str(uuid4()), "clientId": str(uuid4()),
                "tags": {"experiment": {"driver": "benchmark", "aca": "aca", "functions": "flex"}[label]},
            }
            manifests[label] = {"manifest": str(case.out / (target + "-principal.json")), "identity_id": identifier}
        write_manifest(case.out / (target + "-principal.json"), record, case.repo)
    plan["principals"] = manifests
    path.write_text(json.dumps(plan), encoding="utf-8")
    case.state_path.write_text(json.dumps(state), encoding="utf-8")
    result, preview = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path, "-Stage", "Grant")
    assert result.returncode == 0, result.stdout + result.stderr
    result, _ = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path, "-Stage", "Grant",
                           "-Mode", "Apply", "-Confirmation", preview["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    graph = json.loads(ownership.read_text(encoding="utf-8"))["graph"]
    assert len(graph["grants"]) == 3
    assert {grant["principal_id"] for grant in graph["grants"]} == {item["principalId"] for item in state["identities"].values()}
    assert {grant["resource_id"] for grant in graph["grants"]} == {graph["service_principal_id"]}
    assert {grant["role_id"] for grant in graph["grants"]} == {graph["role_id"]}
    state = json.loads(case.state_path.read_text(encoding="utf-8"))
    state["grants"].append({"id": "unrelated-grant", "principalId": str(uuid4()),
                            "resourceId": graph["service_principal_id"], "appRoleId": graph["role_id"]})
    case.state_path.write_text(json.dumps(state), encoding="utf-8")
    result, blocked = case.invoke("shared/Ensure-BenchmarkIdentity.ps1", "-ParametersFile", path, "-Stage", "Grant")
    assert result.returncode != 0 and blocked["error"]["code"] == "unrecorded_graph_grant"

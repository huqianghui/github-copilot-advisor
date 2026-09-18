"""Dataset presence and installed-path rendering; no Azure/Docker operations."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from advisor_deploy.render import PrivateFiles, load_plan, parameters_file
from dataset_fixture import DATASET, add_approved_dataset
from test_deploy_workflows import workflow_case  # shared isolated workspace fixture


def enable_benchmark(case, target):
    path, data, _ = case.plans[target]
    data["overrides"] = {
        "BENCHMARK_ENABLED": "true",
        "BENCHMARK_API_CLIENT_ID": "55555555-5555-5555-5555-555555555555",
        "BENCHMARK_AUDIENCE": "55555555-5555-5555-5555-555555555555",
        "BENCHMARK_RECEIVER_AUDIENCE": "55555555-5555-5555-5555-555555555555",
        "BENCHMARK_RECEIVER_URL": "https://fixture.example.net",
        "BENCHMARK_ALLOWED_PRINCIPAL_IDS": "22222222-2222-2222-2222-222222222222",
        "BENCHMARK_ALLOWED_CLIENT_IDS": "33333333-3333-3333-3333-333333333333",
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_enabled_benchmark_rejects_an_artifact_without_approved_dataset(workflow_case, target):
    case = workflow_case
    path = enable_benchmark(case, target)
    with pytest.raises(ValueError, match="benchmark_dataset_missing"):
        load_plan(path, case.repo, target)


@pytest.mark.parametrize(("target", "installed"), [
    ("aca", "/app/runtime-config/benchmark-cases.json"),
    ("functions", "/home/site/wwwroot/runtime-config/benchmark-cases.json"),
])
def test_dataset_path_is_rendered_to_private_arm_parameters(workflow_case, target, installed):
    case = workflow_case
    path = enable_benchmark(case, target)
    add_approved_dataset(case, target)
    plan = load_plan(path, case.repo, target)
    assert plan.config.runtime_environment["BENCHMARK_CASES_PATH"] == installed
    assert plan.config.parameters["runtimeSettings"]["BENCHMARK_CASES_PATH"] == installed
    shell = shutil.which("pwsh")
    assert shell is not None
    helper = str(case.repo / "deploy" / "shared" / "Azure.ps1").replace("'", "''")
    result = subprocess.run(
        [shell, "-NoProfile", "-Command", f". '{helper}'; New-AdvisorPrivateDirectory"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    private_path = Path(result.stdout.strip())
    try:
        private = PrivateFiles(private_path, case.repo)
        output = parameters_file(plan, private)
        parameters = json.loads(output.read_text(encoding="utf-8"))["parameters"]
        assert parameters["runtimeSettings"]["value"]["BENCHMARK_CASES_PATH"] == installed
        assert len([name for name in plan.artifact.files if name.startswith("wheels/")]) == 3
    finally:
        directory = str(private_path).replace("'", "''")
        cleaned = subprocess.run(
            [shell, "-NoProfile", "-Command", f". '{helper}'; Remove-AdvisorPrivateDirectory '{directory}'"],
            capture_output=True, text=True, check=False,
        )
        assert cleaned.returncode == 0, cleaned.stderr


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_disabled_benchmark_does_not_require_or_enable_dataset(workflow_case, target):
    case = workflow_case
    path = case.plans[target][0]
    plan = load_plan(path, case.repo, target)
    assert "BENCHMARK_CASES_PATH" not in plan.config.parameters["runtimeSettings"]
    assert DATASET not in plan.artifact.files
    add_approved_dataset(case, target)
    plan = load_plan(path, case.repo, target)
    assert "BENCHMARK_CASES_PATH" not in plan.config.parameters["runtimeSettings"]

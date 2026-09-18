import json
import subprocess
import sys


def test_root_cli_runs_smoke_and_persists_incomplete_result(tmp_path):
    deployment = tmp_path / "deployment.json"
    deployment.write_text('{"private_value":"never-echo-this"}', encoding="utf-8")
    output = tmp_path / "smoke.json"
    result = subprocess.run(
        [sys.executable, "-m", "advisor_deploy", "smoke", "--deployment", str(deployment),
         "--auth-mode", "entra", "--output", str(output)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1
    assert output.is_file()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["issues"] == ["deployment_rejected"]
    assert report["checks"]["complete_answer"] == "not_run"
    assert not report["passed"]
    assert "never-echo-this" not in result.stdout + result.stderr + output.read_text()


def test_root_smoke_cli_retains_body_free_argument_errors():
    result = subprocess.run(
        [sys.executable, "-m", "advisor_deploy", "smoke", "--never-echo-this"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 2
    assert "smoke: invalid_arguments" in result.stderr
    assert "never-echo-this" not in result.stdout + result.stderr

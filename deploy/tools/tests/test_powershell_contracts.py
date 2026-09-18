import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
SUB = "adca540e-984c-4cab-bc86-5d38bf979841"


@pytest.fixture
def powershell():
    command = shutil.which("pwsh")
    if command is None:
        pytest.skip("PowerShell 7 is required")
    return command


def run_ps(shell, script, env=None):
    return subprocess.run(
        [shell, "-NoProfile", "-Command", script], cwd=ROOT,
        env=os.environ | (env or {}), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )


@pytest.fixture
def fake_az(tmp_path):
    implementation = tmp_path / "fake_az.py"
    implementation.write_text(
        "import json,os,sys\n"
        "with open(os.environ['ADVISOR_STUB_LOG'],'a',encoding='utf-8') as log:\n"
        " log.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "mode=os.environ.get('ADVISOR_STUB_MODE','ok')\n"
        "if mode=='fail':\n"
        " print('ERROR: (AuthorizationFailed) fixture-secret',file=sys.stderr);sys.exit(7)\n"
        "if mode=='malformed': print('fixture-secret-not-json');sys.exit(0)\n"
        "if mode=='empty': sys.exit(0)\n"
        f"print(json.dumps({{'id': '{SUB}', 'tenantId': '11111111-1111-1111-1111-111111111111'}}))\n",
        encoding="utf-8",
    )
    command = tmp_path / ("az.cmd" if os.name == "nt" else "az")
    if os.name == "nt":
        command.write_text(f'@"{sys.executable}" "{implementation}" %*\n', encoding="utf-8")
    else:
        command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{implementation}" "$@"\n', encoding="utf-8")
        command.chmod(0o700)
    return {
        "ADVISOR_AZ_COMMAND": str(command),
        "ADVISOR_STUB_LOG": str(tmp_path / "calls.jsonl"),
        "ADVISOR_PYTHON_COMMAND": sys.executable,
    }


@pytest.mark.parametrize("mode", ["fail", "malformed", "empty"])
def test_checked_az_never_swallows_failures_or_echoes_sensitive_output(powershell, fake_az, mode):
    result = run_ps(powershell, ". .\\deploy\\shared\\Azure.ps1; Invoke-CheckedAz -Arguments @('account','show')", fake_az | {"ADVISOR_STUB_MODE": mode})
    assert result.returncode != 0
    assert "fixture-secret" not in result.stdout + result.stderr
    assert Path(fake_az["ADVISOR_STUB_LOG"]).exists()


def test_checked_az_parses_json_success(powershell, fake_az):
    result = run_ps(powershell, ". .\\deploy\\shared\\Azure.ps1; (Invoke-CheckedAz -Arguments @('account','show')).id", fake_az)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == SUB


def test_scope_builder_uses_subscription_roots_and_group_benchmark(powershell):
    script = (
        ". .\\deploy\\shared\\Azure.ps1; "
        "$result=@{}; foreach($target in @('aca','functions','bench')) {"
        "$result[$target]=@(Get-AdvisorDeploymentArguments -Target $target -Action 'what-if' "
        "-TemplateFile 'main.bicep' -ParametersFile 'private.json' -DeploymentName 'fixture') }; "
        "$result | ConvertTo-Json -Depth 5 -Compress"
    )
    result = run_ps(powershell, script)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values["aca"][:3] == ["deployment", "sub", "what-if"]
    assert values["functions"][:3] == ["deployment", "sub", "what-if"]
    assert values["bench"][:3] == ["deployment", "group", "what-if"]
    for arguments in values.values():
        assert arguments[arguments.index("--subscription") + 1] == SUB
        assert "create" not in arguments


def test_private_directory_is_restricted_before_parameter_content(powershell):
    script = (
        ". .\\deploy\\shared\\Azure.ps1; $private=New-AdvisorPrivateDirectory; "
        "try { Assert-AdvisorPrivateDirectory $private; "
        "$file=Join-Path $private 'parameters.json'; "
        "[IO.File]::WriteAllText($file,'fixture-secret'); "
        "Assert-AdvisorPrivateDirectory $private; "
        "if($IsWindows) { $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "$foreign=@((Get-Acl -LiteralPath $file).Access | Where-Object {"
        "$_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $sid}); "
        "if($foreign.Count) {throw 'foreign ACL'} }; "
        "@{valid=$true; path=$private}|ConvertTo-Json -Compress "
        "} finally { Remove-AdvisorPrivateDirectory $private }"
    )
    result = run_ps(powershell, script)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["valid"] is True
    assert not Path(data["path"]).exists()
    assert "fixture-secret" not in result.stdout + result.stderr

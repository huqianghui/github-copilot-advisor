import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "teams_adapter.cloud.ingress", "teams_adapter.cloud.runtime", "teams_adapter.__main__",
    "teams_adapter.cloud.functions", "function_app",
])
def test_cloud_host_import_does_not_load_agent_or_model(module):
    root = Path(__file__).parents[4]
    result = subprocess.run([
        sys.executable, "-c",
        f"import importlib, json, sys; sys.path.insert(0, {str(root / 'deploy' / 'functions')!r}); "
        f"importlib.import_module({module!r}); "
        "print(json.dumps(sorted(sys.modules)))",
    ], cwd=root, capture_output=True, text=True, timeout=120, check=True)
    modules = json.loads(result.stdout)
    assert "openai" not in modules
    assert "agent_framework" not in modules
    assert "advisor_agent.factory" not in modules


def test_constructing_ingress_and_maintenance_remains_model_free():
    from test_aca_lifecycle import CONFIG
    result = subprocess.run([
        sys.executable, "-c",
        "import asyncio, json, sys; from teams_adapter.cloud.runtime import build_runtime; "
        f"runtime = build_runtime({CONFIG!r}); "
        "print(json.dumps(sorted(sys.modules))); asyncio.run(runtime.aclose())",
    ], cwd=Path(__file__).parents[4], capture_output=True, text=True, timeout=120, check=True)
    modules = json.loads(result.stdout)
    assert "openai" not in modules
    assert "agent_framework" not in modules
    assert "advisor_agent.factory" not in modules

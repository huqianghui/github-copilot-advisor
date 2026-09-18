from hashlib import sha256
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_benchmark_context_has_only_two_workspace_wheels_and_locked_closure(tmp_path):
    from advisor_deploy.bench.build import build_context, verify_context
    from advisor_deploy.package import _wheel as app_wheel

    context = tmp_path / "context"
    manifest = build_context(ROOT / "deploy" / "bench" / "run.example.json", context)
    assert set(path.name for path in context.iterdir()) == {
        "Dockerfile", ".dockerignore", "requirements.txt", "wheels",
        "run.json", "runtime-config", "benchmark-manifest.json",
    }
    wheels = sorted(path.name for path in (context / "wheels").iterdir())
    assert {name.split("-")[0] for name in wheels} == {"advisor_deploy", "advisor_shared"}
    assert len(wheels) == 2
    assert manifest["lock_sha256"] == sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    assert manifest["linux_imports_verified"] is False
    assert manifest["base_image_digest"] is None
    dependencies = {item["name"] for item in manifest["registry_dependencies"]}
    assert {"aiohttp", "azure-storage-blob", "azure-identity", "pydantic"} <= dependencies
    assert not dependencies & {"advisor-agent", "advisor-teams", "advisor-ingestion",
                               "agent-framework-core", "openai", "pytest", "respx"}
    requirements = (context / "requirements.txt").read_text()
    assert "azure-storage-blob==" in requirements
    assert requirements.count("./wheels/") == 2
    assert "--hash=sha256:" in requirements
    run = json.loads((context / "run.json").read_text())
    assert run["cases_path"] == "runtime-config/benchmark-cases.json"
    dataset = context / "runtime-config" / "benchmark-cases.json"
    assert dataset.read_bytes() == (ROOT / "deploy" / "bench" / "cases.json").read_bytes()
    assert list((context / "runtime-config").iterdir()) == [dataset]
    assert verify_context(context)["files"] == manifest["files"]
    repeated = build_context(ROOT / "deploy" / "bench" / "run.example.json",
                             tmp_path / "repeated")
    assert repeated["files"] == manifest["files"]

    deploy_wheel = context / "wheels" / wheels[0]
    with pytest.raises(ValueError):
        app_wheel(deploy_wheel.name, deploy_wheel.read_bytes())

    (context / ".env").write_text("NOT_A_REAL_SECRET=must-not-enter-context")
    with pytest.raises(ValueError):
        verify_context(context)
    (context / ".env").unlink()
    original = deploy_wheel.read_bytes()
    deploy_wheel.write_bytes(original + b"tamper")
    with pytest.raises(ValueError):
        verify_context(context)
    deploy_wheel.write_bytes(original)
    assert verify_context(context)["files"] == manifest["files"]


def test_build_rejects_output_outside_artifact_tree_and_undeclared_sink_dependencies(tmp_path):
    from advisor_deploy.bench.build import build_context, validate_direct_dependencies

    with pytest.raises(ValueError):
        validate_direct_dependencies(["advisor-shared", "aiohttp>=3.9"])
    validate_direct_dependencies(["advisor-shared", "aiohttp>=3.9,<4",
                                  "azure-storage-blob>=12.24,<13"])
    with pytest.raises(ValueError):
        build_context(ROOT / "deploy" / "bench" / "run.example.json", ROOT / "bench-context")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("not an owned context")
    with pytest.raises(ValueError):
        build_context(ROOT / "deploy" / "bench" / "run.example.json", occupied)
    assert (occupied / "keep.txt").read_text() == "not an owned context"


@pytest.mark.skipif(os.environ.get("ADVISOR_BENCH_DOCKER") != "1",
                    reason="explicit real Linux Docker verification")
def test_actual_linux_image_imports_use_recorded_base_digest(tmp_path):
    from advisor_deploy.bench.build import build_context, build_image, verify_context

    context = tmp_path / "linux-context"
    build_context(ROOT / "deploy" / "bench" / "run.example.json", context)
    manifest = build_image(context, "advisor-bench-verification:local")
    assert manifest["linux_imports_verified"] is True
    assert manifest["base_image_digest"].startswith("sha256:")
    assert manifest["base_image_reference"].endswith("@" + manifest["base_image_digest"])
    assert manifest["image_id"].startswith("sha256:")
    assert verify_context(context)["linux_imports_verified"] is True

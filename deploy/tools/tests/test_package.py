import hashlib
import io
import json
import zipfile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ONLINE = {"advisor-shared": "advisor_shared", "advisor-agent": "advisor_agent", "advisor-teams": "teams_adapter"}


def make_wheel(name, module):
    buffer = io.BytesIO()
    stem = name.replace("-", "_") + "-0.1.0"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{module}/__init__.py", '"""Packaging test fixture."""\n')
        archive.writestr(f"{module}/__main__.py", '"""Packaging test fixture entry."""\n')
        archive.writestr(f"{stem}.dist-info/METADATA", f"Metadata-Version: 2.3\nName: {name}\nVersion: 0.1.0\n")
        archive.writestr(f"{stem}.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{stem}.dist-info/RECORD", "")
    return stem + "-py3-none-any.whl", buffer.getvalue()


@pytest.fixture
def artifact_files():
    files = {
        "function_app.py": b'"""Test fixture only; not a generated deployment entry."""\n',
        "host.json": b'{"version":"2.0"}\n',
        "runtime-config/escalation.yaml": b"contacts: []\n",
        "runtime-config/diagnostics.yaml": b"checks: []\n",
    }
    requirements = ["--index-url https://packagefeedproxy.microsoft.io/pypi/simple/"]
    requirements.append("azure-functions==1.24.0 --hash=sha256:" + "a" * 64)
    for name, module in ONLINE.items():
        filename, content = make_wheel(name, module)
        files["wheels/" + filename] = content
        requirements.append(f"./wheels/{filename} --hash=sha256:{hashlib.sha256(content).hexdigest()}")
    files["requirements.txt"] = ("\n".join(requirements) + "\n").encode()
    return files


def write_zip(path, files, *, hashes=None):
    from advisor_deploy.package import PackageManifest
    manifest = PackageManifest.create(
        target="functions", files=files, lock_sha256="f" * 64,
        dependency_index="https://packagefeedproxy.microsoft.io/pypi/simple/",
    ).to_dict()
    if hashes:
        manifest["files"].update(hashes)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("package-manifest.json", json.dumps(manifest))
    return path


def verify(path):
    from advisor_deploy.package import validate_package
    return validate_package(path)


def test_valid_functions_zip_is_inspected_without_extraction(tmp_path, artifact_files):
    path = write_zip(tmp_path / "valid.zip", artifact_files)
    manifest = verify(path)
    assert manifest.target == "functions"
    assert manifest.python_version == "3.12"
    assert manifest.platform == "linux/amd64"
    assert manifest.deployment_scope == "subscription"
    assert set(manifest.files) == set(artifact_files)
    assert not manifest.linux_imports_verified


@pytest.mark.parametrize("name", [
    ".env", "nested/.env", ".venv/Lib/site.py", "tests/test_secret.py", ".state/jobs.json",
    "ingestion/__init__.py", "dev/settings.json", "../escape", "/absolute", "C:/escape",
    "nested\\escape", "wheels/../escape", "nested//escape",
])
def test_zip_rejects_forbidden_and_traversing_entries(tmp_path, artifact_files, name):
    path = write_zip(tmp_path / "bad.zip", artifact_files | {name: b"private-value"})
    with pytest.raises(ValueError, match="package") as error:
        verify(path)
    assert "private-value" not in str(error.value)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("name", [
    "host.json", "function_app.py", "runtime-config/escalation.yaml",
    "runtime-config/diagnostics.yaml", "wheels/advisor_agent-0.1.0-py3-none-any.whl",
])
def test_zip_requires_real_entries_configs_and_all_online_modules(tmp_path, artifact_files, name):
    del artifact_files[name]
    with pytest.raises(ValueError, match="package"):
        verify(write_zip(tmp_path / "missing.zip", artifact_files))


def test_zip_rejects_duplicate_entries(tmp_path, artifact_files):
    path = write_zip(tmp_path / "duplicate.zip", artifact_files)
    with zipfile.ZipFile(path, "a") as archive:
        with pytest.warns(UserWarning):
            archive.writestr("host.json", b'{"version":"2.0"}')
    with pytest.raises(ValueError, match="package"):
        verify(path)


def test_zip_rejects_symlinks(tmp_path, artifact_files):
    path = write_zip(tmp_path / "link.zip", artifact_files)
    with zipfile.ZipFile(path, "a") as archive:
        link = zipfile.ZipInfo("runtime-config/link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "../../outside")
    with pytest.raises(ValueError, match="package"):
        verify(path)


def test_package_rejects_tampered_payload_hash(tmp_path, artifact_files):
    path = write_zip(tmp_path / "tampered.zip", artifact_files, hashes={"host.json": "0" * 64})
    with pytest.raises(ValueError, match="hash"):
        verify(path)


@pytest.mark.parametrize("change", ["missing-hash", "wrong-hash", "absolute-wheel", "editable", "unhashed-dependency", "credential-index"])
def test_requirements_cannot_bypass_hashes_or_leak_credentials(tmp_path, artifact_files, change):
    content = artifact_files["requirements.txt"].decode()
    wheel = "advisor_shared-0.1.0-py3-none-any.whl"
    digest = hashlib.sha256(artifact_files["wheels/" + wheel]).hexdigest()
    if change == "missing-hash":
        content = content.replace(" --hash=sha256:" + digest, "")
    elif change == "wrong-hash":
        content = content.replace(digest, "0" * 64)
    elif change == "absolute-wheel":
        content = content.replace("./wheels/" + wheel, "C:/private/" + wheel)
    elif change == "editable":
        content += "\n-e ../shared\n"
    elif change == "unhashed-dependency":
        content += "\npytest==9.1.1\n"
    else:
        content = content.replace("https://packagefeedproxy", "https://user:private-value@packagefeedproxy")
    artifact_files["requirements.txt"] = content.encode()
    with pytest.raises(ValueError, match="package") as error:
        verify(write_zip(tmp_path / "requirements.zip", artifact_files))
    assert "private-value" not in str(error.value)


def test_wheel_contents_are_inspected_not_just_the_outer_archive(tmp_path, artifact_files):
    wheel = "wheels/advisor_agent-0.1.0-py3-none-any.whl"
    old = hashlib.sha256(artifact_files[wheel]).hexdigest()
    buffer = io.BytesIO(artifact_files[wheel])
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("advisor_agent/.env", "private-value")
    artifact_files[wheel] = buffer.getvalue()
    new = hashlib.sha256(artifact_files[wheel]).hexdigest()
    artifact_files["requirements.txt"] = artifact_files["requirements.txt"].replace(old.encode(), new.encode())
    with pytest.raises(ValueError, match="package"):
        verify(write_zip(tmp_path / "nested.zip", artifact_files))


def test_functions_missing_entry_is_clear_and_does_not_generate_placeholder(tmp_path):
    from advisor_deploy.package import build_package
    repository = tmp_path / "repo"
    repository.mkdir()
    escalation, diagnostics = tmp_path / "escalation.yaml", tmp_path / "diagnostics.yaml"
    escalation.write_text("contacts: []\n", encoding="utf-8")
    diagnostics.write_text("checks: []\n", encoding="utf-8")
    output = repository / "deploy" / "out" / "functions.zip"
    with pytest.raises(ValueError, match="function_app.py"):
        build_package("functions", output, repo_root=repository, escalation_config=escalation, diagnostics_config=diagnostics)
    assert not output.exists()
    assert not (repository / "deploy" / "functions").exists()


def test_build_rejects_output_outside_owned_tree_before_running_tools(tmp_path):
    from advisor_deploy.package import build_package
    with pytest.raises(ValueError, match="output"):
        build_package("aca", tmp_path / "source", repo_root=tmp_path)
    assert not (tmp_path / "source").exists()


def test_build_rejects_existing_unowned_output(tmp_path):
    from advisor_deploy.package import build_package
    output = tmp_path / "deploy" / "out" / "context"
    output.mkdir(parents=True)
    (output / "keep.txt").write_text("user owned", encoding="utf-8")
    with pytest.raises(ValueError, match="owned"):
        build_package("aca", output, repo_root=tmp_path)
    assert (output / "keep.txt").read_text(encoding="utf-8") == "user owned"


@pytest.mark.parametrize(("field", "value"), [
    ("target", []), ("base_image_digest", 17), ("dependency_index", 17),
    ("files", None), ("linux_imports_verified", "true"),
])
def test_manifest_type_errors_are_bounded_public_errors(tmp_path, artifact_files, field, value):
    path = write_zip(tmp_path / "bad-type.zip", artifact_files)
    with zipfile.ZipFile(path) as archive:
        content = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(content["package-manifest.json"])
    manifest[field] = value
    content["package-manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in content.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="package"):
        verify(path)


@pytest.mark.parametrize("suffix", ["..", "../outside", ".uv-cache/context"])
def test_output_paths_cannot_escape_or_claim_internal_build_storage(tmp_path, suffix):
    from advisor_deploy.package import build_package
    with pytest.raises(ValueError, match="output"):
        build_package("aca", tmp_path / "deploy" / "out" / suffix, repo_root=tmp_path)


def test_cli_config_never_prints_secret_values(config_case):
    path = config_case(GITHUB_TOKEN="fixture-private-token")
    result = subprocess.run(
        [sys.executable, "-m", "advisor_deploy", "config", "--target", "aca", "--env-file", str(path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "fixture-private-token" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["deployment_scope"] == "subscription"


def test_real_aca_build_is_reproducible_and_inspected_end_to_end(tmp_path, monkeypatch):
    import advisor_deploy.package as packaging
    from advisor_deploy.package import build_package
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the real locked-wheel build")
    live_root = Path(__file__).resolve().parents[3]
    # Other workers edit the live application. Freeze real inputs so the two
    # builds test reproducibility of identical sources, not concurrent changes.
    root = tmp_path / "snapshot"
    root.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(live_root / name, root / name)
    for member in ("shared", "agent", "channels/teams", "ingestion", "deploy/tools"):
        destination = root / member
        destination.mkdir(parents=True)
        shutil.copyfile(live_root / member / "pyproject.toml", destination / "pyproject.toml")
        if member in ("shared", "agent", "channels/teams"):
            shutil.copytree(live_root / member / "src", destination / "src",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".state", ".venv"))
    docker = root / "deploy" / "aca"
    docker.mkdir()
    for name in ("Dockerfile", "Dockerfile.dockerignore"):
        shutil.copyfile(live_root / "deploy" / "aca" / name, docker / name)
    # Windows keeps uv build interpreters mapped during pytest. Use the existing
    # persistent project cache, not an interpreter cache in a disposable fixture.
    # Commands still execute real uv builds against the frozen source workspace.
    run = packaging._run

    def shared_build_cache(args, *arguments, **keywords):
        if args[0] == "uv":
            args = args[:2] + ["--cache-dir", str(live_root / "deploy" / "out" / ".uv-cache")] + args[2:]
        return run(args, *arguments, **keywords)

    monkeypatch.setattr(packaging, "_run", shared_build_cache)
    approved = tmp_path / "approved"
    approved.mkdir()
    escalation = approved / "escalation.yaml"
    diagnostics = approved / "diagnostics.yaml"
    escalation.write_text("contacts: []\n", encoding="utf-8")
    diagnostics.write_text("checks: []\n", encoding="utf-8")
    output = root / "deploy" / "out" / "context"
    manifest = build_package(
        "aca", output, repo_root=root, escalation_config=escalation, diagnostics_config=diagnostics,
    )
    inspected = verify(output)
    assert manifest == inspected
    assert len(list((output / "wheels").glob("*.whl"))) == 3
    assert not (output / ".env").exists()
    assert not (output / "advisor_deploy").exists()
    assert (output / "runtime-config" / "escalation.yaml").read_bytes() == escalation.read_bytes()
    result = subprocess.run(
        [sys.executable, "-m", "advisor_deploy", "verify-package", str(output)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["files"] == manifest.files
    second = build_package(
        "aca", output, repo_root=root, escalation_config=escalation, diagnostics_config=diagnostics,
    )
    assert manifest.content_sha256 == second.content_sha256
    assert manifest.files == second.files


def test_powershell_wrapper_resolves_explicit_paths_from_the_caller(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None or shutil.which("uv") is None:
        pytest.skip("PowerShell and uv are required for the wrapper integration")
    root = Path(__file__).resolve().parents[3]
    (tmp_path / "escalation.yaml").write_text("contacts: []\n", encoding="utf-8")
    (tmp_path / "diagnostics.yaml").write_text("checks: []\n", encoding="utf-8")
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(root / "deploy" / "shared" / "Package.ps1"),
         "-Target", "aca", "-Output", "context", "-EscalationConfig", "escalation.yaml",
         "-DiagnosticsConfig", "diagnostics.yaml"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=240, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["target"] == "aca"
    assert verify(tmp_path / "context").target == "aca"


def test_validation_does_not_require_python312_path_methods(tmp_path, artifact_files, monkeypatch):
    path = write_zip(tmp_path / "portable.zip", artifact_files)
    monkeypatch.delattr(Path, "is_junction", raising=False)
    assert verify(path).target == "functions"


def test_child_build_process_does_not_receive_alternate_openai_credentials(tmp_path, monkeypatch):
    from advisor_deploy.package import _run
    names = ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN")
    for name in names:
        monkeypatch.setenv(name, "fixture-child-secret")
    root = Path(__file__).resolve().parents[3]
    code = f"import os,json; print(json.dumps({{name:os.environ.get(name) for name in {names!r}}}))"
    result = json.loads(_run([sys.executable, "-c", code], root, tmp_path))
    assert result == {name: None for name in names}


def test_installed_deployment_tools_preserve_python311_support():
    from importlib.metadata import metadata
    from packaging.specifiers import SpecifierSet
    assert SpecifierSet(metadata("advisor-deploy")["Requires-Python"]).contains("3.11")

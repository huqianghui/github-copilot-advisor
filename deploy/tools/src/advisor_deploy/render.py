"""Read-only plan/artifact verification and private ARM parameter rendering."""

from dataclasses import dataclass, field, replace
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import json
import os
import re
import subprocess
import tomllib

from advisor_deploy.config import DeployConfig, IGNORED_NAMES, SECRET_NAMES, load_deploy_config
from advisor_deploy.manifest import GROUPS, SUBSCRIPTION, fail, guid, read_json
from advisor_deploy.package import BENCHMARK_DATASET, ONLINE, PackageManifest, _directory_files, _zip_files, validate_package
from advisor_deploy.paths import has_link


def digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command_environment() -> dict[str, str]:
    return {
        name: value for name, value in os.environ.items()
        if not name.startswith(("OPENAI_", "AZURE_OPENAI_", "CONNECTIONS__", "UV_INDEX", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX"))
        and name not in SECRET_NAMES | IGNORED_NAMES
    }


def _normalized_requirement(value: str) -> tuple:
    requirement, _, marker = value.partition(";")
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[([A-Za-z0-9_,.-]+)\])?(.*)", requirement.strip())
    if not match:
        raise fail("invalid_package_metadata")
    return (
        match[1].lower().replace("_", "-"), tuple(sorted((match[2] or "").lower().split(","))),
        tuple(sorted(part.strip() for part in match[3].split(",") if part.strip())),
        re.sub(r"\s+", "", marker).replace("'", '"'),
    )


def _runtime_config(config: DeployConfig) -> DeployConfig:
    settings = dict(config.parameters["runtimeSettings"])
    environment = dict(config.runtime_environment)
    if config.parameters["benchmarkEnabled"]:
        prefixes = {"aca": "/app", "functions": "/home/site/wwwroot"}
        installed = prefixes[config.target] + "/" + BENCHMARK_DATASET
        settings["BENCHMARK_CASES_PATH"] = installed
        environment["BENCHMARK_CASES_PATH"] = installed
    else:
        settings.pop("BENCHMARK_CASES_PATH", None)
        environment.pop("BENCHMARK_CASES_PATH", None)
    return replace(config, parameters=config.parameters | {"runtimeSettings": settings}, runtime_environment=environment)


def verify_current_artifact(path: Path, repo: Path, config: DeployConfig) -> PackageManifest:
    manifest = validate_package(path)
    if config.parameters["benchmarkEnabled"] and BENCHMARK_DATASET not in manifest.files:
        raise fail("benchmark_dataset_missing")
    if manifest.target != config.target or manifest.lock_sha256 != sha256((repo / "uv.lock").read_bytes()).hexdigest():
        raise fail("artifact_lock_or_target_mismatch")
    files = _directory_files(path) if path.is_dir() else _zip_files(path)
    directories = {"advisor-shared": "shared", "advisor-agent": "agent", "advisor-teams": "channels/teams"}
    for wheel_name, content in files.items():
        if not wheel_name.startswith("wheels/"):
            continue
        wheel = _zip_files(BytesIO(content))
        metadata = BytesParser().parsebytes(next(data for name, data in wheel.items() if name.endswith(".dist-info/METADATA")))
        distribution = str(metadata["Name"]).lower().replace("_", "-")
        project_path = repo / directories[distribution]
        project = tomllib.loads((project_path / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        if str(metadata["Version"]) != project["version"] or str(metadata["Requires-Python"]) != project["requires-python"]:
            raise fail("artifact_project_metadata_mismatch")
        requirements = list(project.get("dependencies", []))
        for extra, dependencies in project.get("optional-dependencies", {}).items():
            requirements.extend(f'{dependency}; extra == "{extra}"' for dependency in dependencies)
        if {_normalized_requirement(value) for value in metadata.get_all("Requires-Dist", [])} != {
            _normalized_requirement(value) for value in requirements
        }:
            raise fail("artifact_dependency_metadata_mismatch")
        module = ONLINE[distribution]
        expected = {}
        for source in (project_path / "src" / module).rglob("*"):
            relative = source.relative_to(project_path / "src")
            if "__pycache__" in relative.parts or source.suffix == ".pyc" or any(part.startswith(".") for part in relative.parts):
                continue
            if has_link(source):
                raise fail("source_link_forbidden")
            if source.is_file():
                expected[relative.as_posix()] = source.read_bytes()
        actual = {name: data for name, data in wheel.items() if name.startswith(module + "/")}
        if actual != expected:
            raise fail("artifact_source_mismatch")
    for name, source in config.runtime_files.items():
        if files["runtime-config/" + name] != source.read_bytes():
            raise fail("artifact_runtime_configuration_mismatch")
    names = ("Dockerfile", "Dockerfile.dockerignore") if config.target == "aca" else ("function_app.py", "host.json")
    for name in names:
        if files[name] != (repo / "deploy" / config.target / name).read_bytes():
            raise fail("artifact_entry_mismatch")
    command = [
        "uv", "export", "--locked", "--offline", "--package", "advisor-teams", "--no-dev",
        "--no-editable", "--no-emit-workspace", "--emit-index-url", "--no-header", "--no-annotate",
    ]
    if config.target == "functions":
        command += ["--extra", "functions"]
    result = subprocess.run(command, cwd=repo, env=command_environment(), capture_output=True, text=True, encoding="utf-8", timeout=180)
    if result.returncode:
        raise fail("locked_dependency_export_failed")
    expected = result.stdout.replace("\r\n", "\n").rstrip() + "\n"
    for name in sorted(name for name in files if name.startswith("wheels/")):
        expected += f"./{name} --hash=sha256:{sha256(files[name]).hexdigest()}\n"
    if files["requirements.txt"] != expected.encode():
        raise fail("artifact_locked_requirements_mismatch")
    return manifest


@dataclass
class PreparedPlan:
    repository: Path
    run_id: str
    target: str
    env_file: Path = field(repr=False)
    artifact_path: Path
    manifest_path: Path
    config: DeployConfig = field(repr=False)
    artifact: PackageManifest
    capacity_evidence: dict | None = None
    benchmark_manifest: Path | None = None

    @property
    def group(self) -> str:
        return GROUPS[self.target]

    @property
    def configuration_digest(self) -> str:
        return digest({"parameters": self.config.parameters, "build_mode": self.config.build_mode})

    def summary(self) -> dict:
        parameters = self.config.parameters
        return {
            "location": "southeastasia", "profile": parameters.get("resourceProfile", "flex"),
            "memory_mb": parameters.get("instanceMemoryMB", 512),
            "max_instances": parameters["maxInstances"], "worker_parallelism": parameters["workerParallelism"],
            "min_replicas": parameters.get("minReplicas", 0),
            "openai_auth": parameters["openaiAuthMode"], "search_auth": parameters["searchAuthMode"],
        }


def load_plan(path: Path, repo: Path, target: str) -> PreparedPlan:
    data = read_json(path)
    required = {"format", "target", "run_id", "env_file", "artifact", "manifest"}
    if set(data) - required - {"overrides", "capacity_evidence", "benchmark_manifest"} or not required <= data.keys():
        raise fail("invalid_plan_fields")
    if data["format"] != "advisor-plan-v1" or data["target"] != target or target not in {"aca", "functions"}:
        raise fail("invalid_plan_target")
    run_id = guid(data["run_id"])
    overrides = data.get("overrides", {})
    if not isinstance(overrides, dict) or any(
        name in SECRET_NAMES | IGNORED_NAMES or name.startswith("CONNECTIONS__") for name in overrides
    ):
        raise fail("plan_must_not_contain_credentials")
    paths = {}
    for field in ("env_file", "artifact", "manifest"):
        if not isinstance(data[field], str):
            raise fail("invalid_plan_path")
        paths[field] = (path.parent / data[field]).absolute()
        if has_link(paths[field]):
            raise fail("plan_link_forbidden")
        paths[field] = paths[field].resolve()
    manifest_path = paths["manifest"]
    if not manifest_path.is_relative_to(repo / "deploy" / "out") or manifest_path.suffix != ".json":
        raise fail("manifest_output_scope")
    config = _runtime_config(load_deploy_config(paths["env_file"], target, overrides=overrides))
    if target == "aca" and not paths["artifact"].is_relative_to(repo / "deploy" / "out"):
        raise fail("aca_context_must_be_owned_output")
    artifact = verify_current_artifact(paths["artifact"], repo, config)
    if data.get("capacity_evidence") is not None and not isinstance(data["capacity_evidence"], dict):
        raise fail("invalid_capacity_evidence")
    benchmark = data.get("benchmark_manifest")
    if benchmark is not None and not isinstance(benchmark, str):
        raise fail("invalid_benchmark_manifest_path")
    benchmark_path = (path.parent / benchmark).absolute() if benchmark else None
    if benchmark_path is not None:
        if has_link(benchmark_path):
            raise fail("plan_link_forbidden")
        benchmark_path = benchmark_path.resolve()
    return PreparedPlan(repo, run_id, target, paths["env_file"], paths["artifact"], manifest_path, config, artifact,
                        data.get("capacity_evidence"), benchmark_path)


class PrivateFiles:
    """The PowerShell boundary creates/restricts the directory before Python writes."""

    def __init__(self, directory: Path, repo: Path):
        self.directory = directory.absolute()
        if has_link(self.directory) or self.directory.parent != repo / "deploy" / "out" or not re.fullmatch(r"\.private-[a-f0-9]{32}", self.directory.name):
            raise fail("private_directory_scope")
        checker = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(repo / "deploy" / "shared" / "Azure.ps1"),
             "-CheckPrivateDirectory", "-Directory", str(self.directory)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if checker.returncode or checker.stdout.strip() != '{"private_directory_valid":true}':
            raise fail("private_directory_permissions")

    def json(self, value: object) -> Path:
        path = self.directory / (uuid4().hex + ".json")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, separators=(",", ":"))
        return path


def parameters_file(plan: PreparedPlan, private: PrivateFiles, *, deploy_app: bool = False, image: str | None = None) -> Path:
    if plan.config.parameters["benchmarkEnabled"] and BENCHMARK_DATASET not in plan.artifact.files:
        raise fail("benchmark_dataset_missing")
    parameters = dict(_runtime_config(plan.config).parameters)
    if plan.target == "aca":
        parameters["deployApp"] = deploy_app
        if deploy_app:
            if not image or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}", image):
                raise fail("immutable_image_required")
            parameters["image"] = image
    else:
        parameters["packageIdentifier"] = plan.artifact.content_sha256
    parameters["secretSettings"] = plan.config.secret_settings.reveal_for_deployment()
    return private.json({
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {name: {"value": value} for name, value in parameters.items()},
    })

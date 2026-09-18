"""Isolated benchmark-only build contexts; the application package contract is unchanged."""
import argparse
from collections import defaultdict, deque
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tomllib
from uuid import uuid4

from advisor_deploy.bench.plan import RunPlan, load_cases, load_plan
from advisor_deploy.package import (
    _directory_files, _index_url, _run, _safe_name, _zip_files,
)
from advisor_deploy.paths import has_link
from advisor_shared.benchmark import DEFAULT_DATASET_PATH


ROOT = Path(__file__).resolve().parents[5]
WORKSPACE = {"advisor-shared": "advisor_shared", "advisor-deploy": "advisor_deploy"}
MANIFEST = "benchmark-manifest.json"
BASE_IMAGE = "python:3.12-slim"
HASH = re.compile(r"[a-f0-9]{64}")
FIXED = {"Dockerfile", ".dockerignore", "requirements.txt", "run.json", DEFAULT_DATASET_PATH}


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def validate_direct_dependencies(dependencies: list[str]) -> None:
    names = {re.split(r"[\[<>=!~ ;@]", value, maxsplit=1)[0].lower().replace("_", "-")
             for value in dependencies}
    if not {"advisor-shared", "aiohttp", "azure-storage-blob"} <= names:
        raise ValueError("package owner must declare direct shared/aiohttp/Blob dependencies")


def _snapshot(repo: Path) -> dict[str, str]:
    paths = [repo / name for name in (
        "uv.lock", "pyproject.toml", "shared/pyproject.toml", "deploy/tools/pyproject.toml")]
    for folder in (repo / "shared" / "src" / "advisor_shared",
                   repo / "deploy" / "tools" / "src" / "advisor_deploy"):
        paths.extend(path for path in folder.rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix != ".pyc")
    snapshot = {}
    for path in paths:
        if has_link(path):
            raise ValueError("build inputs cannot be links")
        name = path.relative_to(repo).as_posix()
        _safe_name(name)
        snapshot[name] = _digest(path.read_bytes())
    return snapshot


def _locked_registry(lock: dict) -> dict[tuple[str, str], set[str]]:
    packages = defaultdict(list)
    for package in lock["package"]:
        packages[package["name"]].append(package)
    queue = deque({"name": name} for name in WORKSPACE)
    visited = set()
    registry = {}
    locals_seen = set()
    while queue:
        dependency = queue.popleft()
        candidates = [item for item in packages[dependency["name"]]
                      if dependency.get("version", item["version"]) == item["version"]]
        if not candidates:
            raise ValueError("locked benchmark dependency is missing")
        for package in candidates:
            extras = tuple(dependency.get("extra", []))
            key = package["name"], package["version"], extras
            if key in visited:
                continue
            visited.add(key)
            source = package["source"]
            if "registry" in source:
                hashes = {item["hash"].removeprefix("sha256:")
                          for item in package.get("wheels", [])}
                if "sdist" in package:
                    hashes.add(package["sdist"]["hash"].removeprefix("sha256:"))
                if not hashes or any(not HASH.fullmatch(value) for value in hashes):
                    raise ValueError("locked registry dependency has no valid hashes")
                registry[package["name"], package["version"]] = hashes
            elif package["name"] in WORKSPACE:
                locals_seen.add(package["name"])
            else:
                raise ValueError("unapproved workspace/VCS dependency in benchmark closure")
            queue.extend(package.get("dependencies", []))
            for extra in extras:
                queue.extend(package.get("optional-dependencies", {}).get(extra, []))
    if locals_seen != set(WORKSPACE):
        raise ValueError("benchmark workspace roots missing from lock")
    return registry


def _wheel(filename: str, content: bytes) -> str:
    files = _zip_files(BytesIO(content))
    metadata_paths = [name for name in files if name.endswith(".dist-info/METADATA")]
    if len(metadata_paths) != 1:
        raise ValueError("benchmark wheel metadata is missing")
    metadata = BytesParser().parsebytes(files[metadata_paths[0]])
    name = str(metadata.get("Name", "")).lower().replace("_", "-")
    version = str(metadata.get("Version", ""))
    if name not in WORKSPACE or not re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
        raise ValueError("only shared and deploy workspace wheels are permitted")
    stem = name.replace("-", "_") + "-" + version
    dist_info = stem + ".dist-info"
    wheel = BytesParser().parsebytes(files.get(dist_info + "/WHEEL", b""))
    if (filename != stem + "-py3-none-any.whl"
            or metadata_paths != [dist_info + "/METADATA"]
            or wheel.get("Root-Is-Purelib") != "true" or wheel.get_all("Tag") != ["py3-none-any"]
            or WORKSPACE[name] + "/__init__.py" not in files
            or dist_info + "/RECORD" not in files):
        raise ValueError("benchmark wheels must be matching pure-Python distributions")
    for entry in files:
        if (entry.split("/")[0] not in {WORKSPACE[name], dist_info}
                or entry.lower().endswith((".pyd", ".dll", ".exe", ".pyc"))):
            raise ValueError("unexpected benchmark wheel content")
    return name


def _requirements(content: bytes, files: dict[str, bytes], index: str) -> list[dict]:
    references, registry = set(), []
    indexes = 0
    for raw in content.decode("utf-8").replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "--index-url " + index:
            indexes += 1
            continue
        hashes = re.findall(r"--hash=sha256:([a-f0-9]{64})(?=\s|$)", line)
        requirement = re.sub(r"\s+--hash=sha256:[a-f0-9]{64}(?=\s|$)", "", line).strip()
        if not hashes or "--" in requirement:
            raise ValueError("benchmark requirements must be locked and hashed")
        if requirement.startswith("./wheels/"):
            name = requirement[2:]
            if (name not in files or name in references or hashes != [_digest(files[name])]):
                raise ValueError("benchmark workspace wheel hash mismatch")
            references.add(name)
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)(?:\s*;\s*([A-Za-z0-9_.\"' =!<>()&|+-]+))?",
            requirement)
        if not match:
            raise ValueError("unlocked or direct-URL requirement is forbidden")
        name = match[1].lower().replace("_", "-")
        if name.startswith("advisor-") or name in {"pytest", "pytest-asyncio", "respx"}:
            raise ValueError("workspace or development dependency escaped benchmark closure")
        registry.append(dict(name=name, version=match[2], hashes=sorted(set(hashes)),
                             marker=match[3]))
    wheels = {name for name in files if name.startswith("wheels/")}
    if references != wheels or len(references) != 2 or indexes != 1:
        raise ValueError("requirements must cover exactly two workspace wheels and one index")
    return sorted(registry, key=lambda item: (item["name"], item["version"], item["marker"] or ""))


def _output(repo: Path, output: Path) -> Path:
    output = output.absolute()
    if has_link(output):
        raise ValueError("output links forbidden")
    try:
        relative = output.relative_to(repo / "deploy" / "out")
    except ValueError:
        raise ValueError("benchmark context must be inside this repository's deploy/out") from None
    if not relative.parts or any(part.startswith(".") or part == ".." for part in relative.parts):
        raise ValueError("output must be a named benchmark context")
    if any((parent / MANIFEST).exists() or (parent / "package-manifest.json").exists()
           for parent in output.parents):
        raise ValueError("cannot nest build contexts")
    if output.exists():
        verify_context(output)
    return output


def verify_context(context: Path) -> dict:
    if has_link(context):
        raise ValueError("context links forbidden")
    files = _directory_files(context)
    try:
        manifest = json.loads(files.pop(MANIFEST))
        expected_keys = {
            "format", "python_version", "platform", "lock_sha256", "source_sha256",
            "dependency_index", "registry_dependencies", "files", "uv_version",
            "base_image_reference", "base_image_digest", "linux_imports_verified", "image_id",
        }
        if (set(manifest) != expected_keys or manifest["format"] != "advisor-benchmark-context-v1"
                or manifest["python_version"] != "3.12" or manifest["platform"] != "linux/amd64"
                or not HASH.fullmatch(manifest["lock_sha256"])
                or type(manifest["linux_imports_verified"]) is not bool):
            raise ValueError("invalid benchmark context manifest")
        _index_url(manifest["dependency_index"])
        if set(files) != set(manifest["files"]):
            raise ValueError("unlisted benchmark context entry")
        for name, content in files.items():
            if _digest(content) != manifest["files"][name]:
                raise ValueError("benchmark context hash mismatch")
        wheels = {name for name in files if name.startswith("wheels/")}
        if set(files) != FIXED | wheels or len(wheels) != 2:
            raise ValueError("unexpected benchmark context contents")
        if {_wheel(PurePosixPath(name).name, files[name]) for name in wheels} != set(WORKSPACE):
            raise ValueError("benchmark context workspace distributions differ")
        requirements = _requirements(files["requirements.txt"], files, manifest["dependency_index"])
        if requirements != manifest["registry_dependencies"]:
            raise ValueError("benchmark dependency closure changed")
        plan = RunPlan.model_validate_json(files["run.json"], context={"historical": True})
        if plan.cases_path != DEFAULT_DATASET_PATH:
            raise ValueError("packaged dataset path must be runtime-config/benchmark-cases.json")
        load_cases(context.joinpath(*DEFAULT_DATASET_PATH.split("/")), plan)
        digest = manifest["base_image_digest"]
        if digest is None:
            if manifest["base_image_reference"] != BASE_IMAGE or manifest["linux_imports_verified"]:
                raise ValueError("unresolved benchmark base image")
        elif (not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
              or not re.fullmatch(r"(?:docker.io/library/)?python@" + digest,
                                  manifest["base_image_reference"])):
            raise ValueError("invalid pinned benchmark base image")
        if manifest["linux_imports_verified"] and not re.fullmatch(
            r"sha256:[a-f0-9]{64}", manifest["image_id"] or ""
        ):
            raise ValueError("verified benchmark image ID is missing")
        return manifest
    except (KeyError, TypeError, UnicodeError):
        raise ValueError("malformed benchmark context") from None


def build_context(plan_path: Path, output: Path, *, repo_root: Path | None = None) -> dict:
    repo = (repo_root or ROOT).resolve()
    output = _output(repo, Path(output))
    if has_link(plan_path):
        raise ValueError("plan links forbidden")
    plan = load_plan(plan_path)
    cases_path = repo / "deploy" / "bench" / "cases.json"
    if has_link(cases_path):
        raise ValueError("dataset links forbidden")
    load_cases(cases_path, plan)
    snapshot = _snapshot(repo)
    project = tomllib.loads((repo / "deploy" / "tools" / "pyproject.toml").read_text(encoding="utf-8"))
    validate_direct_dependencies(project["project"]["dependencies"])
    root = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    index = next(item["url"] for item in root["tool"]["uv"]["index"] if item.get("default"))
    _index_url(index)
    lock = tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8"))
    expected = _locked_registry(lock)
    work = repo / "deploy" / "out" / (".bench-work-" + uuid4().hex)
    work.mkdir(parents=True)
    guard_path = repo / "deploy" / "out" / (".bench-lock-" + _digest(str(output).encode())[:20])
    try:
        guard = guard_path.open("x")
    except FileExistsError:
        work.rmdir()
        raise ValueError("benchmark context build already in progress") from None
    try:
        with guard:
            wheels = work / "wheels"
            wheels.mkdir()
            requirements = _run([
                "uv", "export", "--locked", "--package", "advisor-deploy", "--no-dev",
                "--no-editable", "--no-emit-workspace", "--emit-index-url", "--no-header",
                "--no-annotate",
            ], repo, work).replace("\r\n", "\n").rstrip() + "\n"
            for name in WORKSPACE:
                _run(["uv", "build", "--package", name, "--wheel", "--out-dir", str(wheels)], repo, work)
            files = {}
            for path in sorted(wheels.glob("*.whl")):
                data = path.read_bytes()
                _wheel(path.name, data)
                name = "wheels/" + path.name
                files[name] = data
                requirements += f"./{name} --hash=sha256:{_digest(data)}\n"
            files["requirements.txt"] = requirements.encode()
            registry = _requirements(files["requirements.txt"], files, index)
            if ({(item["name"], item["version"]) for item in registry} != set(expected)
                    or any(set(item["hashes"]) != expected[item["name"], item["version"]]
                           for item in registry)):
                raise ValueError("exported benchmark dependencies differ from exact locked closure")
            files["Dockerfile"] = (repo / "deploy" / "bench" / "Dockerfile").read_bytes()
            files["run.json"] = json.dumps(
                plan.model_dump() | {"cases_path": DEFAULT_DATASET_PATH}, indent=2, allow_nan=False).encode()
            files[DEFAULT_DATASET_PATH] = cases_path.read_bytes()
            files[".dockerignore"] = (
                "**\n!Dockerfile\n!.dockerignore\n!requirements.txt\n!run.json\n"
                "!runtime-config/\n!" + DEFAULT_DATASET_PATH + "\n!wheels/\n"
                + "".join(f"!{name}\n" for name in sorted(files) if name.startswith("wheels/"))
            ).encode()
            if _snapshot(repo) != snapshot:
                raise ValueError("benchmark sources or lock changed during packaging")
            manifest = dict(
                format="advisor-benchmark-context-v1", python_version="3.12", platform="linux/amd64",
                lock_sha256=snapshot["uv.lock"], source_sha256=snapshot, dependency_index=index,
                registry_dependencies=registry, files={name: _digest(data) for name, data in files.items()},
                uv_version=_run(["uv", "--version"], repo, work).strip(),
                base_image_reference=BASE_IMAGE, base_image_digest=None,
                linux_imports_verified=False, image_id=None)
            files[MANIFEST] = json.dumps(manifest, indent=2, allow_nan=False).encode()
            stage = work / "context"
            for name, data in files.items():
                destination = stage.joinpath(*name.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            verify_context(stage)
            _output(repo, output)
            output.parent.mkdir(parents=True, exist_ok=True)
            backup = work / "previous"
            if output.exists():
                output.rename(backup)
            try:
                stage.rename(output)
            except OSError:
                if backup.exists():
                    backup.rename(output)
                raise
            return manifest
    finally:
        shutil.rmtree(work)
        guard_path.unlink(missing_ok=True)


def build_image(context: Path, tag: str, *, repo_root: Path | None = None,
                base_image: str | None = None, docker_context: str | None = None) -> dict:
    repo = (repo_root or ROOT).resolve()
    context = _output(repo, context)
    manifest = verify_context(context)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_.-]+", tag):
        raise ValueError("invalid local benchmark image tag")
    if base_image is not None and not re.fullmatch(
        r"(?:docker.io/library/)?python@sha256:[a-f0-9]{64}", base_image
    ):
        raise ValueError("explicit benchmark base image must be digest-pinned official Python")
    work = repo / "deploy" / "out" / (".bench-image-" + uuid4().hex)
    work.mkdir()
    try:
        selected = docker_context or _run(["docker", "context", "show"], repo, work, timeout=30).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", selected):
            raise ValueError("invalid Docker context")
        endpoint = _run([
            "docker", "context", "inspect", selected, "--format", "{{.Endpoints.docker.Host}}",
        ], repo, work, timeout=30).strip()
        if not endpoint.startswith(("npipe://", "unix://")):
            raise ValueError("benchmark build requires a local Docker engine; remote uploads forbidden")
        docker = ["docker", "--context", selected]
        if _run(docker + ["info", "--format", "{{.OSType}}"], repo, work, timeout=30).strip() != "linux":
            raise ValueError("benchmark image verification requires a running Linux engine")
        reference = base_image or BASE_IMAGE
        _run(docker + ["pull", "--platform", "linux/amd64", reference], repo, work, timeout=300)
        if base_image is None:
            reference = _run(docker + [
                "image", "inspect", BASE_IMAGE, "--format", "{{index .RepoDigests 0}}",
            ], repo, work, timeout=30).strip()
        if not re.fullmatch(r"(?:docker.io/library/)?python@sha256:[a-f0-9]{64}", reference):
            raise ValueError("Docker did not resolve an official Python base digest")
        manifest.update(base_image_reference=reference, base_image_digest=reference.split("@")[1],
                        linux_imports_verified=False, image_id=None)
        (context / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        verify_context(context)
        _run(docker + [
            "build", "--platform", "linux/amd64", "--build-arg", "PYTHON_IMAGE=" + reference,
            "--tag", tag, "--file", str(context / "Dockerfile"), str(context),
        ], repo, work, timeout=900)
        probe = (
            "import importlib,importlib.util,importlib.metadata,platform;"
            "assert platform.system()=='Linux';assert platform.machine()=='x86_64';"
            "[importlib.import_module(m) for m in ['advisor_shared.benchmark',"
            "'advisor_shared.entra_api','advisor_deploy.bench.__main__',"
            "'advisor_deploy.bench.sink','advisor_deploy.bench.build',"
            "'aiohttp','azure.storage.blob.aio']];"
            "assert {d.metadata['Name'].lower().replace('_','-') for d in "
            "importlib.metadata.distributions() if d.metadata['Name'].startswith('advisor-')}"
            "=={'advisor-shared','advisor-deploy'};"
            "assert all(importlib.util.find_spec(m) is None for m in "
            "['advisor_agent','teams_adapter','advisor_ingestion']);"
            "print('benchmark-only Linux imports verified; no cloud/model calls')"
        )
        _run(docker + [
            "run", "--rm", "--network", "none", "--platform", "linux/amd64",
            "--entrypoint", "python", tag, "-I", "-c", probe,
        ], repo, work, timeout=120)
        image_id = _run(docker + ["image", "inspect", tag, "--format", "{{.Id}}"],
                        repo, work, timeout=30).strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ValueError("verified local benchmark image ID missing")
        manifest.update(linux_imports_verified=True, image_id=image_id)
        (context / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return verify_context(context)
    finally:
        shutil.rmtree(work)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Locked isolated benchmark build context")
    commands = parser.add_subparsers(dest="command", required=True)
    context = commands.add_parser("context")
    context.add_argument("--plan", type=Path, required=True)
    context.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("context", type=Path)
    image = commands.add_parser("image")
    image.add_argument("context", type=Path)
    image.add_argument("--tag", required=True)
    image.add_argument("--base-image")
    image.add_argument("--docker-context")
    args = parser.parse_args(argv)
    try:
        if args.command == "context":
            result = build_context(args.plan, args.output)
        elif args.command == "image":
            result = build_image(args.context, args.tag, base_image=args.base_image,
                                 docker_context=args.docker_context)
        else:
            result = verify_context(args.context)
        print(json.dumps({key: result[key] for key in (
            "format", "lock_sha256", "base_image_digest", "linux_imports_verified", "image_id")}))
        return 0
    except (ValueError, OSError) as error:
        print(f"Benchmark build stopped ({type(error).__name__}): {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

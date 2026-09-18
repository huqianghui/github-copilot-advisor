"""Build and inspect bounded, hash-checked online artifacts without extracting ZIPs."""

from dataclasses import dataclass, replace
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import uuid4
import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
import zipfile

from advisor_deploy.config import approved_file
from advisor_deploy.paths import has_link, is_link


ONLINE = {"advisor-shared": "advisor_shared", "advisor-agent": "advisor_agent", "advisor-teams": "teams_adapter"}
MANIFEST = "package-manifest.json"
FORMAT = "advisor-package-v1"
BASE_IMAGE = "python:3.12-slim"
MAX_FILE = 128 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
FORBIDDEN = {".env", ".venv", "venv", ".state", ".git", "__pycache__", "tests", "test", "dev", "ingestion", "advisor_ingestion"}
HASH = re.compile(r"[0-9a-f]{64}")


def _bad(message: str) -> ValueError:
    return ValueError("invalid package: " + message)


def _digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def _safe_name(name: str) -> None:
    if (
        not name or "\\" in name or ":" in name or "\x00" in name
        or name.startswith("/") or name.endswith("/") or "//" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or any(part.lower() in FORBIDDEN or part.lower().startswith(".env.") for part in name.split("/"))
    ):
        raise _bad("forbidden or unsafe entry")


def _json(content: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _bad("duplicate JSON field")
            result[key] = value
        return result
    try:
        result = json.loads(content, object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise _bad("JSON object required")
        return result
    except (UnicodeError, json.JSONDecodeError):
        raise _bad("malformed JSON") from None


def _zip_files(source: Path | BytesIO) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    names: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > 10000:
                raise _bad("too many ZIP entries")
            for info in archive.infolist():
                _safe_name(info.filename)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in {0, stat.S_IFREG} or info.flag_bits & 1:
                    raise _bad("ZIP links, special files or encryption forbidden")
                if info.filename.casefold() in names:
                    raise _bad("duplicate ZIP entry")
                names.add(info.filename.casefold())
                total += info.file_size
                if info.file_size > MAX_FILE or total > MAX_TOTAL:
                    raise _bad("ZIP size limit")
                files[info.filename] = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise _bad("unreadable ZIP") from None
    return files


def _directory_files(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    folded: set[str] = set()
    total = 0
    for item in path.rglob("*"):
        name = item.relative_to(path).as_posix()
        _safe_name(name)
        if is_link(item):
            raise _bad("filesystem links forbidden")
        if item.is_dir():
            continue
        if not item.is_file():
            raise _bad("special file forbidden")
        if name.casefold() in folded:
            raise _bad("duplicate entry")
        folded.add(name.casefold())
        size = item.stat().st_size
        total += size
        if size > MAX_FILE or total > MAX_TOTAL:
            raise _bad("artifact size limit")
        files[name] = item.read_bytes()
    return files


@dataclass(frozen=True)
class PackageManifest:
    target: str
    files: dict[str, str]
    lock_sha256: str
    dependency_index: str
    python_version: str = "3.12"
    platform: str = "linux/amd64"
    deployment_scope: str = "subscription"
    base_image: str | None = None
    base_image_digest: str | None = None
    linux_imports_verified: bool = False
    format: str = FORMAT

    @classmethod
    def create(cls, *, target: str, files: dict[str, bytes], lock_sha256: str, dependency_index: str):
        return cls(
            target=target, files={name: _digest(data) for name, data in sorted(files.items())},
            lock_sha256=lock_sha256, dependency_index=dependency_index,
            base_image=BASE_IMAGE if target == "aca" else None,
        )

    def to_dict(self) -> dict:
        return {
            "format": self.format, "target": self.target, "files": dict(self.files),
            "lock_sha256": self.lock_sha256, "dependency_index": self.dependency_index,
            "python_version": self.python_version, "platform": self.platform,
            "deployment_scope": self.deployment_scope, "base_image": self.base_image,
            "base_image_digest": self.base_image_digest, "linux_imports_verified": self.linux_imports_verified,
        }

    @property
    def content_sha256(self) -> str:
        return _digest(_manifest_bytes(self))


def _manifest_bytes(manifest: PackageManifest) -> bytes:
    return (json.dumps(manifest.to_dict(), sort_keys=True, indent=2) + "\n").encode()


def _index_url(value: str) -> None:
    from urllib.parse import urlsplit
    if not isinstance(value, str):
        raise _bad("dependency index must be credential-free HTTPS")
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.port == 0
            or any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value
        ):
            raise _bad("dependency index must be credential-free HTTPS")
    except ValueError:
        raise _bad("dependency index must be credential-free HTTPS") from None


def _wheel(filename: str, data: bytes) -> str:
    if not filename.endswith("-py3-none-any.whl"):
        raise _bad("online wheels must be platform-independent Python wheels")
    files = _zip_files(BytesIO(data))
    metadata_paths = [name for name in files if name.endswith(".dist-info/METADATA")]
    if len(metadata_paths) != 1:
        raise _bad("wheel metadata missing")
    metadata = BytesParser().parsebytes(files[metadata_paths[0]])
    name = str(metadata.get("Name", "")).lower().replace("_", "-")
    version = str(metadata.get("Version", ""))
    if name not in ONLINE or not re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
        raise _bad("only the three online workspace distributions are permitted")
    dist_info = name.replace("-", "_") + "-" + version + ".dist-info"
    if metadata_paths[0] != dist_info + "/METADATA" or filename != f"{name.replace('-', '_')}-{version}-py3-none-any.whl":
        raise _bad("wheel filename/metadata mismatch")
    wheel_metadata = BytesParser().parsebytes(files.get(dist_info + "/WHEEL", b""))
    if wheel_metadata.get("Root-Is-Purelib") != "true" or wheel_metadata.get_all("Tag") != ["py3-none-any"]:
        raise _bad("wheel is not Linux-compatible pure Python")
    module = ONLINE[name]
    if module + "/__init__.py" not in files or dist_info + "/RECORD" not in files:
        raise _bad("wheel module or RECORD missing")
    if name == "advisor-teams" and module + "/__main__.py" not in files:
        raise _bad("Teams entry module missing")
    for entry in files:
        if entry.split("/")[0] not in {module, dist_info}:
            raise _bad("unexpected wheel content")
        if entry.lower().endswith((".pyd", ".exe", ".dll", ".pyc")):
            raise _bad("Windows or compiled local artifacts forbidden")
    return name


def _requirements(data: bytes, files: dict[str, bytes], index: str, target: str) -> None:
    try:
        physical = data.decode("utf-8").splitlines()
    except UnicodeError:
        raise _bad("requirements encoding") from None
    lines, pending = [], ""
    for line in physical:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
        else:
            lines.append(pending + line)
            pending = ""
    if pending:
        raise _bad("unterminated requirement")
    references: set[str] = set()
    remote_names: set[str] = set()
    indexes = 0
    for line in lines:
        if line.startswith("--index-url "):
            url = line[len("--index-url "):]
            _index_url(url)
            if url != index:
                raise _bad("unexpected dependency index")
            indexes += 1
            continue
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})(?=\s|$)", line)
        requirement = re.sub(r"\s+--hash=sha256:[0-9a-f]{64}(?=\s|$)", "", line).strip()
        if not hashes or "--" in requirement:
            raise _bad("requirements must be pinned and hashed")
        if requirement.startswith("./wheels/"):
            entry = requirement[2:]
            _safe_name(entry)
            if entry not in files or entry in references or hashes != [_digest(files[entry])]:
                raise _bad("local wheel hash or reference mismatch")
            references.add(entry)
        else:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)(?:\s*;\s*[A-Za-z0-9_.\"' =!<>()&|+-]+)?", requirement)
            if not match:
                raise _bad("only locked registry dependencies are permitted")
            name = match[1].lower().replace("_", "-")
            if name in {*ONLINE, "advisor-deploy", "advisor-ingestion", "pytest", "pytest-asyncio", "respx"}:
                raise _bad("development or workspace dependency escaped the online closure")
            remote_names.add(name)
    wheels = {name for name in files if name.startswith("wheels/")}
    if references != wheels or len(references) != 3 or indexes != 1:
        raise _bad("requirements do not cover exactly the online wheels and one index")
    if target == "functions" and "azure-functions" not in remote_names:
        raise _bad("Functions dependency missing from locked requirements")


def validate_package(path: Path) -> PackageManifest:
    path = Path(path)
    if has_link(path):
        raise _bad("artifact links forbidden")
    try:
        files = _directory_files(path) if path.is_dir() else _zip_files(path)
        raw = _json(files.pop(MANIFEST))
        if set(raw) != set(PackageManifest.create(target="aca", files={}, lock_sha256="", dependency_index="").to_dict()):
            raise _bad("unknown manifest fields")
        if any(not isinstance(raw[name], str) for name in (
            "format", "target", "python_version", "platform", "deployment_scope", "lock_sha256", "dependency_index"
        )):
            raise _bad("invalid manifest field type")
        if any(raw[name] is not None and not isinstance(raw[name], str) for name in ("base_image", "base_image_digest")):
            raise _bad("invalid image manifest field type")
        manifest = PackageManifest(**raw)
    except (KeyError, TypeError, OSError):
        raise _bad("manifest missing or malformed") from None
    if (
        manifest.format != FORMAT or manifest.target not in {"aca", "functions"}
        or manifest.python_version != "3.12" or manifest.platform != "linux/amd64"
        or manifest.deployment_scope != "subscription"
        or not isinstance(manifest.files, dict) or not isinstance(manifest.lock_sha256, str)
        or not HASH.fullmatch(manifest.lock_sha256) or type(manifest.linux_imports_verified) is not bool
    ):
        raise _bad("unsupported manifest contract")
    if manifest.target == "aca":
        if manifest.base_image != BASE_IMAGE or not path.is_dir():
            raise _bad("ACA requires a staged build context")
    elif manifest.base_image is not None or path.is_dir() or manifest.linux_imports_verified:
        raise _bad("Functions requires a ZIP, without image assertions")
    if manifest.base_image_digest is not None and (
        manifest.target != "aca" or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.base_image_digest)
    ):
        raise _bad("invalid base image digest")
    if manifest.linux_imports_verified and manifest.base_image_digest is None:
        raise _bad("verified image requires a resolved base digest")
    _index_url(manifest.dependency_index)
    if set(files) != set(manifest.files):
        raise _bad("manifest entry mismatch")
    for name, expected in manifest.files.items():
        _safe_name(name)
        if not isinstance(expected, str) or not HASH.fullmatch(expected) or _digest(files[name]) != expected:
            raise _bad("payload hash mismatch")
    required = {"requirements.txt", "runtime-config/escalation.yaml", "runtime-config/diagnostics.yaml"}
    required |= {"Dockerfile", "Dockerfile.dockerignore"} if manifest.target == "aca" else {"function_app.py", "host.json"}
    wheels = {name for name in files if name.startswith("wheels/") and name.endswith(".whl")}
    if set(files) != required | wheels or len(wheels) != 3:
        raise _bad("unexpected or missing deployment entries")
    distributions = {_wheel(PurePosixPath(name).name, files[name]) for name in wheels}
    if distributions != set(ONLINE):
        raise _bad("online workspace module missing or duplicated")
    _requirements(files["requirements.txt"], files, manifest.dependency_index, manifest.target)
    if manifest.target == "functions" and _json(files["host.json"]).get("version") != "2.0":
        raise _bad("Functions host.json version must be 2.0")
    if any(not files[name].strip() for name in required):
        raise _bad("empty required file")
    return manifest


def _repository() -> Path:
    root = Path(__file__).resolve().parents[4]
    if not (root / "uv.lock").is_file():
        raise _bad("repository root must be supplied")
    return root


def _output_path(repo: Path, output: Path, target: str) -> Path:
    output = output.absolute()
    allowed = repo / "deploy" / "out"
    if has_link(output):
        raise _bad("output links forbidden")
    try:
        relative = output.relative_to(allowed)
    except ValueError:
        raise _bad("output must be inside this repository's deploy/out") from None
    if not relative.parts or any(part.startswith(".") or part == ".." for part in relative.parts):
        raise _bad("output must be a named artifact, not the output root")
    if target == "functions" and output.suffix != ".zip":
        raise _bad("Functions output must end with .zip")
    if any((parent / MANIFEST).exists() for parent in output.parents if parent != output):
        raise _bad("output cannot be nested inside another artifact")
    if output.exists():
        try:
            previous = validate_package(output)
        except ValueError:
            raise _bad("output is not an owned valid package; refusing to replace it") from None
        if previous.target != target:
            raise _bad("output belongs to another target")
    return output


def _run(args: list[str], repo: Path, work: Path, *, timeout: int = 300) -> str:
    scratch = work / "scratch"
    scratch.mkdir(exist_ok=True)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AZURE_", "OPENAI_", "ADVISOR_", "BENCHMARK_", "CONNECTIONS__", "PIP_", "UV_INDEX", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX"))
        and key not in {"GITHUB_TOKEN", "TAVILY_API_KEY", "BRAVE_API_KEY", "UV_NO_VERIFY_HASHES"}
    }
    environment.update({
        "UV_CACHE_DIR": str(repo / "deploy" / "out" / ".uv-cache"),
        "TEMP": str(scratch), "TMP": str(scratch), "TMPDIR": str(scratch),
        "SOURCE_DATE_EPOCH": "315532800", "PYTHONDONTWRITEBYTECODE": "1",
    })
    try:
        result = subprocess.run(
            args, cwd=repo, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _bad(f"{args[0]} {args[1]} unavailable or timed out") from None
    if result.returncode:
        # Tool stderr can include URLs and user paths; never log or rethrow it.
        raise _bad(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout


def _write_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def build_package(
    target: str, output: Path, *, repo_root: Path | None = None,
    escalation_config: Path | None = None, diagnostics_config: Path | None = None,
) -> PackageManifest:
    if target not in {"aca", "functions"}:
        raise _bad("unknown target")
    repo = (repo_root or _repository()).resolve()
    output = _output_path(repo, Path(output), target)
    entry_files = ["function_app.py", "host.json"] if target == "functions" else ["Dockerfile", "Dockerfile.dockerignore"]
    source_directory = repo / "deploy" / ("functions" if target == "functions" else "aca")
    for name in entry_files:
        if not (source_directory / name).is_file() or has_link(source_directory / name):
            raise _bad(f"required entry {name} missing; implement the entry task before packaging")
    if escalation_config is None or diagnostics_config is None:
        raise _bad("explicit approved escalation/diagnostics YAML paths are required")
    configs = {
        "runtime-config/escalation.yaml": approved_file(escalation_config),
        "runtime-config/diagnostics.yaml": approved_file(diagnostics_config),
    }
    try:
        lock_bytes = (repo / "uv.lock").read_bytes()
        lock = tomllib.loads(lock_bytes.decode())
        teams = tomllib.loads((repo / "channels" / "teams" / "pyproject.toml").read_text(encoding="utf-8"))
        root = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
        indexes = root["tool"]["uv"]["index"]
        index = next(item["url"] for item in indexes if item.get("default"))
    except (OSError, ValueError, KeyError, StopIteration):
        raise _bad("workspace lock or index configuration missing") from None
    _index_url(index)
    if target == "functions" and "functions" not in teams["project"].get("optional-dependencies", {}):
        raise _bad("advisor-teams functions extra missing; implement the Functions entry task first")
    if {package["name"] for package in lock["package"] if package["name"] in ONLINE} != set(ONLINE):
        raise _bad("online workspace packages missing from lock")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = repo / "deploy" / "out" / (".package-work-" + uuid4().hex)
    lock_path = repo / "deploy" / "out" / (".package-lock-" + _digest(str(output).encode())[:20])
    try:
        guard = lock_path.open("x")
    except FileExistsError:
        raise _bad("output build already in progress") from None
    try:
        with guard:
            work.mkdir()
            stage = work / "context"
            stage.mkdir()
            wheels = work / "built-wheels"
            wheels.mkdir()
            command = [
                "uv", "export", "--locked", "--package", "advisor-teams", "--no-dev",
                "--no-editable", "--no-emit-workspace", "--emit-index-url", "--no-header", "--no-annotate",
            ]
            if target == "functions":
                command.extend(["--extra", "functions"])
            requirements = _run(command, repo, work).replace("\r\n", "\n").rstrip() + "\n"
            for name in ONLINE:
                _run(["uv", "build", "--package", name, "--wheel", "--out-dir", str(wheels)], repo, work)
            files = {name: (source_directory / name).read_bytes() for name in entry_files}
            files.update({name: path.read_bytes() for name, path in configs.items()})
            for wheel in sorted(wheels.glob("*.whl")):
                content = wheel.read_bytes()
                _wheel(wheel.name, content)
                name = "wheels/" + wheel.name
                files[name] = content
                requirements += f"./{name} --hash=sha256:{_digest(content)}\n"
            files["requirements.txt"] = requirements.encode()
            if (repo / "uv.lock").read_bytes() != lock_bytes:
                raise _bad("lock changed while building")
            manifest = PackageManifest.create(
                target=target, files=files, lock_sha256=_digest(lock_bytes), dependency_index=index,
            )
            files[MANIFEST] = _manifest_bytes(manifest)
            for name, content in files.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            candidate = work / "package.zip" if target == "functions" else stage
            if target == "functions":
                _write_zip(candidate, files)
            validate_package(candidate)
            # Recheck ownership immediately before publishing the fully validated candidate.
            _output_path(repo, output, target)
            backup = work / "previous"
            if output.exists():
                output.rename(backup)
            try:
                candidate.rename(output)
            except OSError:
                if backup.exists():
                    backup.rename(output)
                raise _bad("could not publish artifact") from None
            return manifest
    finally:
        if work.exists():
            shutil.rmtree(work)
        lock_path.unlink(missing_ok=True)


def build_image(context: Path, tag: str, *, repo_root: Path | None = None) -> PackageManifest:
    """Optional local build: resolve and record the digest, then verify offline imports."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_.-]+", tag):
        raise _bad("invalid local image tag")
    manifest = validate_package(context)
    if manifest.target != "aca":
        raise _bad("only ACA contexts build images")
    repo = (repo_root or _repository()).resolve()
    context = _output_path(repo, context, "aca")
    work = repo / "deploy" / "out" / (".image-work-" + uuid4().hex)
    work.mkdir()
    try:
        _run(["docker", "info", "--format", "{{.OSType}}"], repo, work, timeout=30)
        _run(["docker", "pull", "--platform", "linux/amd64", BASE_IMAGE], repo, work, timeout=300)
        reference = _run(["docker", "image", "inspect", BASE_IMAGE, "--format", "{{index .RepoDigests 0}}"], repo, work).strip()
        if not re.fullmatch(r"(?:docker.io/library/)?python@sha256:[0-9a-f]{64}", reference):
            raise _bad("base image did not resolve to a digest")
        manifest = replace(manifest, base_image_digest=reference.split("@")[1], linux_imports_verified=False)
        (context / MANIFEST).write_bytes(_manifest_bytes(manifest))
        _run([
            "docker", "build", "--platform", "linux/amd64", "--build-arg", "BASE_IMAGE=" + reference,
            "--tag", tag, "--file", str(context / "Dockerfile"), str(context),
        ], repo, work, timeout=900)
        probe = (
            "import advisor_shared,advisor_agent.factory,teams_adapter.__main__; "
            "from advisor_agent.factory import build_advisor\n"
            "try:\n build_advisor(channel_name='teams')\n"
            "except ValueError:\n print('online imports pass; missing configuration rejected')\n"
            "else:\n raise AssertionError('missing configuration was accepted')\n"
        )
        _run(["docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64", "--entrypoint", "python", tag, "-c", probe], repo, work)
        manifest = replace(manifest, linux_imports_verified=True)
        (context / MANIFEST).write_bytes(_manifest_bytes(manifest))
        return validate_package(context)
    finally:
        shutil.rmtree(work)

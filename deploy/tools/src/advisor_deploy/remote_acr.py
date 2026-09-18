"""Sealed ACR quick tasks: resolve the base, build, test without networking, push."""

import base64
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from advisor_deploy.manifest import fail, read_json
from advisor_deploy.package import _directory_files, _safe_name
from advisor_deploy.paths import has_link
from advisor_deploy.render import digest


_HASH = re.compile(r"[a-f0-9]{64}")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_PROOF = "ADVISOR_ACR_PROOF:"
TASK_ACTIONS = [
    "Microsoft.ContainerRegistry/registries/scheduleRun/action",
    "Microsoft.ContainerRegistry/registries/listBuildSourceUploadUrl/action",
    "Microsoft.ContainerRegistry/registries/runs/read",
    "Microsoft.ContainerRegistry/registries/runs/listLogSasUrl/action",
    "Microsoft.ContainerRegistry/registries/pull/read",
]
_COMMANDS = {
    "aca": ["python", "-m", "teams_adapter"],
    "bench": ["python", "-m", "advisor_deploy.bench", "run", "--plan", "/app/run.json",
              "--output", "/app/results", "--allow-cloud"],
}


def evidence_path(manifest: Path) -> Path:
    return manifest.with_name(manifest.stem + ".acr.json")


def _owned_path(path, repo):
    return (path.is_absolute() and ".." not in path.parts
            and path.is_relative_to(repo / "deploy" / "out") and not has_link(path))


def _payload(context, files, kind, repo):
    if kind not in _COMMANDS or not _owned_path(context, repo):
        raise fail("acr_context_scope")
    fixed = {"Dockerfile", "requirements.txt"}
    if kind == "aca":
        fixed |= {"Dockerfile.dockerignore", "runtime-config/escalation.yaml", "runtime-config/diagnostics.yaml"}
        optional = {"runtime-config/benchmark-cases.json"}
        distributions = {"advisor_shared", "advisor_agent", "advisor_teams"}
        manifest = "package-manifest.json"
    else:
        fixed |= {".dockerignore", "run.json", "runtime-config/benchmark-cases.json"}
        optional = set()
        distributions = {"advisor_shared", "advisor_deploy"}
        manifest = "benchmark-manifest.json"
    wheels = {name for name in files if re.fullmatch(r"wheels/[a-z_]+-[A-Za-z0-9_.+-]+-py3-none-any\.whl", name)}
    if ({Path(name).name.split("-")[0] for name in wheels} != distributions
            or len(wheels) != len(distributions) or not fixed | wheels <= set(files)
            or set(files) - fixed - optional - wheels):
        raise fail("acr_context_allowlist")
    actual = _directory_files(context)
    if set(actual) != set(files) | {manifest}:
        raise fail("acr_context_extra_files")
    for name, expected in files.items():
        _safe_name(name)
        if not isinstance(expected, str) or not _HASH.fullmatch(expected) or sha256(actual[name]).hexdigest() != expected:
            raise fail("acr_context_hash_mismatch")
    return {name: actual[name] for name in files}


def _inspect(logs):
    """Extract the one complete docker inspect response, not arbitrary log text."""
    found = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\s*\[", logs):
        try:
            value, _ = decoder.raw_decode(logs[match.start():].lstrip())
        except ValueError:
            continue
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and "Id" in value[0]:
            found.append(value[0])
    if len(found) != 1 or found[0].get("Os") != "linux" or found[0].get("Architecture") != "amd64":
        raise fail("acr_image_inspect_unverified")
    return found[0]


def _quick_run(cli, registry, work, task):
    raw = (json.dumps(task, sort_keys=True, indent=2) + "\n").encode()
    (work / "task.yaml").write_bytes(raw)
    queued = cli.run([
        "acr", "run", "--registry", registry, "--file", "task.yaml", "--platform", "linux/amd64",
        "--timeout", "1200", "--no-logs", str(work),
    ], "acr_schedule", mutation=True)
    run_id = queued.get("runId")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9]{1,64}", run_id):
        raise fail("acr_run_id_unverified")
    deadline = time.monotonic() + 1320
    while True:
        run = cli.run(["acr", "task", "show-run", "--registry", registry, "--run-id", run_id], "acr_run_readback")
        if run.get("runId") != run_id:
            raise fail("acr_run_id_mismatch")
        status = run.get("status")
        if status == "Succeeded":
            break
        if status not in {"Queued", "Started", "Running"}:
            raise fail("acr_run_failed_" + run_id)
        if time.monotonic() >= deadline:
            raise fail("acr_run_timeout_" + run_id)
        time.sleep(5)
    if run.get("platform", {}).get("os") != "linux" or run.get("platform", {}).get("architecture") != "amd64":
        raise fail("acr_run_platform_mismatch")
    logs = cli.run(["acr", "task", "logs", "--registry", registry, "--run-id", run_id, "--no-format"],
                   "acr_run_logs", raw=True)
    if not isinstance(logs, str) or len(logs) > 8 * 1024 * 1024:
        raise fail("acr_run_logs_unverified")
    return run, logs, sha256(raw).hexdigest()


def _probe(kind, files, contract):
    source = (
        "import hashlib,importlib,importlib.metadata,importlib.util,json,os,pathlib,platform\n"
        f"CONTRACT = {json.dumps(contract, sort_keys=True)}\n"
        "assert platform.system() == 'Linux' and platform.machine() == 'x86_64'\n"
        "assert os.getuid() == 10001\n"
        f"FILES = {files!r}\n"
        "assert all(hashlib.sha256(pathlib.Path('/app', n).read_bytes()).hexdigest() == h for n,h in FILES.items())\n"
    )
    if kind == "aca":
        source += (
            "import advisor_shared,advisor_agent.factory,teams_adapter.__main__\n"
            "from advisor_agent.factory import build_advisor\n"
            "try:\n build_advisor(channel_name='teams')\n"
            "except ValueError as error:\n assert 'AZURE_SEARCH_ENDPOINT' in str(error)\n"
            "else:\n raise AssertionError('missing configuration accepted')\n"
        )
    else:
        source += (
            "[importlib.import_module(m) for m in ['advisor_shared.benchmark','advisor_shared.entra_api',"
            "'advisor_deploy.bench.__main__','advisor_deploy.bench.sink','aiohttp','azure.storage.blob.aio']]\n"
            "assert {d.metadata['Name'].lower().replace('_','-') for d in importlib.metadata.distributions()"
            " if d.metadata['Name'].startswith('advisor-')} == {'advisor-shared','advisor-deploy'}\n"
            "assert all(importlib.util.find_spec(m) is None for m in ['advisor_agent','teams_adapter','advisor_ingestion'])\n"
            "from advisor_deploy.bench.plan import RunPlan\n"
            "from pydantic import ValidationError\n"
            "try:\n RunPlan.model_validate({})\n"
            "except ValidationError:\n pass\n"
            "else:\n raise AssertionError('missing benchmark configuration accepted')\n"
        )
    source += (
        "CONTRACT['run_id'] = os.environ['ADVISOR_ACR_RUN_ID']\n"
        f"print({_PROOF!r} + json.dumps(CONTRACT,sort_keys=True),flush=True)\n"
    )
    return source.encode()


def _write_evidence(path, value, repo):
    if not _owned_path(path, repo) or path.suffix != ".json":
        raise fail("acr_evidence_scope")
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid4().hex + ".next")
    try:
        with pending.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def publish_image(cli, *, context, files, binding, registry, kind, evidence_path):
    """Called only after ownership, artifact/current-source and Apply approval gates."""
    if cli.mode != "Apply":
        raise fail("acr_mutation_requires_apply")
    if not re.fullmatch(r"[a-z0-9]{5,50}", registry):
        raise fail("acr_registry_name")
    if (not isinstance(binding, dict) or not binding
            or any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in binding.values())):
        raise fail("acr_binding_invalid")
    if not _owned_path(evidence_path, cli.repo) or evidence_path.suffix != ".json":
        raise fail("acr_evidence_scope")
    payload = _payload(context, files, kind, cli.repo)
    work = cli.repo / "deploy" / "out" / (".acr-" + uuid4().hex)
    work.mkdir()
    try:
        base_run, base_logs, base_task_hash = _quick_run(cli, registry, work, {
            "version": "v1.1.0", "steps": [
                {"id": "resolve", "cmd": "docker pull --platform linux/amd64 python:3.12-slim", "timeout": 300},
                {"id": "inspect", "cmd": "docker image inspect python:3.12-slim", "when": ["resolve"], "timeout": 60},
            ],
        })
        base = _inspect(base_logs)
        references = base.get("RepoDigests", [])
        references = [r for r in references if isinstance(r, str)
                      and re.fullmatch(r"(?:docker.io/library/)?python@sha256:[a-f0-9]{64}", r)]
        if len(references) != 1:
            raise fail("acr_base_digest_unverified")
        reference = references[0]
        for name, content in payload.items():
            destination = work / "src" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        repository = "advisor" if kind == "aca" else "advisor-bench"
        server = registry + ".azurecr.io"
        tag = "cloud-" + digest(binding)[:16] + "-{{.Run.ID}}"
        image = server + "/" + repository + ":" + tag
        contract = {"binding_sha256": digest(binding), "files_sha256": digest(files),
                    "base_image_digest": reference.split("@")[1], "kind": kind, "command": _COMMANDS[kind]}
        image_files = {n: h for n, h in files.items() if n not in {"Dockerfile", ".dockerignore", "Dockerfile.dockerignore"}}
        probe = _probe(kind, image_files, contract)
        encoded = base64.b64encode(probe).decode()
        argument = "BASE_IMAGE" if kind == "aca" else "PYTHON_IMAGE"
        task = {"version": "v1.1.0", "steps": [
            {"id": "build", "build": f"--platform linux/amd64 --build-arg {argument}={reference} "
             f"--label io.advisor.binding={digest(binding)} --label io.advisor.files={digest(files)} "
             f"-t {image} -f src/Dockerfile src", "timeout": 900},
            {"id": "probe", "cmd": image + ' -I -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"',
             "entryPoint": "python", "when": ["build"], "network": "none", "pull": False,
             "workingDirectory": "/app", "disableWorkingDirectoryOverride": True,
             "env": ["ADVISOR_ACR_RUN_ID={{.Run.ID}}"], "timeout": 120},
            {"id": "inspect", "cmd": "docker image inspect " + image, "when": ["probe"], "timeout": 60},
            {"id": "push", "push": [image], "when": ["inspect"], "timeout": 120},
        ]}
        # A separate copy, never the ARM-private directory or the repository root.
        run, logs, task_hash = _quick_run(cli, registry, work, task)
        matches = re.findall(r"(?m)^" + _PROOF + r"(\{[^\r\n]*\})\s*$", logs)
        try:
            actual_proof = json.loads(matches[0]) if len(matches) == 1 else None
        except ValueError:
            actual_proof = None
        if actual_proof != contract | {"run_id": run["runId"]}:
            raise fail("acr_linux_probe_unverified")
        image_info = _inspect(logs)
        config = image_info.get("Config", {})
        labels = config.get("Labels", {})
        image_id = image_info.get("Id", "")
        if (not _DIGEST.fullmatch(image_id) or config.get("User") != "10001" or config.get("Cmd") != _COMMANDS[kind]
                or labels.get("io.advisor.binding") != digest(binding) or labels.get("io.advisor.files") != digest(files)):
            raise fail("acr_image_binding_unverified")
        outputs = run.get("outputImages", [])
        if (not isinstance(outputs, list) or len(outputs) != 1 or outputs[0].get("registry") != server
                or outputs[0].get("repository") != repository
                or outputs[0].get("tag") != tag.replace("{{.Run.ID}}", run["runId"])
                or not _DIGEST.fullmatch(outputs[0].get("digest", ""))):
            raise fail("acr_run_output_unverified")
        immutable = outputs[0]["digest"]
        readback = cli.run(["acr", "repository", "show", "--name", registry, "--image", repository + "@" + immutable],
                           "acr_immutable_readback")
        if readback.get("digest") != immutable:
            raise fail("acr_immutable_readback_mismatch")
        # Detect concurrent changes even though only the earlier sealed snapshot was uploaded.
        if _payload(context, files, kind, cli.repo) != payload:
            raise fail("acr_context_changed")
        result = {
            "format": "advisor-acr-publication-v1", "kind": kind, "registry": registry,
            "run_id": run["runId"], "run_status": "Succeeded", "base_run_id": base_run["runId"],
            "base_task_sha256": base_task_hash, "task_sha256": task_hash, "probe_sha256": sha256(probe).hexdigest(),
            "logs_sha256": sha256(logs.encode()).hexdigest(), "files_sha256": digest(files),
            "binding": binding, "image_id": image_id, "image_reference": server + "/" + repository + "@" + immutable,
            "base_image_digest": reference.split("@")[1], "linux_imports_verified": True,
        }
        _write_evidence(evidence_path, result, cli.repo)
        return result
    finally:
        shutil.rmtree(work)


def verify_evidence(cli, path, *, kind, files, binding, image_reference, image_id, registry):
    if not _owned_path(path, cli.repo):
        raise fail("acr_evidence_scope")
    value = read_json(path)
    if (value.get("format") != "advisor-acr-publication-v1" or value.get("kind") != kind
            or value.get("binding") != binding or value.get("files_sha256") != digest(files)
            or value.get("registry") != registry or value.get("image_reference") != image_reference
            or value.get("image_id") != image_id or value.get("linux_imports_verified") is not True
            or value.get("run_status") != "Succeeded"
            or not re.fullmatch(r"[a-z0-9]{1,64}", value.get("run_id", ""))):
        raise fail("acr_evidence_binding_mismatch")
    repository = "advisor" if kind == "aca" else "advisor-bench"
    server = registry + ".azurecr.io"
    if not re.fullmatch(re.escape(server + "/" + repository + "@") + r"sha256:[a-f0-9]{64}", image_reference):
        raise fail("acr_evidence_image_mismatch")
    run_id = value["run_id"]
    run = cli.run(["acr", "task", "show-run", "--registry", registry, "--run-id", run_id], "acr_evidence_run")
    outputs = run.get("outputImages", [])
    if (run.get("runId") != run_id or run.get("status") != "Succeeded" or len(outputs) != 1
            or any(outputs[0].get(k) != v for k, v in {
                "registry": server, "repository": repository, "tag": "cloud-" + digest(binding)[:16] + "-" + run_id,
                "digest": image_reference.split("@")[1],
            }.items())):
        raise fail("acr_evidence_run_mismatch")
    logs = cli.run(["acr", "task", "logs", "--registry", registry, "--run-id", run_id, "--no-format"],
                   "acr_evidence_logs", raw=True)
    if not isinstance(logs, str) or sha256(logs.encode()).hexdigest() != value.get("logs_sha256"):
        raise fail("acr_evidence_logs_mismatch")
    matches = re.findall(r"(?m)^" + _PROOF + r"(\{[^\r\n]*\})\s*$", logs)
    try:
        proof = json.loads(matches[0]) if len(matches) == 1 else None
    except ValueError:
        proof = None
    if proof != {"binding_sha256": digest(binding), "files_sha256": digest(files),
                 "base_image_digest": value.get("base_image_digest"), "kind": kind,
                 "command": _COMMANDS[kind], "run_id": run_id}:
        raise fail("acr_evidence_probe_unverified")
    inspected = _inspect(logs)
    config = inspected.get("Config", {})
    if (inspected.get("Id") != image_id or config.get("User") != "10001" or config.get("Cmd") != _COMMANDS[kind]
            or config.get("Labels", {}).get("io.advisor.binding") != digest(binding)
            or config.get("Labels", {}).get("io.advisor.files") != digest(files)):
        raise fail("acr_evidence_image_mismatch")
    remote = cli.run(["acr", "repository", "show", "--name", registry,
                      "--image", repository + "@" + image_reference.split("@")[1]], "acr_evidence_registry")
    if remote.get("digest") != image_reference.split("@")[1]:
        raise fail("acr_evidence_registry_mismatch")
    return value

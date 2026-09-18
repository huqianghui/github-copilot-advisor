"""ACR quick-run contracts with an isolated CLI double; never contacts Azure."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


BASE = "sha256:" + "b" * 64
IMAGE = "sha256:" + "c" * 64
PUBLISHED = "sha256:" + "d" * 64


class RemoteCli:
    def __init__(self, repo, *, mode="Apply", defect=None):
        self.repo, self.mode, self.defect = repo, mode, defect
        self.calls, self.uploads, self.runs = [], [], {}

    def run(self, args, phase, **kwargs):
        assert kwargs.get("tool", "az") == "az", "cloud build must never invoke local Docker"
        self.calls.append((args, kwargs))
        if args[:2] == ["acr", "run"]:
            if "--no-wait" in args:
                from advisor_deploy.operations import CliFailure
                raise CliFailure("acr_schedule", "empty_response")
            assert kwargs["mutation"] is True and self.mode == "Apply"
            work = Path(args[-1])
            uploaded = {p.relative_to(work).as_posix(): p.read_bytes() for p in work.rglob("*") if p.is_file()}
            self.uploads.append(uploaded)
            task = json.loads(uploaded["task.yaml"])
            run_id = "ca" + str(len(self.runs) + 1)
            self.runs[run_id] = task
            return {"runId": run_id, "status": "Queued"}
        if args[:3] == ["acr", "task", "show-run"]:
            run_id = args[args.index("--run-id") + 1]
            task = self.runs[run_id]
            build = next((s for s in task["steps"] if "build" in s), None)
            images = []
            if build:
                tag = task["steps"][-1]["push"][0].replace("{{.Run.ID}}", run_id)
                images = [{"registry": tag.split("/")[0], "repository": tag.split("/")[1].split(":")[0],
                           "tag": tag.split(":")[1], "digest": PUBLISHED}]
            return {"runId": run_id, "status": "Failed" if build and self.defect == "probe-failed" else "Succeeded",
                    "platform": {"os": "linux", "architecture": "amd64"},
                    "outputImages": images if self.defect != "missing-output" else []}
        if args[:3] == ["acr", "task", "logs"]:
            run_id = args[args.index("--run-id") + 1]
            task = self.runs[run_id]
            probe = next((s for s in task["steps"] if s["id"] == "probe"), None)
            if not probe:
                return json.dumps([{"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "amd64",
                                    "RepoDigests": ["python@" + BASE]}])
            import base64
            source = base64.b64decode(probe["cmd"].split("b64decode('")[1].split("'")[0]).decode()
            constants = json.loads(source.split("CONTRACT = ", 1)[1].split("\n", 1)[0])
            contract = deepcopy(constants)
            contract["run_id"] = run_id
            if self.defect == "wrong-proof":
                contract["binding_sha256"] = "f" * 64
            proof = "" if self.defect == "missing-proof" else "ADVISOR_ACR_PROOF:" + json.dumps(contract) + "\n"
            inspect = [{"Id": IMAGE, "Os": "linux", "Architecture": "amd64", "Config": {
                "User": "10001", "Cmd": constants["command"], "Labels": {
                    "io.advisor.binding": constants["binding_sha256"],
                    "io.advisor.files": constants["files_sha256"],
                }}}]
            return proof + json.dumps(inspect)
        if args[:3] == ["acr", "repository", "show"]:
            return {"digest": "sha256:" + "e" * 64 if self.defect == "readback-drift" else PUBLISHED}
        raise AssertionError((args, kwargs))


def context_case(tmp_path, kind):
    out = tmp_path / "deploy" / "out"
    context = out / "context"
    context.mkdir(parents=True)
    names = ["Dockerfile", "requirements.txt"]
    if kind == "aca":
        names += ["Dockerfile.dockerignore", "runtime-config/escalation.yaml", "runtime-config/diagnostics.yaml"]
        distributions = ["advisor_shared", "advisor_agent", "advisor_teams"]
    else:
        names += [".dockerignore", "run.json", "runtime-config/benchmark-cases.json"]
        distributions = ["advisor_shared", "advisor_deploy"]
    names += ["wheels/" + d + "-0.1.0-py3-none-any.whl" for d in distributions]
    files = {}
    for name in names:
        path = context / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (name + "\n").encode()
        path.write_bytes(content)
        files[name] = sha256(content).hexdigest()
    manifest = "package-manifest.json" if kind == "aca" else "benchmark-manifest.json"
    (context / manifest).write_text("{}")
    binding = {"artifact_sha256": "a" * 64, "lock_sha256": "b" * 64}
    return SimpleNamespace(context=context, files=files, binding=binding, evidence=out / "image.acr.json")


@pytest.mark.parametrize("kind", ["aca", "bench"])
def test_cloud_build_uploads_only_sealed_payload_and_tests_before_push(tmp_path, kind):
    from advisor_deploy.remote_acr import publish_image
    case = context_case(tmp_path, kind)
    (tmp_path / "deploy" / "out" / "private-parameters.json").write_text('{"secret":"do-not-upload"}')
    cli = RemoteCli(tmp_path)
    result = publish_image(cli, context=case.context, files=case.files, binding=case.binding,
                           registry="owned", kind=kind, evidence_path=case.evidence)
    assert result["image_reference"] == "owned.azurecr.io/" + ("advisor" if kind == "aca" else "advisor-bench") + "@" + PUBLISHED
    assert result["image_id"] == IMAGE and result["base_image_digest"] == BASE
    assert result["linux_imports_verified"] is True and result["run_id"] == "ca2"
    assert result["binding"] == case.binding
    assert json.loads(case.evidence.read_text()) == result
    assert not list((tmp_path / "deploy" / "out").glob(".acr-*"))
    assert len(cli.uploads) == 2
    assert set(cli.uploads[0]) == {"task.yaml"}
    assert set(cli.uploads[1]) == {"task.yaml"} | {"src/" + n for n in case.files}
    task = cli.runs["ca2"]
    steps = {s["id"]: s for s in task["steps"]}
    assert steps["probe"]["when"] == ["build"] and steps["inspect"]["when"] == ["probe"]
    assert steps["push"]["when"] == ["inspect"]
    assert all(not s.get("ignoreErrors") and not s.get("detach") for s in task["steps"])
    # The actual acr-builder graph.Step schema declares Network as string;
    # a network-definition object here is rejected before any step executes.
    assert steps["probe"]["network"] == "none"
    assert "python@" + BASE in steps["build"]["build"]
    assert b"do-not-upload" not in b"".join(cli.uploads[1].values())
    assert all("--no-wait" not in args and "--no-logs" in args for args, _ in cli.calls if args[:2] == ["acr", "run"])


@pytest.mark.parametrize("defect", ["probe-failed", "missing-proof", "wrong-proof", "missing-output", "readback-drift"])
def test_remote_success_requires_probe_and_bound_immutable_readback(tmp_path, defect):
    from advisor_deploy.remote_acr import publish_image
    case = context_case(tmp_path, "aca")
    cli = RemoteCli(tmp_path, defect=defect)
    with pytest.raises(ValueError, match="acr_"):
        publish_image(cli, context=case.context, files=case.files, binding=case.binding,
                      registry="owned", kind="aca", evidence_path=case.evidence)
    assert not case.evidence.exists()
    assert not list((tmp_path / "deploy" / "out").glob(".acr-*"))


@pytest.mark.parametrize("defect", ["whatif", "extra-secret", "changed-wheel", "unsafe-name", "evidence-traversal"])
def test_cloud_build_rejects_unsafe_or_unapproved_inputs_before_upload(tmp_path, defect):
    from advisor_deploy.remote_acr import publish_image
    case = context_case(tmp_path, "aca")
    cli = RemoteCli(tmp_path, mode="WhatIf" if defect == "whatif" else "Apply")
    if defect == "extra-secret":
        (case.context / ".env").write_text("SECRET=do-not-upload")
    elif defect == "changed-wheel":
        next((case.context / "wheels").iterdir()).write_text("changed")
    elif defect == "unsafe-name":
        case.files["../private-parameters.json"] = "c" * 64
    elif defect == "evidence-traversal":
        case.evidence = tmp_path / "deploy" / "out" / ".." / ".." / "unapproved.json"
    with pytest.raises(ValueError):
        publish_image(cli, context=case.context, files=case.files, binding=case.binding,
                      registry="owned", kind="aca", evidence_path=case.evidence)
    assert not cli.calls


def test_remote_pending_run_is_bounded_and_never_sets_proof(tmp_path, monkeypatch):
    from advisor_deploy import remote_acr
    case = context_case(tmp_path, "aca")

    class Pending(RemoteCli):
        def run(self, args, phase, **kwargs):
            if args[:3] == ["acr", "task", "show-run"]:
                return {"runId": "ca1", "status": "Running"}
            return super().run(args, phase, **kwargs)

    ticks = iter([0, 1321])
    monkeypatch.setattr(remote_acr.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ValueError, match="acr_run_timeout_ca1"):
        remote_acr.publish_image(Pending(tmp_path), context=case.context, files=case.files, binding=case.binding,
                                 registry="owned", kind="aca", evidence_path=case.evidence)
    assert not case.evidence.exists()


@pytest.mark.parametrize("tamper", [None, "binding", "image", "run", "proof", "forged-build-only"])
def test_later_app_stage_rechecks_stored_cloud_evidence_and_live_run(tmp_path, tamper):
    from advisor_deploy.remote_acr import publish_image, verify_evidence
    case = context_case(tmp_path, "bench")
    cli = RemoteCli(tmp_path)
    proof = publish_image(cli, context=case.context, files=case.files, binding=case.binding,
                          registry="owned", kind="bench", evidence_path=case.evidence)
    binding = dict(case.binding)
    image = proof["image_reference"]
    if tamper == "binding":
        binding["artifact_sha256"] = "f" * 64
    elif tamper == "image":
        image = image.replace(PUBLISHED, "sha256:" + "f" * 64)
    elif tamper == "run":
        cli.defect = "missing-output"
    elif tamper in {"proof", "forged-build-only"}:
        cli.defect = "missing-proof"
        if tamper == "forged-build-only":
            logs = cli.run(["acr", "task", "logs", "--run-id", proof["run_id"]], "test", raw=True)
            proof["logs_sha256"] = sha256(logs.encode()).hexdigest()
            case.evidence.write_text(json.dumps(proof))
    kwargs = dict(kind="bench", files=case.files, binding=binding, image_reference=image,
                  image_id=proof["image_id"], registry="owned")
    if tamper:
        with pytest.raises(ValueError, match="acr_"):
            verify_evidence(cli, case.evidence, **kwargs)
    else:
        assert verify_evidence(cli, case.evidence, **kwargs) == proof

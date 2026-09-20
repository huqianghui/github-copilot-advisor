"""Reversible closure never deletes state, changes other settings, or assumes max0."""

from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from advisor_deploy.config import SUBSCRIPTION, TENANT
from advisor_deploy.manifest import group_id, new_manifest, write_manifest
from advisor_deploy.render import digest


@pytest.fixture
def close_case(tmp_path, monkeypatch):
    from advisor_deploy.bench import build
    from advisor_deploy import remote_acr
    repo = tmp_path
    out = repo / "deploy" / "out"
    out.mkdir(parents=True)
    group = group_id("aca")
    target_id = group + "/providers/Microsoft.App/containerApps/ca-advisor-fixture"
    observer_id = group + "/providers/Microsoft.App/containerApps/ca-bench-fixture"
    identity = group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/target"
    driver = group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/driver"
    storage = group + "/providers/Microsoft.Storage/storageAccounts/stfixture"
    registry = group + "/providers/Microsoft.ContainerRegistry/registries/fixture"
    target_image = "fixture.azurecr.io/advisor@sha256:" + "a" * 64
    observer_image = "fixture.azurecr.io/advisor-bench@sha256:" + "b" * 64
    ledger = new_manifest("aca", str(uuid4()), "fixture", "c" * 64, "d" * 64)
    ledger.update(status="deployed", tenant_id=TENANT, image_reference=target_image)
    ledger["owned_resource_ids"] = [group, target_id, observer_id, identity, driver, storage, registry]
    ledger["outputs"] = {
        "target": "aca", "resource_group": ledger["resource_group"], "app_id": target_id,
        "app_name": "ca-advisor-fixture", "endpoint": "https://target.example.net",
        "storage_id": storage, "blob_url": "https://stfixture.blob.core.windows.net/",
        "queue_url": "https://stfixture.queue.core.windows.net/", "identity_id": identity,
        "identity_client_id": str(uuid4()), "identity_principal_id": str(uuid4()),
        "bot_id": "", "created_role_assignment_ids": [],
    }
    context = out / "context"
    context.mkdir()
    run = {"expected_target_id": target_id, "deployment": {"artifact_sha256": ledger["artifact_digest"],
            "config_sha256": ledger["config_digest"], "valid_until": "2020-01-01T00:00:00Z"}}
    (context / "run.json").write_text(json.dumps(run))
    context_manifest = {"files": {"run.json": "e" * 64}, "source_sha256": {"old_source.py": "f" * 64},
                        "lock_sha256": "0" * 64}
    state = {"format": "advisor-benchmark-deployment-v1", "run_id": ledger["run_id"], "phase": "app_deployed",
             "planned_app_id": observer_id, "planned_app_name": "ca-bench-fixture",
             "identity_id": driver, "image_reference": observer_image, "image_id": "sha256:" + "1" * 64,
             "context_digest": digest(context_manifest), "run_plan_digest": digest(run),
             "resource_ids": [observer_id, driver], "role_assignment_ids": []}
    ledger_path, state_path = out / "aca.json", out / "observer.json"
    write_manifest(ledger_path, ledger, repo)
    state_path.write_text(json.dumps(state))
    plan_path = out / "close.json"
    plan_path.write_text(json.dumps({"format": "advisor-benchmark-close-v1", "aca_manifest": "aca.json",
                                    "observer_state": "observer.json", "observer_context": "context"}))
    monkeypatch.setattr(build, "verify_context", lambda path: context_manifest)
    monkeypatch.setattr(build, "_snapshot", lambda *a: pytest.fail("Closure must not rebuild a deployed artifact"))
    proof_calls = []
    monkeypatch.setattr(remote_acr, "verify_evidence", lambda *a, **k: proof_calls.append(k))
    apps = {}
    for identifier, name, image, mid, minimum in (
        (target_id, "advisor", target_image, identity, 0), (observer_id, "driver-receiver", observer_image, driver, 1),
    ):
        env = [{"name": "BENCHMARK_ENABLED", "value": "true"}, {"name": "MODEL_KEY", "secretRef": "model"},
               {"name": "KEEP", "value": "unchanged"}]
        if minimum:
            env += [{"name": "BENCHMARK_AUTOSTART", "value": "false"}]
        apps[identifier] = {"id": identifier, "name": identifier.rsplit("/", 1)[1],
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {mid: {}}},
            "properties": {"provisioningState": "Succeeded", "environmentId": group + "/providers/Microsoft.App/managedEnvironments/env",
                "latestRevisionName": identifier.rsplit("/", 1)[1] + "--one",
                "configuration": {"activeRevisionsMode": "Single", "secrets": [{"name": "model"}],
                                  "ingress": {"external": True, "allowInsecure": False, "targetPort": 8080 if minimum else 3978,
                                              "fqdn": "observer.example.net" if minimum else "target.example.net"}},
                "template": {"containers": [{"name": name, "image": image, "env": env,
                                            "resources": {"cpu": .5 if minimum else .25, "memory": "1Gi" if minimum else "0.5Gi"}}],
                             "scale": {"minReplicas": minimum, "maxReplicas": 1 if minimum else 40,
                                       "pollingInterval": 30, "cooldownPeriod": 300}}}}
    revisions = {identifier: [{"id": identifier + "/revisions/" + app["properties"]["latestRevisionName"],
                               "name": app["properties"]["latestRevisionName"], "properties": {
                                   "active": True, "runningState": "Running" if identifier == observer_id else "ScaledToZero",
                                   "provisioningState": "Provisioned", "template": deepcopy(app["properties"]["template"])}}]
                 for identifier, app in apps.items()}
    clock = SimpleNamespace(value=0.)

    class Commands:
        mode = "WhatIf"
        calls = []
        defect = ""
        drain_reads = 0
        stale_app = None

        def run(self, args, phase, **options):
            self.calls.append((args, phase, options))
            if args[:2] == ["account", "show"]:
                return {"id": SUBSCRIPTION, "tenantId": TENANT}
            if args[:2] == ["group", "exists"]:
                return True
            if args[:2] == ["group", "show"]:
                return {"tags": {"advisorDeploymentId": ledger["run_id"]}}
            if args[:2] == ["resource", "list"]:
                return [{"id": x, "type": "/".join(x.split("/providers/")[1].split("/")[:2])} for x in ledger["owned_resource_ids"][1:]]
            if args[:2] == ["containerapp", "show"]:
                if self.stale_app is not None and args[args.index("--name") + 1] == apps[target_id]["name"]:
                    old, self.stale_app = self.stale_app, None
                    return old
                return deepcopy(next(v for v in apps.values() if v["name"] == args[args.index("--name") + 1]))
            if args[:2] == ["containerapp", "update"]:
                assert options["mutation"] and args[args.index("--name") + 1] == apps[target_id]["name"]
                assert args[args.index("--set-env-vars") + 1] == "BENCHMARK_ENABLED=false"
                if self.defect == "stale-read":
                    self.stale_app = deepcopy(apps[target_id])
                env = apps[target_id]["properties"]["template"]["containers"][0]["env"]
                next(v for v in env if v["name"] == "BENCHMARK_ENABLED")["value"] = "false"
                if self.defect == "env-drift":
                    env[:] = [v for v in env if v["name"] != "MODEL_KEY"]
                revisions[target_id][0]["properties"]["template"] = deepcopy(apps[target_id]["properties"]["template"])
                if self.defect == "target-pending":
                    apps[target_id]["properties"]["provisioningState"] = "Updating"
                return None
            if args[:3] == ["containerapp", "ingress", "disable"]:
                assert options["mutation"] and args[args.index("--name") + 1] == apps[observer_id]["name"]
                apps[observer_id]["properties"]["configuration"]["ingress"] = None
                return None
            if args[:3] == ["containerapp", "revision", "deactivate"]:
                assert options["mutation"] and args[args.index("--name") + 1] == apps[observer_id]["name"]
                name = args[args.index("--revision") + 1]
                assert name in [r["name"] for r in revisions[observer_id]]
                for row in revisions[observer_id]:
                    row["properties"]["active"] = False
                    row["properties"]["runningState"] = "Deactivating" if self.defect == "drain" else "Stopped"
                return None
            raise AssertionError((args, phase))

        def rest(self, method, url, phase, **options):
            assert method == "get"
            self.calls.append(([method, url], phase, options))
            if "/permissions?" in url:
                return {"value": [{"actions": ["*"], "notActions": []}]}
            if "/queueServices/default/queues/" in url:
                return {"properties": {"approximateMessageCount": 1 if self.defect == "queue" else 0}}
            identifier = url.removeprefix("https://management.azure.com").split("?")[0]
            if identifier.endswith("/revisions"):
                app = identifier[:-len("/revisions")]
                if app == observer_id and self.defect == "drain" and not revisions[app][0]["properties"]["active"]:
                    self.drain_reads += 1
                    if self.drain_reads > 1:
                        revisions[app][0]["properties"]["runningState"] = "Stopped"
                return {"value": deepcopy(revisions[app]), "nextLink": "https://unrelated.invalid" if self.defect == "pagination" else None}
            if identifier.endswith("/replicas"):
                app = identifier.split("/revisions/")[0]
                active = any(r["properties"]["active"] for r in revisions[app])
                if app == target_id:
                    return {"value": [{"id": "live"}] if self.defect in {"target-live", "probe-wakeup"} else []}
                return {"value": [{"id": "observer-replica", "properties": {"containers": [
                    {"name": "driver-receiver", "started": self.defect == "driver-live", "ready": False, "restartCount": 6},
                    {"name": "http-auth", "started": True, "ready": True, "restartCount": 0}]}}] if active else []}
            raise AssertionError((method, url, phase))

    directory = out / "private"
    directory.mkdir()

    def private_json(value):
        path = directory / (uuid4().hex + ".json")
        path.write_text(json.dumps(value))
        return path

    return SimpleNamespace(repo=repo, out=out, plan=plan_path, aca_path=ledger_path, state_path=state_path,
                           ledger=ledger, state=state, apps=apps, revisions=revisions, cli=Commands(),
                           target=target_id, observer=observer_id, clock=clock, proof_calls=proof_calls,
                           private=SimpleNamespace(directory=directory, json=private_json))


def configure(monkeypatch, case):
    from advisor_deploy import benchmark_close
    monkeypatch.setattr(benchmark_close.time, "monotonic", lambda: case.clock.value)
    monkeypatch.setattr(benchmark_close.time, "sleep", lambda seconds: setattr(case.clock, "value", case.clock.value + seconds))
    monkeypatch.setattr(benchmark_close, "_probe_access", lambda *args, **kwargs: {"benchmark": 404, "teams": 401})
    return benchmark_close


def test_closure_preview_is_exact_read_only_and_historical_provenance_is_checked(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    before = c.aca_path.read_bytes(), c.state_path.read_bytes()
    report = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    assert report["mode"] == "WhatIf" and report["quiescence_verified"]
    assert report["actions"]["target_benchmark_enabled"] is False
    assert report["actions"]["observer_ingress_enabled"] is False
    assert report["actions"]["observer_revisions_to_deactivate"] == [c.revisions[c.observer][0]["name"]]
    assert c.proof_calls and c.clock.value >= 30
    assert (c.aca_path.read_bytes(), c.state_path.read_bytes()) == before
    assert not any(k.get("mutation") for _, _, k in c.cli.calls)


def test_closure_applies_only_feature_ingress_and_owned_revision_changes(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    original = deepcopy(c.apps)
    preview = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    c.cli.mode = "Apply"
    result = module.close_benchmark(c.plan, c.repo, c.cli, c.private, preview["confirmation"])
    assert result["status"] == "closed" and result["observer_active_revisions"] == []
    assert result["observer_replicas"] == result["target_replicas"] == 0
    assert c.apps[c.target]["properties"]["configuration"] == original[c.target]["properties"]["configuration"]
    assert c.apps[c.target]["properties"]["template"]["scale"] == original[c.target]["properties"]["template"]["scale"]
    assert c.apps[c.observer]["properties"]["template"] == original[c.observer]["properties"]["template"]
    env = c.apps[c.target]["properties"]["template"]["containers"][0]["env"]
    assert next(v for v in env if v["name"] == "MODEL_KEY") == {"name": "MODEL_KEY", "secretRef": "model"}
    assert json.loads(c.state_path.read_text())["phase"] == "suspended"
    assert json.loads(c.aca_path.read_text())["config_digest"] == c.ledger["config_digest"]
    mutations = [args for args, _, kwargs in c.cli.calls if kwargs.get("mutation")]
    assert len(mutations) == 3
    assert not any(word in args for args in mutations for word in ("delete", "start", "--max-replicas", "--replace-env-vars"))
    assert all(0 < kwargs["timeout"] <= 1200 for _, _, kwargs in c.cli.calls if kwargs.get("mutation"))


@pytest.mark.parametrize("defect", ["queue", "target-live", "driver-live", "pagination"])
def test_unsafe_or_incomplete_quiescence_never_mutates_or_discards_state(close_case, monkeypatch, defect):
    c = close_case
    module = configure(monkeypatch, c)
    c.cli.defect = defect
    before = c.state_path.read_bytes()
    with pytest.raises(ValueError):
        module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    assert c.state_path.read_bytes() == before
    assert not any(kwargs.get("mutation") for _, _, kwargs in c.cli.calls)


def test_wrong_confirmation_never_mutates(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    c.cli.mode = "Apply"
    with pytest.raises(ValueError, match="closure_confirmation"):
        module.close_benchmark(c.plan, c.repo, c.cli, c.private, "wrong")
    assert not any(kwargs.get("mutation") for _, _, kwargs in c.cli.calls)


@pytest.mark.parametrize("defect", ["env-drift", "target-pending"])
def test_closure_drift_or_timeout_is_bounded_and_never_claimed_closed(close_case, monkeypatch, defect):
    c = close_case
    module = configure(monkeypatch, c)
    preview = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    c.cli.mode, c.cli.defect = "Apply", defect
    began = c.clock.value
    with pytest.raises(ValueError):
        module.close_benchmark(c.plan, c.repo, c.cli, c.private, preview["confirmation"])
    record = json.loads(c.state_path.with_suffix(".closure.json").read_text())
    assert record["phase"] == "blocked" and c.clock.value - began <= 1230
    assert json.loads(c.state_path.read_text())["phase"] == "app_deployed"


def test_checked_cli_accepts_a_shorter_bounded_timeout(close_case, monkeypatch):
    from advisor_deploy.operations import Cli
    import advisor_deploy.operations as operations
    captured = []
    monkeypatch.setattr(operations.subprocess, "run", lambda *a, **k: (
        captured.append(k) or SimpleNamespace(returncode=0, stdout="{}", stderr="")))
    cli = Cli("Apply", close_case.repo, close_case.private)
    cli.az_path = "az.exe"
    cli.run(["containerapp", "ingress", "disable"], "bounded", mutation=True, timeout=7)
    assert captured[0]["timeout"] == 7


def test_closure_preview_does_not_echo_unrelated_environment_values(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    entry = {"name": "PRIVATE_VALUE", "value": "never-print-this-value"}
    c.apps[c.target]["properties"]["template"]["containers"][0]["env"].append(entry)
    c.revisions[c.target][0]["properties"]["template"] = deepcopy(c.apps[c.target]["properties"]["template"])
    report = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    assert "never-print-this-value" not in json.dumps(report)


def test_deactivation_waits_for_real_replica_shutdown_instead_of_failing_transient_state(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    preview = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    c.cli.mode, c.cli.defect = "Apply", "drain"
    result = module.close_benchmark(c.plan, c.repo, c.cli, c.private, preview["confirmation"])
    assert result["status"] == "closed" and c.cli.drain_reads > 1


@pytest.mark.parametrize("which", ["target", "observer"])
def test_closure_rejects_image_drift_before_mutation(close_case, monkeypatch, which):
    c = close_case
    module = configure(monkeypatch, c)
    c.apps[getattr(c, which)]["properties"]["template"]["containers"][0]["image"] = "unowned.azurecr.io/wrong:latest"
    with pytest.raises(ValueError, match="closure_deployed_app_mismatch"):
        module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    assert not any(kwargs.get("mutation") for _, _, kwargs in c.cli.calls)


def test_probe_failure_preserves_unresolved_state_and_does_not_claim_closed(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    preview = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    before = c.state_path.read_bytes()

    def reject(*args, **kwargs):
        raise ValueError("closure_route_readback_unverified")

    monkeypatch.setattr(module, "_probe_access", reject)
    c.cli.mode = "Apply"
    with pytest.raises(ValueError, match="closure_route"):
        module.close_benchmark(c.plan, c.repo, c.cli, c.private, preview["confirmation"])
    assert c.state_path.read_bytes() == before
    assert json.loads(c.state_path.with_suffix(".closure.json").read_text())["phase"] == "blocked"


def test_live_arm_revision_defaults_do_not_change_revision_ownership(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    app = c.apps[c.observer]["properties"]["template"]
    revision = c.revisions[c.observer][0]["properties"]["template"]
    app["revisionSuffix"] = "one"
    app["customMetricsSettings"] = None
    app["containers"][0]["imageType"] = "ContainerImage"
    app["containers"][0]["resources"]["ephemeralStorage"] = "2Gi"
    revision["revisionSuffix"] = None
    revision["scale"]["cooldownPeriod"] = None
    revision["scale"]["pollingInterval"] = None
    revision["containers"][0]["probes"] = [{"type": "Readiness", "tcpSocket": {"port": 8080}}]
    preview = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    assert preview["quiescence_verified"] is True


def test_derived_revision_suffix_is_not_treated_as_business_configuration(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    before = deepcopy(c.apps[c.target])
    after = deepcopy(before)
    before["properties"]["template"]["revisionSuffix"] = "0000003"
    after["properties"]["template"]["revisionSuffix"] = "0000004"
    assert module._stable(before) == module._stable(after)


def test_unchanged_stale_read_after_no_wait_is_polled_not_called_drift(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    preview = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    c.cli.mode, c.cli.defect = "Apply", "stale-read"
    result = module.close_benchmark(c.plan, c.repo, c.cli, c.private, preview["confirmation"])
    assert result["status"] == "closed"


def test_rejection_probe_wakeup_does_not_force_target_shutdown(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    preview = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)

    def wake(*args):
        c.cli.defect = "probe-wakeup"
        return {"benchmark": 404, "teams": 401}

    monkeypatch.setattr(module, "_probe_access", wake)
    c.cli.mode = "Apply"
    result = module.close_benchmark(c.plan, c.repo, c.cli, c.private, preview["confirmation"])
    assert result["status"] == "closed" and result["target_replicas"] == 1
    assert c.apps[c.target]["properties"]["template"]["scale"]["minReplicas"] == 0


def test_partial_closure_cannot_renew_the_original_mutation_window(close_case, monkeypatch):
    c = close_case
    module = configure(monkeypatch, c)
    preview = module.close_benchmark(c.plan, c.repo, c.cli, c.private, None)
    c.state_path.with_suffix(".closure.json").write_text(json.dumps({
        "format": "advisor-benchmark-closure-v1", "run_id": c.ledger["run_id"], "phase": "blocked",
        "actions": preview["actions"], "started_at": "2020-01-01T00:00:00+00:00",
    }))
    c.cli.mode = "Apply"
    with pytest.raises(ValueError, match="closure_mutation_window_expired"):
        module.close_benchmark(c.plan, c.repo, c.cli, c.private, preview["confirmation"])
    assert not any(options.get("mutation") for _, _, options in c.cli.calls)

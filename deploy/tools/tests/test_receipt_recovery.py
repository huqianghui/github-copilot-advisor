"""Failed ARM operation receipts, never group-inventory adoption."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from advisor_deploy.config import SUBSCRIPTION, TENANT
from advisor_deploy.manifest import ROLES, group_id, new_manifest, write_manifest
from advisor_deploy.operations import CliFailure


GROUP = group_id("functions")
SUB_DEPLOY = f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Resources/deployments/"
GROUP_DEPLOY = GROUP + "/providers/Microsoft.Resources/deployments/"
IDENTITY = GROUP + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-partial"
STORAGE = GROUP + "/providers/Microsoft.Storage/storageAccounts/stpartial"
APP = GROUP + "/providers/Microsoft.Web/sites/func-partial"
MONITOR = GROUP + "/providers/Microsoft.Insights/components/appi-partial"
LOG = GROUP + "/providers/Microsoft.OperationalInsights/workspaces/log-partial"
PLAN = GROUP + "/providers/Microsoft.Web/serverfarms/plan-partial"
PRINCIPAL, CLIENT = "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333"


def operation(identifier, state="Succeeded", verb="Create"):
    return {"operationId": uuid4().hex[:16].upper(), "properties": {
        "provisioningOperation": verb, "provisioningState": state,
        "targetResource": {"id": identifier, "resourceType": "Microsoft.Resources/deployments"
                           if "/deployments/" in identifier else "fixture", "resourceName": identifier.rsplit("/", 1)[1]},
    }}


@pytest.fixture
def recovery_case(tmp_path):
    out = tmp_path / "deploy" / "out"
    out.mkdir(parents=True)
    ledger = new_manifest("functions", str(uuid4()), "fixture", "a" * 64, "b" * 64)
    ledger.update(status="failed", tenant_id=TENANT, discovery_complete=False, owned_resource_ids=[GROUP])
    path = out / "functions.json"
    write_manifest(path, ledger, tmp_path)
    plan = SimpleNamespace(
        repository=tmp_path, manifest_path=path, target="functions", run_id=ledger["run_id"],
        group=ledger["resource_group"], config=SimpleNamespace(parameters={
            "openaiAuthMode": "key", "searchAuthMode": "key", "instanceMemoryMB": 512,
            "benchmarkEnabled": False, "environmentName": "fixture", "maxInstances": 40,
        }), artifact=SimpleNamespace(content_sha256="a" * 64), configuration_digest="b" * 64,
        capacity_evidence=None,
    )
    root_name = "advisor-functions-" + ledger["run_id"] + "-infra"
    root = SUB_DEPLOY + root_name
    ids = [IDENTITY, STORAGE, APP, MONITOR, LOG, PLAN]
    roles = []
    for scope, role in ((STORAGE, ROLES["blob_owner"]), (STORAGE, ROLES["queue"]), (MONITOR, ROLES["metrics"])):
        identifier = scope + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid4())
        roles.append({"id": identifier, "properties": {"principalId": PRINCIPAL,
                      "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{role}"}})
    docs = {
        root: {"id": root, "properties": {"provisioningState": "Failed", "outputResources": None, "outputs": None}},
        SUB_DEPLOY + "group": {"properties": {"provisioningState": "Succeeded", "outputResources": [{"id": GROUP}]}},
        GROUP_DEPLOY + "resources": {"properties": {"provisioningState": "Succeeded",
                                                   "outputResources": [{"id": i} for i in ids if i != APP]}},
        GROUP_DEPLOY + "app": {"properties": {"provisioningState": "Failed", "outputResources": None}},
        GROUP_DEPLOY + "settings": {"properties": {"provisioningState": "Failed", "outputResources": None}},
        GROUP_DEPLOY + "roles": {"properties": {"provisioningState": "Succeeded", "outputResources": None}},
    }
    ops = {
        root: [operation(SUB_DEPLOY + "group"), operation(GROUP_DEPLOY + "resources"),
               operation(GROUP_DEPLOY + "app", "Failed"), operation(GROUP_DEPLOY + "roles"),
               operation(GROUP + "/providers/Microsoft.Storage/storageAccounts/foreign-read", verb="Read")],
        GROUP_DEPLOY + "app": [operation(APP), operation(GROUP_DEPLOY + "settings", "Failed"),
                              operation(APP + "/config/appsettings", verb="Action")],
        GROUP_DEPLOY + "settings": [operation(APP + "/config/appsettings", "Failed")],
        GROUP_DEPLOY + "roles": [operation(r["id"]) for r in roles] + [
            {"operationId": "evaluate", "properties": {"provisioningOperation": "EvaluateDeploymentOutput",
                                                       "provisioningState": "Succeeded", "targetResource": None}}],
    }

    class Commands:
        mode = "WhatIf"
        repo = tmp_path
        broken_receipts = False
        fail_tag = False
        foreign_inventory = False
        wrong_owner = False
        wrong_principal = False
        calls = []

        def run(self, arguments, phase, **kwargs):
            self.calls.append((arguments, phase, kwargs))
            if arguments[:2] == ["account", "show"]:
                return {"id": SUBSCRIPTION, "tenantId": TENANT}
            if arguments[:2] == ["group", "exists"]:
                return True
            if arguments[:2] == ["provider", "show"]:
                return {"registrationState": "Registered"}
            if arguments[:2] == ["functionapp", "list-flexconsumption-runtimes"]:
                from test_flex_capacity import python_flex_record
                return [python_flex_record("3.12")]
            if arguments[:2] == ["group", "show"]:
                return {"id": GROUP, "tags": {"advisorDeploymentId": "wrong" if self.wrong_owner else ledger["run_id"]}}
            if arguments[:2] == ["group", "update"]:
                if self.fail_tag:
                    raise CliFailure("restore_ownership_tag", "Forbidden")
                return {}
            if arguments[:3] == ["deployment", "sub", "create"]:
                raise CliFailure("infrastructure_apply", "ProviderRejected")
            if arguments[:1] == ["deployment"]:
                if self.broken_receipts:
                    raise CliFailure("deployment_receipts", "Forbidden")
                name = arguments[arguments.index("--name") + 1]
                scope = "sub" if "sub" in arguments[:3] else "group"
                identifier = (SUB_DEPLOY if scope == "sub" else GROUP_DEPLOY) + name
                return deepcopy(ops[identifier] if arguments[1] == "operation" else docs[identifier])
            if arguments[:2] == ["resource", "list"]:
                return [{"id": i, "type": "/".join(i.split("/providers/")[1].split("/")[:2])}
                        for i in ids + ([GROUP + "/providers/Microsoft.Storage/storageAccounts/unrelated"] if self.foreign_inventory else [])]
            if arguments[:2] == ["identity", "show"]:
                return {"id": IDENTITY, "principalId": PRINCIPAL, "clientId": CLIENT, "tenantId": TENANT}
            raise AssertionError((arguments, phase, kwargs))

        def rest(self, method, url, phase, **kwargs):
            assert method == "get", "recovery must never mutate Azure"
            if "/permissions?" in url:
                return {"value": [{"actions": ["*"], "notActions": []}]}
            for role in roles:
                if role["id"] in url:
                    value = deepcopy(role)
                    if self.wrong_principal:
                        value["properties"]["principalId"] = str(uuid4())
                    return value
            raise AssertionError((method, url, phase))

    return SimpleNamespace(plan=plan, ledger=ledger, cli=Commands(), root_name=root_name,
                           root=root, docs=docs, ops=ops, ids=ids, roles=roles, path=path)


def test_failed_root_discovers_only_created_receipts_across_both_deployment_scopes(recovery_case):
    from advisor_deploy.operations import _record_receipts
    case = recovery_case
    _record_receipts(case.plan, case.cli, case.ledger, case.root_name)
    saved = json.loads(case.path.read_text())
    assert set(saved["owned_resource_ids"]) == {GROUP, *case.ids}
    assert set(saved["pending_role_assignment_ids"]) == {r["id"] for r in case.roles}
    assert saved["discovery_complete"] is True and saved["status"] == "failed"
    assert saved["outputs"] == {}
    assert any(args[:3] == ["deployment", "sub", "show"] and args[args.index("--name") + 1] == "group"
               for args, _, _ in case.cli.calls)


def test_inventory_collision_leaves_durable_partial_receipts_not_adoption(recovery_case):
    from advisor_deploy.operations import _record_receipts
    case = recovery_case
    case.cli.foreign_inventory = True
    with pytest.raises(ValueError, match="unrecorded_resource_collision"):
        _record_receipts(case.plan, case.cli, case.ledger, case.root_name)
    saved = json.loads(case.path.read_text())
    assert set(saved["owned_resource_ids"]) == {GROUP, *case.ids}
    assert saved["discovery_complete"] is False
    assert not any("unrelated" in i for i in saved["owned_resource_ids"])


@pytest.mark.parametrize("tag_failure", [False, True])
def test_recovery_failure_never_masks_original_apply_error(recovery_case, tag_failure):
    from advisor_deploy.operations import _infra_apply
    case = recovery_case
    case.cli.broken_receipts = True
    case.cli.fail_tag = tag_failure
    with pytest.raises(CliFailure) as caught:
        _infra_apply(case.plan, case.cli, case.ledger, case.path.parent / "private.json", "infra")
    assert caught.value.phase == "infrastructure_apply" and caught.value.code == "ProviderRejected"
    recovery = json.loads(case.path.with_suffix(".recovery.json").read_text())
    assert recovery["primary_error"]["phase"] == "infrastructure_apply"
    assert any(item["phase"] == "receipt_recovery" for item in recovery["recovery_errors"])
    assert next(item for item in recovery["recovery_errors"] if item["phase"] == "receipt_recovery")["deployment_id"] == case.root
    assert recovery["discovery_complete"] is False


def test_partial_role_binding_uses_receipt_owned_mi_without_fabricating_outputs(recovery_case):
    from advisor_deploy.operations import _record_receipts, _record_roles
    case = recovery_case
    _record_receipts(case.plan, case.cli, case.ledger, case.root_name)
    _record_roles(case.plan, case.cli, case.ledger, partial=True)
    assert len(case.ledger["role_assignments"]) == 3
    assert {r["identity_id"] for r in case.ledger["role_assignments"]} == {IDENTITY}
    assert not case.ledger["pending_role_assignment_ids"]
    assert case.ledger["outputs"] == {} and case.ledger["status"] == "failed"


def test_reconcile_preview_is_read_only_and_approved_apply_only_updates_ledger(recovery_case):
    from advisor_deploy.operations import reconcile_receipts
    case = recovery_case
    before = case.path.read_bytes()
    preview = reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, None)
    assert case.path.read_bytes() == before
    assert preview["resource_count"] == 7 and preview["role_count"] == 3
    case.cli.mode = "Apply"
    with pytest.raises(ValueError, match="reconcile_confirmation_required"):
        reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, "wrong")
    report = reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, preview["confirmation"])
    saved = json.loads(case.path.read_text())
    assert report["status"] == "failed" and saved["discovery_complete"]
    assert saved["outputs"] == {} and len(saved["role_assignments"]) == 3
    assert not any(options.get("mutation") for _, _, options in case.cli.calls)


def test_reconcile_can_persist_proven_subset_but_unknown_inventory_still_blocks_resume(recovery_case):
    from advisor_deploy.operations import reconcile_receipts
    case = recovery_case
    case.cli.foreign_inventory = True
    preview = reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, None)
    assert preview["discovery_complete"] is False and preview["resume_allowed"] is False
    assert preview["blockers"] == ["unrecorded_resource_collision"]
    assert preview["role_count"] == 3
    case.cli.mode = "Apply"
    reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, preview["confirmation"])
    saved = json.loads(case.path.read_text())
    assert not saved["discovery_complete"] and saved["status"] == "failed"
    assert len(saved["role_assignments"]) == 3
    assert set(saved["owned_resource_ids"]) == {GROUP, *case.ids}
    assert not any("unrelated" in identifier for identifier in saved["owned_resource_ids"])


@pytest.mark.parametrize("defect", ["owner", "root", "principal"])
def test_reconcile_rejects_foreign_ownership_or_role_principal(recovery_case, defect):
    from advisor_deploy.operations import reconcile_receipts
    case = recovery_case
    case.cli.wrong_owner = defect == "owner"
    case.cli.wrong_principal = defect == "principal"
    root_name = "unrelated-deployment" if defect == "root" else case.root_name
    before = case.path.read_bytes()
    with pytest.raises(ValueError):
        reconcile_receipts(case.path, case.plan.repository, case.cli, root_name, None)
    assert case.path.read_bytes() == before


def _external_cli():
    state = json.loads(Path(os.environ["RECEIPT_STUB_STATE"]).read_text())
    args = sys.argv[1:]
    with Path(os.environ["RECEIPT_STUB_LOG"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(args) + "\n")

    def option(name):
        return args[args.index(name) + 1]

    if args[:2] == ["account", "show"]:
        result = {"id": SUBSCRIPTION, "tenantId": TENANT}
    elif args[:2] == ["group", "show"]:
        result = {"id": GROUP, "tags": {"advisorDeploymentId": state["run_id"]}}
    elif args[0] == "deployment":
        scope = SUB_DEPLOY if "sub" in args[:3] else GROUP_DEPLOY
        result = state["ops" if args[1] == "operation" else "docs"][scope + option("--name")]
    elif args[:2] == ["resource", "list"]:
        result = [{"id": identifier, "type": "/".join(identifier.split("/providers/")[1].split("/")[:2])}
                  for identifier in state["ids"]]
    elif args[:2] == ["identity", "show"]:
        result = {"id": IDENTITY, "principalId": PRINCIPAL, "clientId": CLIENT, "tenantId": TENANT}
    elif args[0] == "rest" and option("--method") == "get":
        result = next(role for role in state["roles"] if role["id"] in option("--url"))
    else:
        raise AssertionError("unexpected command")
    print(json.dumps(result))


def test_real_reconcile_wrapper_previews_then_updates_local_ledger_only(recovery_case):
    case = recovery_case
    root = Path(__file__).resolve().parents[3]
    shared = case.plan.repository / "deploy" / "shared"
    shared.mkdir()
    for name in ("Azure.ps1", "Reconcile.ps1"):
        shutil.copyfile(root / "deploy" / "shared" / name, shared / name)
    state = case.path.parent / "stub.json"
    state.write_text(json.dumps({"run_id": case.ledger["run_id"], "docs": case.docs, "ops": case.ops,
                                "ids": case.ids, "roles": case.roles}))
    stub = case.path.parent / "cli.py"
    stub.write_text(f"import sys\nsys.path.insert(0,{str(Path(__file__).parent)!r})\n"
                    "from test_receipt_recovery import _external_cli\n_external_cli()\n")
    log = case.path.parent / "calls.jsonl"
    env = os.environ | {"ADVISOR_AZ_COMMAND": str(stub), "ADVISOR_PYTHON_COMMAND": sys.executable,
                        "RECEIPT_STUB_STATE": str(state), "RECEIPT_STUB_LOG": str(log)}
    shell = shutil.which("pwsh")
    assert shell

    def invoke(*extra):
        result = subprocess.run([shell, "-NoProfile", "-File", str(shared / "Reconcile.ps1"),
                                 "-OutputsFile", str(case.path), "-DeploymentName", case.root_name, *extra],
                                cwd=case.plan.repository, env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))

    before = case.path.read_bytes()
    preview = invoke()
    assert preview["mode"] == "WhatIf" and case.path.read_bytes() == before
    report = invoke("-Mode", "Apply", "-Confirmation", preview["confirmation"])
    assert report["azure_mutations"] is False and report["role_count"] == 3
    assert json.loads(case.path.read_text())["outputs"] == {}
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(word in args for args in calls for word in ("create", "update", "delete", "config-zip"))


def test_foreign_nested_subscription_is_not_followed_or_adopted(recovery_case):
    from advisor_deploy.operations import _record_receipts
    case = recovery_case
    foreign = SUB_DEPLOY.replace(SUBSCRIPTION, "11111111-1111-1111-1111-111111111111") + "foreign"
    case.ops[case.root].append(operation(foreign))
    with pytest.raises(ValueError):
        _record_receipts(case.plan, case.cli, case.ledger, case.root_name)
    assert not any(foreign in args for args, _, _ in case.cli.calls)
    assert json.loads(case.path.read_text())["discovery_complete"] is False


def test_failed_empty_bootstrap_requires_reconciliation_before_resume(recovery_case):
    from advisor_deploy.operations import preflight, _record_receipts, _record_roles
    case = recovery_case
    with pytest.raises(ValueError, match="ownership_discovery_incomplete"):
        preflight(case.plan, case.cli, bootstrap_only=True)
    _record_receipts(case.plan, case.cli, case.ledger, case.root_name)
    _record_roles(case.plan, case.cli, case.ledger, partial=True)
    report, manifest = preflight(case.plan, case.cli, bootstrap_only=True)
    assert report["phase"] == "bootstrap_only" and report["regional_capacity"] == "unknown_deferred"
    assert manifest["status"] == "failed" and manifest["outputs"] == {}
    assert not any(options.get("mutation") for _, _, options in case.cli.calls)

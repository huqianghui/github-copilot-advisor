"""Group-scoped observer bootstrap through real PowerShell and fake Azure."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
from uuid import UUID, uuid5

import pytest

from advisor_deploy.manifest import ROLES, SUBSCRIPTION, group_id, new_manifest, write_manifest
from test_deploy_workflows import workflow_case


@pytest.fixture
def observer_case(workflow_case):
    case = workflow_case
    root = Path(__file__).resolve().parents[3]
    bench = case.repo / "deploy" / "bench"
    (bench / "infra").mkdir(parents=True)
    for relative in ("Deploy.ps1", "Control.ps1", "infra/main.bicep"):
        shutil.copyfile(root / "deploy" / "bench" / relative, bench / relative)
    state = {"target": "aca", "group_exists": True, "owner": case.run_id,
             "resources": [], "resource_details": {}, "identities": {}}
    manifests = {}
    for target, principal, client in (
        ("aca", "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333"),
        ("functions", "99999999-9999-9999-9999-999999999999", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    ):
        group = group_id(target)
        identity = group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/main"
        storage = group + "/providers/Microsoft.Storage/storageAccounts/stfixture"
        app = group + ("/providers/Microsoft.App/containerApps/main" if target == "aca" else "/providers/Microsoft.Web/sites/main")
        bot = group + "/providers/Microsoft.BotService/botServices/main"
        ledger = new_manifest(target, case.run_id, "fixture", "a" * 64, "b" * 64)
        ledger.update(tenant_id="4bc8a937-2b77-44c4-8790-dfc0068162f2", status="deployed")
        ledger["owned_resource_ids"] = [group, identity, storage, app, bot]
        if target == "aca":
            ledger["owned_resource_ids"] += [
                group + "/providers/Microsoft.ContainerRegistry/registries/fixture",
                group + "/providers/Microsoft.App/managedEnvironments/fixture",
            ]
        ledger["outputs"] = {
            "target": target, "resource_group": ledger["resource_group"], "app_id": app, "app_name": "main",
            "endpoint": "https://" + target + ".example.net", "storage_id": storage,
            "blob_url": "https://stfixture.blob.core.windows.net/", "queue_url": "https://stfixture.queue.core.windows.net/",
            "identity_id": identity, "identity_client_id": client, "identity_principal_id": principal,
            "bot_id": bot, "created_role_assignment_ids": [],
        }
        for identifier in ledger["owned_resource_ids"][1:]:
            provider = identifier.split("/providers/")[1].split("/")
            kind = provider[0] + "/" + provider[1]
            item = {"id": identifier, "name": identifier.split("/")[-1], "type": kind}
            state["resources"].append(item)
            state["resource_details"][identifier] = item | {"properties": {
                "allowSharedKeyAccess": False, "allowBlobPublicAccess": False, "supportsHttpsTrafficOnly": True,
                "defaultDomain": "fixture.southeastasia.azurecontainerapps.io", "loginServer": "fixture.azurecr.io",
            }, "sku": {"name": "Basic"}}
        path = case.out / (target + "-ledger.json")
        write_manifest(path, ledger, case.repo)
        manifests[target] = path
    api = new_manifest("benchmark", case.run_id, "api-fixture", "0" * 64, "0" * 64)
    api["tenant_id"] = "4bc8a937-2b77-44c4-8790-dfc0068162f2"
    api["graph"] = {
        "application_id": "44444444-4444-4444-4444-444444444444",
        "client_id": "55555555-5555-5555-5555-555555555555",
        "service_principal_id": "66666666-6666-6666-6666-666666666666",
        "role_id": str(uuid5(UUID(case.run_id), "Benchmark.Invoke")), "grants": [],
    }
    state["applications"] = [{
        "id": api["graph"]["application_id"], "appId": api["graph"]["client_id"],
        "tags": ["advisorDeploymentId:" + case.run_id], "signInAudience": "AzureADMyOrg",
        "identifierUris": ["api://" + api["graph"]["client_id"]], "api": {"requestedAccessTokenVersion": 2},
        "appRoles": [{"id": api["graph"]["role_id"], "value": "Benchmark.Invoke",
                      "allowedMemberTypes": ["Application"], "isEnabled": True}],
    }]
    state["service_principals"] = [{"id": api["graph"]["service_principal_id"], "appId": api["graph"]["client_id"]}]
    api_path = case.out / "api-ledger.json"
    write_manifest(api_path, api, case.repo)
    identity = group_id("aca") + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-bench-fixture"
    container = json.loads(manifests["aca"].read_text())["outputs"]["storage_id"] + "/blobServices/default/containers/benchmark-results"
    roles = []
    for scope, role in (
        (group_id("aca") + "/providers/Microsoft.ContainerRegistry/registries/fixture", ROLES["pull"]),
        (container, ROLES["blob"]),
    ):
        roles.append(scope + "/providers/Microsoft.Authorization/roleAssignments/" +
                     str(uuid5(UUID("77777777-7777-7777-7777-777777777777"), scope + role)))
    state["bench_changes"] = [{"resourceId": identifier, "changeType": "Create"} for identifier in [identity, container, *roles]]
    case.state_path.write_text(json.dumps(state), encoding="utf-8")
    plan_path = case.repo / "observer-plan.json"
    plan_path.write_text(json.dumps({
        "format": "advisor-bench-deploy-v1", "run_id": case.run_id, "environment_name": "bench-fixture",
        "aca_manifest": str(manifests["aca"]), "functions_manifest": str(manifests["functions"]),
        "api_manifest": str(api_path), "state_file": str(case.out / "observer-state.json"),
    }), encoding="utf-8")
    case.observer_plan = plan_path
    case.aca_ledger = manifests["aca"]
    case.observer_state = case.out / "observer-state.json"
    return case


def test_observer_whatif_is_group_scoped_and_never_changes_main_resources(observer_case):
    case = observer_case
    before = case.aca_ledger.read_bytes()
    result, report = case.invoke("bench/Deploy.ps1", "-ParametersFile", case.observer_plan)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["scope"] == "resourceGroup" and report["stage"] == "Bootstrap"
    assert case.aca_ledger.read_bytes() == before
    assert not case.observer_state.exists()
    calls = case.calls()
    deployments = [item["arguments"] for item in calls if item["arguments"][0] == "deployment"]
    assert deployments and all(args[1] == "group" for args in deployments)
    assert not any(word in item["arguments"] for item in calls for word in ("create", "update", "push", "delete"))
    assert not any(item["tool"] == "docker" for item in calls)


def test_observer_bootstrap_appends_driver_receipts_without_retag_or_app_changes(observer_case):
    case = observer_case
    original = json.loads(case.aca_ledger.read_text(encoding="utf-8"))
    result, preview = case.invoke("bench/Deploy.ps1", "-ParametersFile", case.observer_plan)
    assert result.returncode == 0, result.stdout + result.stderr
    result, report = case.invoke("bench/Deploy.ps1", "-ParametersFile", case.observer_plan,
                                "-Mode", "Apply", "-Confirmation", preview["confirmation"])
    assert result.returncode == 0, result.stdout + result.stderr
    updated = json.loads(case.aca_ledger.read_text(encoding="utf-8"))
    assert updated["outputs"] == original["outputs"]
    assert updated["summary"] == original["summary"]
    assert len(updated["owned_resource_ids"]) == len(original["owned_resource_ids"]) + 2
    assert len(updated["role_assignments"]) == 2
    assert {item["identity_id"] for item in updated["role_assignments"]} == {report["identity_id"]}
    assert {item["principal_id"] for item in updated["role_assignments"]} == {report["identity_principal_id"]}
    assert report["identity_principal_id"] != original["outputs"]["identity_principal_id"]
    assert report["planned_endpoint"] == "https://ca-bench-fixture.fixture.southeastasia.azurecontainerapps.io"
    assert report["app_endpoint"] == ""
    calls = case.calls()
    assert not any(item["arguments"][:2] in (["group", "create"], ["group", "update"], ["containerapp", "update"]) for item in calls)
    assert not any(item["tool"] == "docker" for item in calls)
    start = len(calls)
    result, blocked = case.invoke("bench/Deploy.ps1", "-ParametersFile", case.observer_plan, "-Stage", "Publish")
    assert result.returncode != 0 and blocked["error"]["code"] == "benchmark_context_required"
    assert not any(item["tool"] == "docker" for item in case.calls()[start:])
    assert not any("create" in item["arguments"] for item in case.calls()[start:])

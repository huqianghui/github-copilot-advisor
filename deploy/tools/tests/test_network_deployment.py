"""Network WhatIf/Apply contracts with isolated ARM responses, never Azure."""

from copy import deepcopy
from datetime import datetime, timezone
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
from advisor_deploy.render import digest


@pytest.fixture
def network_case(tmp_path):
    repo = tmp_path
    out = repo / "deploy" / "out"
    (out / "runs").mkdir(parents=True)
    (out / "plans").mkdir()
    template = repo / "deploy" / "network" / "infra" / "main.bicep"
    template.parent.mkdir(parents=True)
    template.write_text("targetScope = 'resourceGroup'\n")
    ledgers, storage, roles = {}, {}, {}
    for target in ("aca", "functions"):
        group = group_id(target)
        identity = group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/main"
        account = group + "/providers/Microsoft.Storage/storageAccounts/st" + target
        app = group + ("/providers/Microsoft.App/containerApps/main" if target == "aca" else "/providers/Microsoft.Web/sites/main")
        bot = group + "/providers/Microsoft.BotService/botServices/main"
        ledger = new_manifest(target, str(uuid4()), "fixture", "a" * 64, "b" * 64)
        principal, client = str(uuid4()), str(uuid4())
        ledger.update(status="deployed" if target == "aca" else "infrastructure_ready", tenant_id=TENANT)
        ledger["owned_resource_ids"] = [group, identity, account, app, bot]
        ledger["outputs"] = {
            "target": target, "resource_group": ledger["resource_group"], "app_id": app, "app_name": "main",
            "endpoint": "https://" + target + ".example.net", "storage_id": account,
            "blob_url": "https://st" + target + ".blob.core.windows.net/", "queue_url": "https://st" + target + ".queue.core.windows.net/",
            "identity_id": identity, "identity_client_id": client, "identity_principal_id": principal,
            "bot_id": bot, "created_role_assignment_ids": [],
        }
        for definition in (ROLES["blob"] if target == "aca" else ROLES["blob_owner"], ROLES["queue"]):
            identifier = account + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid4())
            role = {"id": identifier, "scope": account, "identity_id": identity, "principal_id": principal,
                    "role_definition_id": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{definition}"}
            ledger["role_assignments"].append(role)
            ledger["outputs"]["created_role_assignment_ids"].append(identifier)
            roles[identifier] = {"id": identifier, "properties": {"principalId": principal, "roleDefinitionId": role["role_definition_id"]}}
        path = out / "runs" / (target + ".json")
        write_manifest(path, ledger, repo)
        ledgers[target] = (path, ledger)
        storage[account] = {"id": account, "type": "Microsoft.Storage/storageAccounts", "location": "southeastasia",
                            "kind": "StorageV2", "sku": {"name": "Standard_LRS"}, "tags": {"keep": "unchanged"},
                            "properties": {"allowSharedKeyAccess": False, "allowBlobPublicAccess": False,
                                "supportsHttpsTrafficOnly": True, "minimumTlsVersion": "TLS1_2",
                                "publicNetworkAccess": "Disabled", "networkAcls": {
                                    "defaultAction": "Allow", "bypass": "None", "ipRules": [],
                                    "virtualNetworkRules": [], "resourceAccessRules": []}}}
    run_id = ledgers["aca"][1]["run_id"]
    perimeter = group_id("aca") + "/providers/Microsoft.Network/networkSecurityPerimeters/nsp-advisor-" + run_id.replace("-", "")[:16]
    profile = perimeter + "/profiles/storage"
    rule = profile + "/accessRules/owned-subscription"
    association_ids = [perimeter + "/resourceAssociations/" + value for value in ("aca-state", "flex-state")]
    expected = [perimeter, profile, rule, *association_ids]
    state_path = ledgers["aca"][0].with_suffix(".network.json")
    plan = out / "plans" / "network.json"
    plan.write_text(json.dumps({"format": "advisor-network-plan-v1",
                               "aca_manifest": "../runs/aca.json", "functions_manifest": "../runs/functions.json"}))
    private_directory = out / ".private-network"
    private_directory.mkdir()

    def private_json(value):
        path = private_directory / (uuid4().hex + ".json")
        path.write_text(json.dumps(value))
        return path

    class FakeCli:
        mode = "WhatIf"
        defect = ""
        deployed = False
        calls = []
        patches = []
        network = {}

        def run(self, args, phase, **options):
            self.calls.append((args, phase, options))
            if args[:2] == ["account", "show"]:
                return {"id": SUBSCRIPTION, "tenantId": TENANT}
            if args[:2] == ["provider", "show"]:
                return {"registrationState": "NotRegistered" if self.defect == "provider" else "Registered",
                        "resourceTypes": [{"resourceType": "networkSecurityPerimeters",
                            "locations": ["West Europe"] if self.defect == "region" else ["Southeast Asia"],
                            "apiVersions": ["2024-07-01"]}]}
            if args[:2] == ["group", "exists"]:
                return True
            if args[:2] == ["group", "show"]:
                name = args[args.index("--name") + 1]
                ledger = next(v[1] for v in ledgers.values() if v[1]["resource_group"] == name)
                return {"id": group_id(ledger["target"]), "tags": {"advisorDeploymentId": ledger["run_id"]}}
            if args[:2] == ["resource", "list"]:
                name = args[args.index("--resource-group") + 1]
                ledger = next(v[1] for v in ledgers.values() if v[1]["resource_group"] == name)
                identifiers = ledger["owned_resource_ids"][1:]
                if self.deployed and ledger["target"] == "aca":
                    identifiers = identifiers + [perimeter]
                return [{"id": i, "type": "/".join(i.split("/providers/")[1].split("/")[:2])} for i in identifiers]
            if args[:2] == ["resource", "show"]:
                return deepcopy(storage[args[args.index("--ids") + 1]])
            if args[:2] == ["bicep", "build"]:
                return json.dumps({"$schema": "fixture-compiled-template", "resources": []})
            if args[0] == "rest" and "prices.azure.com" in args[args.index("--url") + 1]:
                assert "--skip-authorization-header" in args
                return {"Items": [], "NextPageLink": None}
            if args[:2] == ["deployment", "group"]:
                if args[2] == "what-if":
                    changes = [{"resourceId": i, "changeType": "Create"} for i in expected]
                    if self.defect == "foreign-preview":
                        changes.append({"resourceId": storage_ids[0], "changeType": "Modify"})
                    return {"changes": changes}
                if args[2] == "validate":
                    return {"properties": {"provisioningState": "Succeeded"}}
                if args[2] == "create":
                    assert self.mode == "Apply" and options["mutation"]
                    self.deployed = True
                    self.network = {
                        perimeter: {"id": perimeter, "location": "southeastasia", "tags": {"advisorDeploymentId": run_id},
                                    "properties": {"provisioningState": "Succeeded"}},
                        profile: {"id": profile, "properties": {"provisioningState": "Succeeded"}},
                        rule: {"id": rule, "name": "owned-subscription", "properties": {
                            "provisioningState": "Succeeded", "direction": "Inbound", "subscriptions": [{"id": "/subscriptions/" + SUBSCRIPTION}]}},
                    }
                    for identifier, account in zip(association_ids, storage_ids):
                        self.network[identifier] = {"id": identifier, "properties": {"provisioningState": "Succeeded",
                            "hasProvisioningIssues": "no",
                            "accessMode": "Learning" if self.defect == "learning" else "Enforced",
                            "privateLinkResource": {"id": account}, "profile": {"id": profile}}}
                    return {"properties": {"provisioningState": "Succeeded"}}
                if args[2] == "show":
                    ids = expected + ([storage_ids[1]] if self.defect == "foreign-receipt" else [])
                    return {"properties": {"outputResources": [{"id": i} for i in ids], "provisioningState": "Succeeded"}}
            raise AssertionError((args, phase, options))

        def rest(self, method, url, phase, **options):
            self.calls.append(([method, url], phase, options | {"mutation": method != "get"}))
            identifier = url.removeprefix("https://management.azure.com").split("?")[0]
            if "/permissions?" in url:
                return {"value": [{"actions": [] if self.defect == "permission" else ["*"], "notActions": []}]}
            if identifier in roles:
                return roles[identifier]
            if identifier in storage:
                if method == "patch":
                    assert self.mode == "Apply" and self.deployed
                    patch = options["body"]
                    self.patches.append((identifier, deepcopy(patch)))
                    storage[identifier]["properties"].update(deepcopy(patch["properties"]))
                    if self.defect == "policy-reverted":
                        storage[identifier]["properties"]["publicNetworkAccess"] = "Disabled"
                value = deepcopy(storage[identifier])
                if method == "get" and self.defect == "server-defaults":
                    value["properties"]["networkAcls"].pop("resourceAccessRules", None)
                    value["properties"]["networkAcls"]["ipRules"] = None
                    value["properties"]["networkAcls"]["virtualNetworkRules"] = None
                return value
            if "/networkSecurityPerimeterConfigurations" in identifier:
                account = identifier.split("/networkSecurityPerimeterConfigurations")[0]
                if not self.deployed:
                    return {"value": []}
                index = storage_ids.index(account)
                rule_props = {"direction": "Inbound", "subscriptions": [{"id": "/subscriptions/" + SUBSCRIPTION}]}
                if self.defect == "effective-rule":
                    rule_props["addressPrefixes"] = ["0.0.0.0/0"]
                return {"value": [{"id": account + "/networkSecurityPerimeterConfigurations/guid.association",
                    "properties": {"provisioningState": "Succeeded", "provisioningIssues": [],
                        "networkSecurityPerimeter": {"id": perimeter},
                        "resourceAssociation": {"name": association_ids[index].rsplit("/", 1)[1], "accessMode": "Enforced"},
                        "profile": {"name": "storage", "accessRules": [{"name": "owned-subscription", "properties": rule_props}]}}}]}
            if identifier in self.network:
                return deepcopy(self.network[identifier])
            if identifier.endswith("/profiles"):
                return {"value": [self.network[profile]]}
            if identifier.endswith("/accessRules"):
                return {"value": [self.network[rule]]}
            if identifier.endswith("/resourceAssociations"):
                return {"value": [self.network[i] for i in association_ids]}
            raise AssertionError((method, url, phase, options))

    storage_ids = [ledgers[target][1]["outputs"]["storage_id"] for target in ("aca", "functions")]
    cli = FakeCli()
    cli.repo = repo
    return SimpleNamespace(repo=repo, plan=plan, cli=cli, ledgers=ledgers, storage=storage, storage_ids=storage_ids, roles=roles,
                           ids=expected, state_path=state_path, private=SimpleNamespace(directory=private_directory, json=private_json))


def approve_cost(case, preview):
    data = json.loads(case.plan.read_text())
    data["cost_review"] = {"pricing_sha256": preview["pricing"]["sha256"], "reviewed_at": datetime.now(timezone.utc).isoformat(),
                           "acknowledge_unpublished_meter_risk": True, "acknowledge_existing_service_costs": True}
    case.plan.write_text(json.dumps(data))


def test_network_whatif_is_read_only_and_requires_real_cost_review(network_case):
    from advisor_deploy.network_deploy import deploy_network
    case = network_case
    before = {name: path.read_bytes() for name, (path, _) in case.ledgers.items()}
    report = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    assert not report["can_apply"] and report["pricing"]["meter_count"] == 0
    assert report["pricing"]["zero_cost_verified"] is False
    assert report["storage_ids"] == case.storage_ids and report["access_mode"] == "Enforced"
    assert all(path.read_bytes() == before[name] for name, (path, _) in case.ledgers.items())
    assert not case.state_path.exists()
    assert not any(options.get("mutation") for _, _, options in case.cli.calls)
    case.cli.mode = "Apply"
    with pytest.raises(ValueError, match="network_cost_review"):
        deploy_network(case.plan, case.repo, case.cli, case.private, report["confirmation"])
    assert not case.cli.deployed


def test_network_apply_appends_only_receipts_preserves_both_ledgers_and_locks_storage(network_case):
    from advisor_deploy.network_deploy import deploy_network
    case = network_case
    preview = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    approve_cost(case, preview)
    preview = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    before = {key: deepcopy(value) for key, (_, value) in case.ledgers.items()}
    flex_bytes = case.ledgers["functions"][0].read_bytes()
    case.cli.mode = "Apply"
    report = deploy_network(case.plan, case.repo, case.cli, case.private, preview["confirmation"])
    assert report["status"] == "network_verified" and report["data_plane_validation"] == "not_run"
    aca = json.loads(case.ledgers["aca"][0].read_text())
    assert set(aca["owned_resource_ids"]) == set(before["aca"]["owned_resource_ids"]) | set(case.ids)
    for key in ("outputs", "artifact_digest", "config_digest", "summary", "role_assignments"):
        assert aca[key] == before["aca"][key]
    assert case.ledgers["functions"][0].read_bytes() == flex_bytes
    assert len(case.cli.patches) == 2 and {item[0] for item in case.cli.patches} == set(case.storage_ids)
    for _, patch in case.cli.patches:
        assert set(patch) == {"properties"}
        assert patch["properties"]["publicNetworkAccess"] == "SecuredByPerimeter"
        assert patch["properties"]["networkAcls"]["bypass"] == "None"
        assert patch["properties"]["networkAcls"]["defaultAction"] == "Deny"
        assert not any("tag" in name.lower() or "key" in name.lower() for name in patch["properties"])
    state = json.loads(case.state_path.read_text())
    assert state["storage_ids"] == case.storage_ids and state["resource_ids"] == sorted(case.ids)
    assert state["access_mode"] == "Enforced" and state["inbound_subscription_id"] == "/subscriptions/" + SUBSCRIPTION
    assert len(state["compiled_template_sha256"]) == 64 and state["verified_at"]
    assert not any("register" in args or "delete" in args for args, _, _ in case.cli.calls)


@pytest.mark.parametrize("defect", ["provider", "region", "permission", "foreign-preview"])
def test_network_preflight_rejects_unavailable_or_widened_scope(network_case, defect):
    from advisor_deploy.network_deploy import deploy_network
    case = network_case
    case.cli.defect = defect
    with pytest.raises(ValueError):
        deploy_network(case.plan, case.repo, case.cli, case.private, None)
    assert not case.cli.deployed and not case.cli.patches


@pytest.mark.parametrize("action", ["NoChange", "Ignore", "Deploy", "Modify"])
def test_network_preview_never_adopts_a_colliding_unrecorded_perimeter(network_case, action):
    from advisor_deploy.network_deploy import deploy_network

    case = network_case
    original = case.cli.run

    def collided(args, phase, **options):
        result = original(args, phase, **options)
        if phase == "network_arm_whatif":
            result["changes"][0]["changeType"] = action
        return result

    case.cli.run = collided
    with pytest.raises(ValueError, match="network_unowned_collision"):
        deploy_network(case.plan, case.repo, case.cli, case.private, None)
    assert not case.cli.deployed and not case.cli.patches


@pytest.mark.parametrize("issue", [True, "yes", "unknown", "false", 0])
def test_network_does_not_relax_storage_for_unknown_or_positive_provisioning_issues(network_case, issue):
    from advisor_deploy.network_deploy import deploy_network

    case = network_case
    preview = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    approve_cost(case, preview)
    preview = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    original = case.cli.rest

    def issue_response(method, url, phase, **options):
        result = original(method, url, phase, **options)
        if phase == "network_children":
            for row in result.get("value", []):
                if "accessMode" in row.get("properties", {}):
                    row["properties"]["hasProvisioningIssues"] = issue
        return result

    case.cli.rest = issue_response
    case.cli.mode = "Apply"
    with pytest.raises(ValueError, match="network_child_not_ready"):
        deploy_network(case.plan, case.repo, case.cli, case.private, preview["confirmation"])
    assert not case.cli.patches


@pytest.mark.parametrize("defect", ["learning", "effective-rule", "policy-reverted", "foreign-receipt"])
def test_network_readback_never_claims_success_with_drift(network_case, defect):
    from advisor_deploy.network_deploy import deploy_network
    case = network_case
    preview = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    approve_cost(case, preview)
    preview = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    case.cli.mode, case.cli.defect = "Apply", defect
    with pytest.raises(ValueError):
        deploy_network(case.plan, case.repo, case.cli, case.private, preview["confirmation"])
    assert json.loads(case.state_path.read_text())["phase"] == "failed"
    if defect in {"learning", "effective-rule", "foreign-receipt"}:
        assert not case.cli.patches


def test_network_plan_cannot_replace_an_owned_storage_id_or_escape_output_scope(network_case):
    from advisor_deploy.network_deploy import deploy_network
    case = network_case
    data = json.loads(case.plan.read_text())
    data["storage_ids"] = [case.storage_ids[0], "/subscriptions/foreign/storage"]
    case.plan.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="network_plan"):
        deploy_network(case.plan, case.repo, case.cli, case.private, None)
    del data["storage_ids"]
    data["aca_manifest"] = "../../../../unowned.json"
    case.plan.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="network_plan"):
        deploy_network(case.plan, case.repo, case.cli, case.private, None)


def test_storage_readback_accepts_empty_server_defaults_without_relaxing_acl(network_case):
    from advisor_deploy.network_deploy import deploy_network
    case = network_case
    preview = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    approve_cost(case, preview)
    preview = deploy_network(case.plan, case.repo, case.cli, case.private, None)
    case.cli.mode, case.cli.defect = "Apply", "server-defaults"
    report = deploy_network(case.plan, case.repo, case.cli, case.private, preview["confirmation"])
    assert report["status"] == "network_verified"


def test_existing_perimeter_does_not_adopt_unrecorded_child_resources(network_case):
    from advisor_deploy.network_deploy import deploy_network
    case = network_case
    case.cli.mode = "Apply"
    case.cli.run(["deployment", "group", "create"], "fixture-initial-state", mutation=True)
    case.cli.mode = "WhatIf"
    path, ledger = case.ledgers["aca"]
    ledger["owned_resource_ids"].append(case.ids[0])
    write_manifest(path, ledger, case.repo)
    start = len(case.cli.calls)
    with pytest.raises(ValueError, match="network_prior_receipts"):
        deploy_network(case.plan, case.repo, case.cli, case.private, None)
    assert not any(options.get("mutation") for _, _, options in case.cli.calls[start:])


def _external_network_cli():
    state = json.loads(Path(os.environ["NETWORK_STUB_STATE"]).read_text())
    args = sys.argv[1:]
    with Path(os.environ["NETWORK_STUB_LOG"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(args) + "\n")

    def option(name):
        return args[args.index(name) + 1]

    if args[:2] == ["account", "show"]:
        result = {"id": SUBSCRIPTION, "tenantId": TENANT}
    elif args[:2] == ["provider", "show"]:
        result = {"registrationState": "Registered", "resourceTypes": [
            {"resourceType": "networkSecurityPerimeters", "locations": ["Southeast Asia"], "apiVersions": ["2024-07-01"]}]}
    elif args[:2] == ["group", "exists"]:
        result = True
    elif args[:2] == ["group", "show"]:
        ledger = next(v for v in state["ledgers"] if v["resource_group"] == option("--name"))
        result = {"tags": {"advisorDeploymentId": ledger["run_id"]}}
    elif args[:2] == ["resource", "list"]:
        ledger = next(v for v in state["ledgers"] if v["resource_group"] == option("--resource-group"))
        result = [{"id": i, "type": "/".join(i.split("/providers/")[1].split("/")[:2])}
                  for i in ledger["owned_resource_ids"] if "/providers/" in i]
    elif args[:2] == ["resource", "show"]:
        result = state["storage"][option("--ids")]
    elif args[:2] == ["bicep", "build"]:
        result = {"resources": []}
    elif args[:3] == ["deployment", "group", "validate"]:
        result = {"properties": {"provisioningState": "Succeeded"}}
    elif args[:3] == ["deployment", "group", "what-if"]:
        result = {"changes": [{"resourceId": i, "changeType": "Create"} for i in state["ids"]]}
    elif args[0] == "rest" and option("--method") == "get":
        url = option("--url")
        identifier = url.removeprefix("https://management.azure.com").split("?")[0]
        if "prices.azure.com" in url:
            assert "--skip-authorization-header" in args
            result = {"Items": [], "NextPageLink": None}
        elif "/permissions?" in url:
            result = {"value": [{"actions": ["*"], "notActions": []}]}
        elif "networkSecurityPerimeterConfigurations" in url:
            result = {"value": []}
        else:
            result = state["roles"][identifier]
    else:
        raise AssertionError("unexpected or mutating CLI command")
    print(json.dumps(result))


def test_real_network_wrapper_defaults_to_nonmutating_whatif(network_case):
    case = network_case
    root = Path(__file__).resolve().parents[3]
    for relative in (r"network\Deploy.ps1", r"shared\Azure.ps1"):
        destination = case.repo / "deploy" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "deploy" / relative, destination)
    state = case.repo / "deploy" / "out" / "stub-state.json"
    state.write_text(json.dumps({"ledgers": [v for _, v in case.ledgers.values()], "storage": case.storage,
                                 "roles": case.roles, "ids": case.ids}))
    stub = state.with_name("fake-az.py")
    stub.write_text(f"import sys\nsys.path.insert(0,{str(Path(__file__).parent)!r})\n"
                    "from test_network_deployment import _external_network_cli\n_external_network_cli()\n")
    log = state.with_name("calls.jsonl")
    environment = os.environ | {"ADVISOR_AZ_COMMAND": str(stub), "ADVISOR_PYTHON_COMMAND": sys.executable,
                                "NETWORK_STUB_STATE": str(state), "NETWORK_STUB_LOG": str(log)}
    command = shutil.which("pwsh")
    assert command
    before = {key: path.read_bytes() for key, (path, _) in case.ledgers.items()}
    result = subprocess.run([command, "-NoProfile", "-File", str(case.repo / "deploy" / "network" / "Deploy.ps1"),
                             "-ParametersFile", str(case.plan)], cwd=case.repo, env=environment,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))
    assert report["mode"] == "WhatIf" and report["access_mode"] == "Enforced" and not report["can_apply"]
    assert all(path.read_bytes() == before[key] for key, (path, _) in case.ledgers.items())
    assert not case.state_path.exists()
    assert all(not any(word in args for word in ("create", "update", "patch", "delete", "register"))
               for args in (json.loads(line) for line in log.read_text().splitlines()))


def test_manifest_network_allowance_is_only_the_aca_nsp_family(network_case):
    from advisor_deploy.manifest import experiment_resource
    case = network_case
    for identifier in case.ids:
        assert experiment_resource(identifier) == identifier
    for identifier in (
        case.ids[0].replace("aca-exp", "flex-exp"),
        case.ids[0].replace("networkSecurityPerimeters", "virtualNetworks"),
        case.ids[0] + "/links/foreign",
        case.ids[0] + "/providers/Microsoft.Network/virtualNetworks/foreign",
    ):
        with pytest.raises(ValueError):
            experiment_resource(identifier)

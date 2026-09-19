"""Guarded, receipt-owned Enforced NSP for the two experiment Storage accounts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import time
from urllib.parse import quote

from advisor_deploy.config import SUBSCRIPTION
from advisor_deploy.manifest import (
    ROLES, arm_parts, fail, group_id, load_manifest, read_json, timestamp, write_manifest,
)
from advisor_deploy.paths import has_link
from advisor_deploy.render import digest


ARM = "https://management.azure.com"
NETWORK_API = "2024-07-01"
STORAGE_API = "2025-01-01"
LOCATION = "southeastasia"
SUB_ID = "/subscriptions/" + SUBSCRIPTION
PRICING_URL = "https://prices.azure.com/api/retail/prices?$filter=" + quote(
    "serviceFamily eq 'Networking' and contains(productName, 'Network Security Perimeter')"
)
STORAGE_PATCH = {"properties": {
    "publicNetworkAccess": "SecuredByPerimeter",
    "networkAcls": {"defaultAction": "Deny", "bypass": "None", "ipRules": [],
                    "virtualNetworkRules": [], "resourceAccessRules": []},
}}


def _same(left, right):
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


def _region(value):
    return str(value).replace(" ", "").lower()


def _list(value, code):
    if not isinstance(value, dict) or value.get("nextLink") or not isinstance(value.get("value"), list):
        raise fail(code)
    return value["value"]


def _plan(path, repo):
    data = read_json(path)
    required = {"format", "aca_manifest", "functions_manifest"}
    if (set(data) - required - {"cost_review"} or not required <= data.keys()
            or data["format"] != "advisor-network-plan-v1"):
        raise fail("network_plan_fields")
    paths = {}
    for name in ("aca_manifest", "functions_manifest"):
        if not isinstance(data[name], str):
            raise fail("network_plan_path")
        joined = (path.parent / data[name]).absolute()
        if has_link(joined):
            raise fail("network_plan_path")
        paths[name] = joined.resolve()
        if not paths[name].is_relative_to(repo / "deploy" / "out"):
            raise fail("network_plan_path")
    ledgers = [load_manifest(paths[key]) for key in ("aca_manifest", "functions_manifest")]
    for ledger, target in zip(ledgers, ("aca", "functions")):
        if (ledger["target"] != target or ledger["status"] not in {"deployed", "infrastructure_ready"}
                or not ledger["discovery_complete"] or ledger["pending_role_assignment_ids"]):
            raise fail("network_owned_ready_ledgers_required")
    return data, paths, ledgers


def _ids(aca):
    name = "nsp-advisor-" + aca["run_id"].replace("-", "")[:16]
    perimeter = group_id("aca") + "/providers/Microsoft.Network/networkSecurityPerimeters/" + name
    profile = perimeter + "/profiles/storage"
    rule = profile + "/accessRules/owned-subscription"
    associations = [perimeter + "/resourceAssociations/" + value for value in ("aca-state", "flex-state")]
    return name, perimeter, profile, rule, associations


def _storage(cli, ledger):
    identifier = ledger["outputs"].get("storage_id")
    parts = arm_parts(identifier)
    if (len(parts) != 9 or [p.lower() for p in parts[6:8]] != ["microsoft.storage", "storageaccounts"]
            or identifier.lower() not in {i.lower() for i in ledger["owned_resource_ids"]}
            or parts[4].lower() != ledger["resource_group"]):
        raise fail("network_storage_not_owned")
    value = cli.run(["resource", "show", "--ids", identifier, "--api-version", STORAGE_API], "network_storage")
    props = value.get("properties", {})
    acl = props.get("networkAcls", {})
    if (not _same(value.get("id"), identifier) or _region(value.get("location")) != LOCATION
            or value.get("kind") != "StorageV2" or value.get("sku", {}).get("name") != "Standard_LRS"
            or props.get("allowSharedKeyAccess") is not False or props.get("allowBlobPublicAccess") is not False
            or props.get("supportsHttpsTrafficOnly") is not True or props.get("minimumTlsVersion") != "TLS1_2"
            or props.get("publicNetworkAccess") not in {"Disabled", "SecuredByPerimeter"}
            or acl.get("bypass") != "None" or any(acl.get(key) for key in ("ipRules", "virtualNetworkRules", "resourceAccessRules"))):
        raise fail("network_storage_security_mismatch")
    host_roles = {
        ROLES["blob"] if ledger["target"] == "aca" else ROLES["blob_owner"], ROLES["queue"],
    }
    records = [r for r in ledger["role_assignments"]
               if _same(r["scope"], identifier) or r["scope"].lower().startswith(identifier.lower() + "/")]
    actual_host_roles = {r["role_definition_id"].rsplit("/", 1)[1].lower() for r in records
                         if _same(r["scope"], identifier) and r["principal_id"] == ledger["outputs"]["identity_principal_id"]}
    if not host_roles <= actual_host_roles:
        raise fail("network_storage_rbac_missing")
    for record in records:
        live = cli.rest("get", ARM + record["id"] + "?api-version=2022-04-01", "network_storage_role")
        properties = live.get("properties", {})
        if (not _same(live.get("id", record["id"]), record["id"])
                or not _same(properties.get("principalId"), record["principal_id"])
                or not _same(properties.get("roleDefinitionId"), record["role_definition_id"])):
            raise fail("network_storage_rbac_drift")
    return value


def _pricing(cli, review):
    result = cli.run(["rest", "--method", "get", "--url", PRICING_URL, "--skip-authorization-header"], "network_retail_pricing")
    if not isinstance(result, dict) or result.get("NextPageLink") or not isinstance(result.get("Items"), list):
        raise fail("network_pricing_unverified")
    columns = ("meterId", "productName", "skuName", "meterName", "armRegionName", "currencyCode",
               "retailPrice", "unitOfMeasure", "effectiveStartDate")
    items = [{key: item.get(key) for key in columns} for item in result["Items"]]
    items.sort(key=lambda item: json.dumps(item, sort_keys=True))
    evidence = {"source": PRICING_URL, "items": items}
    fingerprint = digest(evidence)
    approved = False
    if isinstance(review, dict) and set(review) == {
        "pricing_sha256", "reviewed_at", "acknowledge_unpublished_meter_risk", "acknowledge_existing_service_costs",
    }:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(review["reviewed_at"].replace("Z", "+00:00"))
            approved = (timedelta(0) <= age <= timedelta(hours=24) and review["pricing_sha256"] == fingerprint
                        and review["acknowledge_unpublished_meter_risk"] is True
                        and review["acknowledge_existing_service_costs"] is True)
        except (ValueError, TypeError, AttributeError):
            pass
    return {"sha256": fingerprint, "meter_count": len(items), "retail_matches": items, "source": PRICING_URL,
            "zero_cost_verified": False, "review_accepted": approved}


def _rule(properties):
    subscriptions = properties.get("subscriptions")
    if (properties.get("direction") != "Inbound" or not isinstance(subscriptions, list) or len(subscriptions) != 1
            or not _same(subscriptions[0].get("id"), SUB_ID)
            or any(properties.get(key) for key in (
                "addressPrefixes", "fullyQualifiedDomainNames", "serviceTags", "emailAddresses", "phoneNumbers",
            ))):
        raise fail("network_subscription_only_rule_required")


def _effective(cli, storage_id, perimeter, association_name, *, required):
    value = cli.rest("get", ARM + storage_id + "/networkSecurityPerimeterConfigurations?api-version=" + STORAGE_API,
                     "network_storage_effective")
    configurations = _list(value, "network_effective_configuration_unverified")
    if not configurations:
        if required:
            raise fail("network_effective_configuration_pending")
        return
    if len(configurations) != 1:
        raise fail("network_foreign_storage_association")
    configuration = configurations[0]
    props = configuration.get("properties", {})
    association, profile = props.get("resourceAssociation", {}), props.get("profile", {})
    if (not str(configuration.get("id", "")).lower().startswith(storage_id.lower() + "/networksecurityperimeterconfigurations/")
            or not _same(props.get("networkSecurityPerimeter", {}).get("id"), perimeter)
            or association.get("name") != association_name or association.get("accessMode") != "Enforced"
            or profile.get("name") != "storage" or props.get("provisioningIssues")):
        raise fail("network_effective_binding_mismatch")
    if props.get("provisioningState") != "Succeeded":
        raise fail("network_effective_configuration_pending")
    rules = profile.get("accessRules")
    if not isinstance(rules, list) or len(rules) != 1 or rules[0].get("name") != "owned-subscription":
        raise fail("network_effective_rule_mismatch")
    _rule(rules[0].get("properties", {}))


def _readback(cli, aca, ids, storage_ids, *, wait=False):
    perimeter, profile, rule, *associations = ids
    root = cli.rest("get", ARM + perimeter + "?api-version=" + NETWORK_API, "network_perimeter_readback")
    if (not _same(root.get("id"), perimeter) or _region(root.get("location")) != LOCATION
            or root.get("tags", {}).get("advisorDeploymentId") != aca["run_id"]
            or root.get("properties", {}).get("provisioningState") != "Succeeded"):
        raise fail("network_perimeter_readback_mismatch")
    for suffix, expected in (("/profiles", [profile]), ("/profiles/storage/accessRules", [rule]),
                             ("/resourceAssociations", associations)):
        rows = _list(cli.rest("get", ARM + perimeter + suffix + "?api-version=" + NETWORK_API, "network_children"),
                     "network_children_unverified")
        if {str(row.get("id", "")).lower() for row in rows} != {i.lower() for i in expected} or len(rows) != len(expected):
            raise fail("network_unexpected_children")
        for row in rows:
            props = row.get("properties", {})
            issues = props.get("hasProvisioningIssues")
            no_issues = issues is None or issues is False or issues == "no"
            if props.get("provisioningState", "Succeeded") != "Succeeded" or not no_issues:
                raise fail("network_child_not_ready")
            if row["id"].lower() == rule.lower():
                _rule(props)
            elif row["id"].lower() in {i.lower() for i in associations}:
                index = next(i for i, identifier in enumerate(associations) if _same(row["id"], identifier))
                if (props.get("accessMode") != "Enforced" or not _same(props.get("profile", {}).get("id"), profile)
                        or not _same(props.get("privateLinkResource", {}).get("id"), storage_ids[index])):
                    raise fail("network_association_readback_mismatch")
    deadline = time.monotonic() + (300 if wait else 0)
    while True:
        try:
            for identifier, association in zip(storage_ids, associations):
                _effective(cli, identifier, perimeter, association.rsplit("/", 1)[1], required=True)
            return
        except ValueError as error:
            if str(error) != "deployment guard: network_effective_configuration_pending" or time.monotonic() >= deadline:
                raise
            time.sleep(5)


def _save_state(path, value, private):
    if has_link(path):
        raise fail("network_state_link")
    private.json(value).replace(path)


def _append_receipts(cli, aca, aca_path, repo, deployment, expected, state):
    record = cli.run(["deployment", "group", "show", "--resource-group", aca["resource_group"], "--name", deployment,
                      "--query", "{properties:{provisioningState:properties.provisioningState,outputResources:properties.outputResources}}"],
                     "network_receipts")
    resources = record.get("properties", {}).get("outputResources")
    if resources is None:
        operations = cli.run(["deployment", "operation", "group", "list", "--resource-group", aca["resource_group"],
                             "--name", deployment, "--query",
                             "[].{properties:{provisioningOperation:properties.provisioningOperation,"
                             "provisioningState:properties.provisioningState,targetResource:properties.targetResource}}"],
                            "network_operation_receipts")
        if not isinstance(operations, list):
            raise fail("network_receipts_unavailable")
        resources = [item["properties"]["targetResource"] for item in operations
                     if item.get("properties", {}).get("provisioningOperation") == "Create"
                     and item["properties"].get("provisioningState") == "Succeeded"]
    if not isinstance(resources, list):
        raise fail("network_receipts_unavailable")
    received = {str(item.get("id", "")).lower() for item in resources if isinstance(item, dict)}
    allowed = {identifier.lower(): identifier for identifier in expected}
    proven = {allowed[key] for key in received & allowed.keys()}
    updated = deepcopy(aca)
    updated["owned_resource_ids"] = sorted(set(updated["owned_resource_ids"]) | proven)
    updated["updated_at"] = timestamp()
    if received - allowed.keys():
        updated["discovery_complete"] = False
    # Never overwrite a main-app edit made while the network deployment ran.
    if read_json(aca_path) != aca:
        raise fail("network_ownership_changed")
    write_manifest(aca_path, updated, repo)
    aca.clear()
    aca.update(updated)
    state["resource_ids"] = sorted(set(state["resource_ids"]) | proven)
    if received - allowed.keys() or set(state["resource_ids"]) != set(expected):
        raise fail("network_receipt_scope_or_completeness")


def deploy_network(path, repo, cli, private, supplied_confirmation):
    from advisor_deploy.operations import account, assert_owned_group, confirmation, require_permissions
    data, paths, ledgers = _plan(path, repo)
    aca, flex = ledgers
    context = account(cli)
    if any(ledger["tenant_id"] != context["tenantId"] for ledger in ledgers):
        raise fail("network_tenant_mismatch")
    for ledger in ledgers:
        assert_owned_group(cli, ledger)
    provider = cli.run(["provider", "show", "--namespace", "Microsoft.Network"], "network_provider")
    rows = [row for row in provider.get("resourceTypes", []) if row.get("resourceType", "").lower() == "networksecurityperimeters"]
    if (provider.get("registrationState") != "Registered" or len(rows) != 1
            or LOCATION not in {_region(value) for value in rows[0].get("locations", [])}
            or NETWORK_API not in rows[0].get("apiVersions", [])):
        raise fail("network_region_or_provider_unavailable")
    require_permissions(cli, group_id("aca"), [
        "Microsoft.Resources/deployments/write", "Microsoft.Network/networkSecurityPerimeters/write",
        "Microsoft.Network/networkSecurityPerimeters/profiles/write",
        "Microsoft.Network/networkSecurityPerimeters/profiles/accessRules/write",
        "Microsoft.Network/networkSecurityPerimeters/resourceAssociations/write",
    ])
    storage = [_storage(cli, ledger) for ledger in ledgers]
    storage_ids = [item["id"] for item in storage]
    if len({value.lower() for value in storage_ids}) != 2:
        raise fail("network_distinct_storage_required")
    for identifier in storage_ids:
        require_permissions(cli, identifier, ["Microsoft.Storage/storageAccounts/read", "Microsoft.Storage/storageAccounts/write",
                                             "Microsoft.Storage/storageAccounts/joinPerimeter/action"])
    name, perimeter, profile, rule, associations = _ids(aca)
    ids = [perimeter, profile, rule, *associations]
    roots = [i for i in aca["owned_resource_ids"] if "/providers/microsoft.network/networksecurityperimeters/" in i.lower()
             and len(arm_parts(i)) == 9]
    if roots and (len(roots) != 1 or not _same(roots[0], perimeter)):
        raise fail("network_perimeter_collision")
    if roots and not {i.lower() for i in ids} <= {i.lower() for i in aca["owned_resource_ids"]}:
        raise fail("network_prior_receipts_incomplete")
    for identifier, association in zip(storage_ids, associations):
        _effective(cli, identifier, perimeter, association.rsplit("/", 1)[1], required=bool(roots))
    if roots:
        _readback(cli, aca, ids, storage_ids)
    pricing = _pricing(cli, data.get("cost_review"))
    template = repo / "deploy" / "network" / "infra" / "main.bicep"
    source = template.read_bytes()
    compiled = cli.run(["bicep", "build", "--file", str(template), "--stdout"], "network_compile", raw=True)
    try:
        artifact = json.loads(compiled)
    except (ValueError, TypeError):
        raise fail("network_compilation_invalid") from None
    if template.read_bytes() != source:
        raise fail("network_template_changed")
    template_path = private.json(artifact)
    parameters = {"location": LOCATION, "perimeterName": name, "ownerRunId": aca["run_id"],
                  "acaStorageId": storage_ids[0], "flexStorageId": storage_ids[1], "inboundSubscriptionId": SUB_ID}
    parameter_path = private.json({"parameters": {key: {"value": value} for key, value in parameters.items()}})
    deployment = "advisor-network-" + aca["run_id"]
    common = ["--resource-group", aca["resource_group"], "--name", deployment,
              "--template-file", str(template_path), "--parameters", "@" + str(parameter_path)]
    cli.run(["deployment", "group", "validate", *common], "network_arm_validate")
    preview = cli.run(["deployment", "group", "what-if", *common, "--no-pretty-print", "--result-format", "ResourceIdOnly"],
                      "network_arm_whatif")
    changes = preview.get("changes")
    if not isinstance(changes, list):
        raise fail("network_preview_unverified")
    owned = {identifier.lower() for identifier in aca["owned_resource_ids"]}
    expected = {identifier.lower() for identifier in ids}
    for change in changes:
        identifier, action = str(change.get("resourceId", "")).lower(), change.get("changeType")
        if identifier in expected and action != "Create" and identifier not in owned:
            raise fail("network_unowned_collision")
        if action in {"NoChange", "Ignore"} and identifier in owned:
            continue
        if identifier not in expected or action not in {"Create", "Modify", "Deploy"}:
            raise fail("network_preview_scope")
        if action in {"Modify", "Deploy"} and identifier not in owned:
            raise fail("network_unowned_collision")
    state_path = paths["aca_manifest"].with_suffix(".network.json")
    state = {"format": "advisor-network-deployment-v1", "run_id": aca["run_id"], "phase": "planned",
             "perimeter_id": perimeter, "profile_id": profile, "rule_id": rule, "association_ids": associations,
             "storage_ids": storage_ids, "resource_ids": sorted(i for i in ids if i.lower() in owned),
             "deployment_name": deployment, "pricing_sha256": pricing["sha256"],
             "access_mode": "Enforced", "inbound_subscription_id": SUB_ID,
             "storage_public_network_access": "SecuredByPerimeter",
             "source_template_sha256": sha256(source).hexdigest(), "compiled_template_sha256": digest(artifact)}
    if state_path.exists():
        old = read_json(state_path)
        if any(old.get(key) != state[key] for key in ("format", "run_id", "perimeter_id", "profile_id", "rule_id",
                                                     "association_ids", "storage_ids", "deployment_name")):
            raise fail("network_state_collision")
    intent = {"source": sha256(source).hexdigest(), "compiled": digest(artifact), "storage": storage,
              "aca": digest(aca), "flex": digest(flex), "pricing": pricing["sha256"], "cost_review": data.get("cost_review"),
              "parameters": parameters, "storage_patch": STORAGE_PATCH}
    token = confirmation("NETWORK", "aca", aca["run_id"], intent)
    report = {"ok": True, "mode": cli.mode, "confirmation": token, "can_apply": pricing["review_accepted"],
              "pricing": pricing, "storage_ids": storage_ids, "resource_ids": ids, "access_mode": "Enforced",
              "storage_patch": deepcopy(STORAGE_PATCH), "data_plane_validation": "not_run"}
    if cli.mode == "WhatIf":
        return report
    if not pricing["review_accepted"]:
        raise fail("network_cost_review_required")
    if supplied_confirmation != token:
        raise fail("network_confirmation_required")
    if read_json(paths["aca_manifest"]) != aca or read_json(paths["functions_manifest"]) != flex:
        raise fail("network_ownership_changed")
    state["phase"] = "applying"
    _save_state(state_path, state, private)
    created = False
    try:
        created = True
        cli.run(["deployment", "group", "create", *common, "--mode", "Incremental"], "network_apply", mutation=True)
        _append_receipts(cli, aca, paths["aca_manifest"], repo, deployment, ids, state)
        state["phase"] = "receipts_recorded"
        _save_state(state_path, state, private)
        _readback(cli, aca, ids, storage_ids, wait=True)
        for identifier in storage_ids:
            cli.rest("patch", ARM + identifier + "?api-version=" + STORAGE_API, "network_storage_intent",
                     body=deepcopy(STORAGE_PATCH))
        for identifier in storage_ids:
            value = cli.rest("get", ARM + identifier + "?api-version=" + STORAGE_API, "network_storage_readback")
            props = value.get("properties", {})
            acl = props.get("networkAcls", {})
            if (not _same(value.get("id"), identifier) or props.get("publicNetworkAccess") != "SecuredByPerimeter"
                    or props.get("allowSharedKeyAccess") is not False or props.get("allowBlobPublicAccess") is not False
                    or props.get("supportsHttpsTrafficOnly") is not True
                    or acl.get("defaultAction") != "Deny" or acl.get("bypass") != "None"
                    or any(acl.get(key) not in (None, []) for key in ("ipRules", "virtualNetworkRules", "resourceAccessRules"))):
                raise fail("network_storage_policy_or_security_readback")
        _readback(cli, aca, ids, storage_ids)
        state["phase"] = "network_verified"
        state["verified_at"] = timestamp()
        _save_state(state_path, state, private)
        return report | {"status": "network_verified"}
    except Exception as primary:
        if created and state["phase"] == "applying":
            try:
                _append_receipts(cli, aca, paths["aca_manifest"], repo, deployment, ids, state)
            except Exception:
                state["receipt_recovery"] = "incomplete"
        state["phase"] = "failed"
        try:
            _save_state(state_path, state, private)
        except Exception:
            primary.add_note("Network state persistence failed; original failure retained.")
        raise

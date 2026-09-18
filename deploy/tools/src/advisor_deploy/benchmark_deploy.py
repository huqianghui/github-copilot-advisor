"""Benchmark-only group deployment, with receipt-backed append to the ACA ledger."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
import json
import re

from advisor_deploy.manifest import (
    ROLES, SUBSCRIPTION, arm_parts, fail, group_id, guid, load_manifest,
    read_json, timestamp, validate_manifest, validate_role, write_manifest,
)
from advisor_deploy.paths import has_link
from advisor_deploy.render import digest


STATE_FIELDS = {
    "format", "run_id", "environment_name", "phase", "identity_id", "identity_client_id", "identity_principal_id",
    "planned_app_id", "planned_app_name", "planned_endpoint", "app_endpoint", "resource_ids",
    "role_assignment_ids", "context_digest", "run_plan_digest", "image_id", "image_reference",
    "base_image_digest", "last_deployment_name",
}


def _role_scope(identifier):
    parts = arm_parts(identifier)
    if [part.lower() for part in parts[-4:-1]] != ["providers", "microsoft.authorization", "roleassignments"]:
        raise fail("invalid_benchmark_role_id")
    guid(parts[-1])
    return "/".join(parts[:-4])


def _bench_resource(identifier, stage, container):
    parts = arm_parts(identifier)
    if parts[4].lower() != "rg-copilot-advisor-aca-exp":
        raise fail("benchmark_scope_mismatch")
    if identifier.lower() == container.lower():
        return True
    if len(parts) >= 9:
        kind = (parts[6] + "/" + parts[7]).lower()
        if kind == "microsoft.managedidentity/userassignedidentities" and len(parts) == 9:
            return parts[8].startswith("id-bench-")
        if kind == "microsoft.app/containerapps" and stage == "App":
            return parts[8].startswith("ca-bench-") and (
                len(parts) == 9 or (len(parts) == 11 and parts[-2:] == ["authConfigs", "current"])
            )
    return False


def validate_preview(changes, ledger, state, stage, registry, container):
    if not isinstance(changes, list):
        raise fail("benchmark_whatif_unverified")
    previous = set((state or {}).get("resource_ids", [])) | set((state or {}).get("role_assignment_ids", []))
    protected = {value.lower() for value in ledger["owned_resource_ids"]} - {value.lower() for value in previous}
    allowed = set(previous)
    for change in changes:
        if not isinstance(change, dict):
            raise fail("benchmark_whatif_unverified")
        identifier, action = change.get("resourceId"), change.get("changeType")
        arm_parts(identifier)
        if action in {"NoChange", "Ignore"}:
            if "/providers/microsoft.authorization/roleassignments/" in identifier.lower() and identifier not in previous:
                raise fail("unowned_benchmark_collision")
            if _bench_resource(identifier, "App", container) and identifier not in previous:
                if identifier.lower() == container.lower() and identifier.lower() in protected:
                    allowed.add(identifier)
                else:
                    raise fail("unowned_benchmark_collision")
            continue
        if action not in {"Create", "Modify"} or identifier.lower() in protected:
            raise fail("main_aca_resource_change_forbidden")
        if action == "Modify" and identifier not in previous:
            raise fail("unowned_benchmark_collision")
        if "/providers/microsoft.authorization/roleassignments/" in identifier.lower():
            if _role_scope(identifier).lower() not in {registry.lower(), container.lower()}:
                raise fail("benchmark_role_scope_mismatch")
        elif not _bench_resource(identifier, stage, container):
            raise fail("unexpected_benchmark_resource")
        allowed.add(identifier)
    return allowed


def append_owned_receipts(ledger, receipt_ids, approved_ids, role_records,
                         identity_id, principal_id, registry_id, container_id):
    if not set(receipt_ids) <= set(approved_ids):
        raise fail("unapproved_benchmark_receipt")
    result = deepcopy(ledger)
    resources = {value for value in receipt_ids if "/providers/microsoft.authorization/roleassignments/" not in value.lower()}
    result["owned_resource_ids"] = sorted(set(result["owned_resource_ids"]) | resources)
    for role in role_records:
        expected = ROLES["pull"] if role["scope"].lower() == registry_id.lower() else ROLES["blob"]
        if (
            role["id"] not in receipt_ids or role["identity_id"] != identity_id or role["principal_id"] != principal_id
            or role["scope"].lower() not in {registry_id.lower(), container_id.lower()}
            or role["role_definition_id"].split("/")[-1].lower() != expected
        ):
            raise fail("benchmark_role_binding_mismatch")
        validate_role(role, result["owned_resource_ids"])
        result["role_assignments"] = [item for item in result["role_assignments"] if item["id"] != role["id"]] + [role]
        result["pending_role_assignment_ids"] = [value for value in result["pending_role_assignment_ids"] if value != role["id"]]
    result["updated_at"] = timestamp()
    return validate_manifest(result)


def _state(path, run_id, environment):
    if not path.exists():
        return {
            "format": "advisor-benchmark-deployment-v1", "run_id": run_id, "environment_name": environment,
            "phase": "planned", "identity_id": "", "identity_client_id": "", "identity_principal_id": "",
            "planned_app_id": "", "planned_app_name": "", "planned_endpoint": "", "app_endpoint": "",
            "resource_ids": [], "role_assignment_ids": [], "context_digest": "", "run_plan_digest": "",
            "image_id": "", "image_reference": "", "base_image_digest": "", "last_deployment_name": "",
        }
    value = read_json(path)
    if set(value) != STATE_FIELDS or value["format"] != "advisor-benchmark-deployment-v1" or value["run_id"] != run_id or value["environment_name"] != environment:
        raise fail("benchmark_state_collision")
    for name in ("identity_client_id", "identity_principal_id"):
        if value[name]:
            guid(value[name])
    for name in ("resource_ids", "role_assignment_ids"):
        if not isinstance(value[name], list) or len(value[name]) > 20:
            raise fail("invalid_benchmark_state")
        for identifier in value[name]:
            arm_parts(identifier)
    if value["phase"] not in {"planned", "bootstrapped", "published", "app_deployed", "failed"}:
        raise fail("invalid_benchmark_state_phase")
    if value["planned_app_id"] and (
        not re.fullmatch(r"ca-bench-[a-z0-9-]+", value["planned_app_name"])
        or value["planned_app_id"] != group_id("aca") + "/providers/Microsoft.App/containerApps/" + value["planned_app_name"]
    ):
        raise fail("invalid_benchmark_state_app")
    return value


def _save_state(path, value, repo):
    if not path.is_relative_to(repo / "deploy" / "out") or path.suffix != ".json" or has_link(path):
        raise fail("benchmark_state_path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".next")
    if temporary.exists() or has_link(temporary):
        raise fail("benchmark_state_write_collision")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, sort_keys=True, indent=2)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare(path, repo, cli):
    from advisor_deploy.operations import account, assert_owned_group, _graph_owner
    data = read_json(path)
    required = {"format", "run_id", "environment_name", "aca_manifest", "functions_manifest", "api_manifest", "state_file"}
    if set(data) - required - {"context"} or not required <= data.keys() or data["format"] != "advisor-bench-deploy-v1":
        raise fail("invalid_benchmark_deployment_plan")
    run_id = guid(data["run_id"])
    environment = data["environment_name"]
    if not isinstance(environment, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", environment):
        raise fail("invalid_benchmark_environment")
    paths = {}
    for name in ("aca_manifest", "functions_manifest", "api_manifest", "state_file", "context"):
        if name in data:
            if not isinstance(data[name], str):
                raise fail("invalid_benchmark_plan_path")
            paths[name] = (path.parent / data[name]).absolute()
            if has_link(paths[name]) or not paths[name].is_relative_to(repo / "deploy" / "out"):
                raise fail("benchmark_plan_path_scope")
    aca, functions, api = (load_manifest(paths[name]) for name in ("aca_manifest", "functions_manifest", "api_manifest"))
    if aca["target"] != "aca" or functions["target"] != "functions" or api["target"] != "benchmark" or aca["run_id"] != run_id:
        raise fail("benchmark_owner_scope")
    context = account(cli)
    if any(value["tenant_id"] != context["tenantId"] for value in (aca, functions, api)):
        raise fail("benchmark_tenant_mismatch")
    assert_owned_group(cli, aca)
    assert_owned_group(cli, functions)
    if not aca["discovery_complete"] or aca["pending_role_assignment_ids"]:
        raise fail("aca_ownership_discovery_incomplete")
    if not api["graph"] or not api["graph"]["service_principal_id"]:
        raise fail("owned_benchmark_api_required")
    _graph_owner(cli, api)
    state = _state(paths["state_file"], run_id, environment)
    if not set(state["resource_ids"]) <= set(aca["owned_resource_ids"]) or not set(state["role_assignment_ids"]) <= {role["id"] for role in aca["role_assignments"]}:
        raise fail("benchmark_state_not_in_aca_ledger")
    selected = {}
    for key, kind in (("registry", "Microsoft.ContainerRegistry/registries"), ("environment", "Microsoft.App/managedEnvironments")):
        candidates = [identifier for identifier in aca["owned_resource_ids"] if f"/providers/{kind}/".lower() in identifier.lower()]
        if len(candidates) != 1:
            raise fail("owned_benchmark_dependency_ambiguous")
        selected[key] = cli.run(["resource", "show", "--ids", candidates[0]], "benchmark_" + key)
        if selected[key].get("id", "").lower() != candidates[0].lower():
            raise fail("benchmark_dependency_mismatch")
    storage_id = aca["outputs"]["storage_id"]
    storage = cli.run(["resource", "show", "--ids", storage_id], "benchmark_storage")
    props = storage.get("properties", {})
    if (storage.get("id", "").lower() != storage_id.lower() or props.get("allowSharedKeyAccess") is not False
        or props.get("allowBlobPublicAccess") is not False or props.get("supportsHttpsTrafficOnly") is not True
        or selected["registry"].get("sku", {}).get("name") != "Basic"):
        raise fail("benchmark_private_owned_storage_or_registry_required")
    selected["storage"] = storage
    selected["container_id"] = storage_id + "/blobServices/default/containers/benchmark-results"
    for identifier in state["resource_ids"]:
        if not _bench_resource(identifier, "App", selected["container_id"]):
            raise fail("invalid_benchmark_state_resource")
    for identifier in state["role_assignment_ids"]:
        role = next(item for item in aca["role_assignments"] if item["id"] == identifier)
        if role["identity_id"] != state["identity_id"] or role["principal_id"] != state["identity_principal_id"]:
            raise fail("benchmark_state_role_binding")
    if state["identity_id"]:
        identity = cli.run(["identity", "show", "--ids", state["identity_id"]], "benchmark_bootstrap_identity")
        if (identity.get("clientId"), identity.get("principalId"), identity.get("tags", {}).get("experiment")) != (
            state["identity_client_id"], state["identity_principal_id"], "benchmark",
        ):
            raise fail("benchmark_bootstrap_identity_changed")
    return data, paths, aca, functions, api, state, selected


def _verify_context(path, repo, state, aca, functions, api):
    from advisor_deploy.bench.build import _locked_registry, _snapshot, verify_context
    from advisor_deploy.bench.plan import load_plan
    import tomllib
    manifest = verify_context(path)
    snapshot = _snapshot(repo)
    if manifest["source_sha256"] != snapshot or manifest["lock_sha256"] != snapshot["uv.lock"]:
        raise fail("benchmark_context_source_or_lock_mismatch")
    indexes = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]["index"]
    index = next((item["url"] for item in indexes if item.get("default")), None)
    if manifest["dependency_index"] != index:
        raise fail("benchmark_context_index_mismatch")
    expected = _locked_registry(tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8")))
    actual = {(item["name"], item["version"]): set(item["hashes"]) for item in manifest["registry_dependencies"]}
    if actual != expected:
        raise fail("benchmark_context_dependency_mismatch")
    for relative in ("Dockerfile", "runtime-config/benchmark-cases.json"):
        source = repo / "deploy" / "bench" / ("cases.json" if relative.startswith("runtime-config/") else relative)
        if (path / relative).read_bytes() != source.read_bytes():
            raise fail("benchmark_context_input_changed")
    run = load_plan(path / "run.json")
    validate_run_bindings(run, state, aca, functions, api)
    if not manifest["linux_imports_verified"] or not manifest["base_image_digest"] or not manifest["image_id"]:
        raise fail("benchmark_linux_image_verification_required")
    return manifest, run


def validate_run_bindings(run, state, aca, functions, api):
    if run.local_test or not state["identity_id"]:
        raise fail("benchmark_bootstrap_and_cloud_plan_required")
    selected = next((item for item in (aca, functions) if item["outputs"].get("app_id") == run.expected_target_id), None)
    if selected is None or selected["status"] != "deployed":
        raise fail("benchmark_target_not_owned_deployed_app")
    if (run.target_url.rstrip("/") != selected["outputs"]["endpoint"].rstrip("/")
        or run.deployment.artifact_sha256 != selected["artifact_digest"]
        or run.deployment.config_sha256 != selected["config_digest"]
        or run.callback_url != state["planned_endpoint"] + "/bench/replies"):
        raise fail("benchmark_plan_provenance_mismatch")
    graph = api["graph"]
    if (run.auth.tenant_id != aca["tenant_id"] or run.auth.audience != graph["client_id"]
        or run.auth.scope != "api://" + graph["client_id"] + "/.default"
        or run.auth.driver_client_id != state["identity_client_id"]
        or run.auth.operator_client_ids != [state["identity_client_id"]]
        or run.auth.operator_principal_ids != [state["identity_principal_id"]]
        or set(run.auth.target_client_ids) != {item["outputs"]["identity_client_id"] for item in (aca, functions)}
        or set(run.auth.target_principal_ids) != {item["outputs"]["identity_principal_id"] for item in (aca, functions)}
        or run.results.account_url.rstrip("/") != aca["outputs"]["blob_url"].rstrip("/")
        or run.results.container != "benchmark-results"):
        raise fail("benchmark_plan_identity_or_storage_mismatch")


def _parameters(data, state, selected, aca, functions, api, stage):
    return {
        "enabled": True, "deployApp": stage == "App", "location": "southeastasia",
        "environmentName": data["environment_name"], "environmentId": selected["environment"]["id"],
        "registryName": selected["registry"]["name"], "storageAccountName": selected["storage"]["name"],
        "image": state["image_reference"].split("/", 1)[1] if stage == "App" else "",
        "apiClientId": api["graph"]["client_id"], "audience": api["graph"]["client_id"],
        "operatorPrincipalIds": [state["identity_principal_id"]] if stage == "App" else [],
        "operatorClientIds": [state["identity_client_id"]] if stage == "App" else [],
        "workloadPrincipalIds": [item["outputs"]["identity_principal_id"] for item in (aca, functions)],
        "workloadClientIds": [item["outputs"]["identity_client_id"] for item in (aca, functions)],
        "targetEndpoint": "",
    }


def _record(cli, repo, paths, aca, state, selected, deployment, approved, outputs):
    record = cli.run(["deployment", "group", "show", "--resource-group", aca["resource_group"], "--name", deployment], "benchmark_receipts")
    receipts = record.get("properties", {}).get("outputResources")
    if not isinstance(receipts, list):
        raise fail("benchmark_receipts_unavailable")
    identifiers = {item["id"] for item in receipts}
    if not identifiers <= approved:
        raise fail("unapproved_benchmark_receipt")
    if outputs.get("results_storage_id") != selected["storage"]["id"] or outputs.get("results_container_id") != selected["container_id"]:
        raise fail("benchmark_storage_output_mismatch")
    identity_id = outputs["identity_id"]
    if identity_id not in identifiers and identity_id not in state["resource_ids"]:
        raise fail("benchmark_identity_receipt_missing")
    identity = cli.run(["identity", "show", "--ids", identity_id], "benchmark_identity_readback")
    if (identity.get("tags", {}).get("experiment") != "benchmark"
        or identity.get("principalId") != outputs["identity_principal_id"]
        or identity.get("clientId") != outputs["identity_client_id"]):
        raise fail("benchmark_identity_readback_mismatch")
    guid(identity["principalId"])
    guid(identity["clientId"])
    roles = []
    for identifier in outputs["created_role_assignment_ids"]:
        if identifier not in identifiers and identifier not in state["role_assignment_ids"]:
            raise fail("benchmark_role_receipt_missing")
        value = cli.rest("get", "https://management.azure.com" + identifier + "?api-version=2022-04-01", "benchmark_role_readback")
        roles.append({
            "id": identifier, "scope": _role_scope(identifier), "identity_id": identity_id,
            "principal_id": value.get("properties", {}).get("principalId"),
            "role_definition_id": value.get("properties", {}).get("roleDefinitionId"),
        })
    if len(roles) != 2:
        raise fail("benchmark_roles_incomplete")
    if {(role["scope"].lower(), role["role_definition_id"].split("/")[-1].lower()) for role in roles} != {
        (selected["registry"]["id"].lower(), ROLES["pull"]), (selected["container_id"].lower(), ROLES["blob"]),
    } or len({role["id"] for role in roles}) != 2:
        raise fail("benchmark_roles_incomplete")
    latest = load_manifest(paths["aca_manifest"])
    if (latest["run_id"], latest["target"]) != (aca["run_id"], "aca"):
        raise fail("aca_ledger_changed_during_benchmark")
    updated = append_owned_receipts(latest, identifiers, approved, roles, identity_id, identity["principalId"],
                                    selected["registry"]["id"], selected["container_id"])
    write_manifest(paths["aca_manifest"], updated, repo)
    planned = outputs["planned_endpoint"]
    domain = selected["environment"].get("properties", {}).get("defaultDomain")
    if planned != "https://" + outputs["planned_app_name"] + "." + str(domain):
        raise fail("benchmark_planned_endpoint_mismatch")
    if outputs["planned_app_id"] != group_id("aca") + "/providers/Microsoft.App/containerApps/" + outputs["planned_app_name"]:
        raise fail("benchmark_planned_app_id_mismatch")
    state.update({
        "identity_id": identity_id, "identity_client_id": identity["clientId"], "identity_principal_id": identity["principalId"],
        "planned_app_id": outputs["planned_app_id"], "planned_app_name": outputs["planned_app_name"],
        "planned_endpoint": planned, "app_endpoint": outputs["driver_endpoint"],
        "resource_ids": sorted(set(state["resource_ids"]) | {value for value in identifiers if "/providers/microsoft.authorization/roleassignments/" not in value.lower()}),
        "role_assignment_ids": sorted(set(state["role_assignment_ids"]) | {role["id"] for role in roles}),
        "last_deployment_name": deployment,
    })
    _save_state(paths["state_file"], state, repo)
    return updated


def deploy_benchmark(path, repo, cli, private, stage, supplied_confirmation):
    from advisor_deploy.operations import confirmation, deployment_arguments, require_permissions, assert_owned_group, assert_known_graph_grants
    data, paths, aca, functions, api, state, selected = _prepare(path, repo, cli)
    if stage not in {"Bootstrap", "Publish", "App"}:
        raise fail("invalid_benchmark_stage")
    context = run = None
    if stage != "Bootstrap":
        if "context" not in paths:
            raise fail("benchmark_context_required")
        context, run = _verify_context(paths["context"], repo, state, aca, functions, api)
        if len(api["graph"]["grants"]) != 3 or state["identity_principal_id"] not in {
            grant["principal_id"] for grant in api["graph"]["grants"]
        }:
            raise fail("benchmark_three_grants_required")
        grants = cli.rest("get", "https://graph.microsoft.com/v1.0/servicePrincipals/" + api["graph"]["service_principal_id"] + "/appRoleAssignedTo",
                          "benchmark_live_grants")
        if grants.get("@odata.nextLink") or len(grants.get("value", [])) != 3:
            raise fail("benchmark_three_grants_required")
        assert_known_graph_grants(grants["value"], api["graph"])
    template = repo / "deploy" / "bench" / "infra" / "main.bicep"
    template_bytes = template.read_bytes()
    intent = {
        "template": sha256(template_bytes).hexdigest(), "environment": data["environment_name"],
        "registry": selected["registry"]["id"], "environment_id": selected["environment"]["id"],
        "storage": selected["storage"]["id"], "api_client_id": api["graph"]["client_id"],
        "context": digest(context) if context else None,
        "image_id": context["image_id"] if context else None,
        "image_reference": state["image_reference"] if stage == "App" else None,
    }
    token = confirmation("BENCH-" + stage.upper(), "aca", aca["run_id"], intent)
    report = {"ok": True, "mode": cli.mode, "stage": stage, "scope": "resourceGroup",
              "confirmation": token, "application_validation": "not_run"}
    if stage == "Publish":
        if cli.mode == "WhatIf":
            return report | {"image_id": context["image_id"], "context_digest": digest(context)}
        if supplied_confirmation != token:
            raise fail("benchmark_confirmation_required")
        require_permissions(cli, selected["registry"]["id"], ["Microsoft.ContainerRegistry/registries/push/write"])
        if cli.run(["info", "--format", "{{.OSType}}"], "benchmark_docker_readiness", tool="docker", raw=True) != "linux":
            raise fail("benchmark_linux_engine_required")
        image = cli.run(["image", "inspect", context["image_id"]], "benchmark_exact_image", tool="docker", raw=True)
        try:
            info = json.loads(image)[0]
        except (ValueError, TypeError, IndexError):
            raise fail("benchmark_image_inspect_invalid") from None
        if (info.get("Id"), info.get("Os"), info.get("Architecture")) != (context["image_id"], "linux", "amd64"):
            raise fail("benchmark_exact_image_mismatch")
        expected_command = [
            "python", "-m", "advisor_deploy.bench", "run", "--plan", "/app/run.json",
            "--output", "/app/results", "--allow-cloud",
        ]
        if info.get("Config", {}).get("Cmd") != expected_command or info.get("Config", {}).get("User") != "10001":
            raise fail("benchmark_image_entrypoint_mismatch")
        payload_names = sorted(name for name in context["files"] if name not in {"Dockerfile", ".dockerignore"})
        probe = (
            "import hashlib,json,pathlib;"
            f"names={payload_names!r};"
            "print(json.dumps({name:hashlib.sha256(pathlib.Path('/app',name).read_bytes()).hexdigest() for name in names},sort_keys=True))"
        )
        payload = cli.run(["run", "--rm", "--network", "none", "--platform", "linux/amd64",
                           "--entrypoint", "python", context["image_id"], "-I", "-c", probe],
                          "benchmark_image_payload", tool="docker", mutation=True, raw=True)
        try:
            actual_payload = json.loads(payload)
        except ValueError:
            raise fail("benchmark_image_payload_unverified") from None
        if actual_payload != {name: context["files"][name] for name in payload_names}:
            raise fail("benchmark_image_payload_mismatch")
        login = cli.run(["acr", "login", "--name", selected["registry"]["name"], "--expose-token"], "benchmark_registry_token", mutation=True)
        server = login.get("loginServer")
        if server != selected["registry"].get("properties", {}).get("loginServer") or not re.fullmatch(r"[a-z0-9]+\.azurecr\.io", server or ""):
            raise fail("benchmark_registry_mismatch")
        (private.directory / "docker").mkdir()
        cli.run(["login", server, "--username", "00000000-0000-0000-0000-000000000000", "--password-stdin"],
                "benchmark_registry_login", tool="docker", mutation=True, raw=True, stdin=login["accessToken"] + "\n")
        tag = "advisor-bench:" + aca["run_id"] + "-" + digest(context)[:12]
        cli.run(["tag", context["image_id"], server + "/" + tag], "benchmark_image_tag", tool="docker", mutation=True, raw=True)
        pushed = cli.run(["push", server + "/" + tag], "benchmark_image_push", tool="docker", mutation=True, raw=True)
        digests = set(re.findall(r"digest:\s*(sha256:[a-f0-9]{64})", pushed))
        if len(digests) != 1:
            raise fail("benchmark_push_digest_unverified")
        remote_digest = digests.pop()
        remote = cli.run(["acr", "repository", "show", "--name", selected["registry"]["name"],
                          "--image", "advisor-bench@" + remote_digest], "benchmark_remote_digest")
        if remote.get("digest") != remote_digest:
            raise fail("benchmark_remote_digest_mismatch")
        state.update(phase="published", context_digest=digest(context), run_plan_digest=digest(run.model_dump()),
                     image_id=context["image_id"], image_reference=server + "/advisor-bench@" + remote_digest,
                     base_image_digest=context["base_image_digest"])
        _save_state(paths["state_file"], state, repo)
        return report | {"image_reference": state["image_reference"], "context_digest": state["context_digest"]}
    if stage == "App" and (
        state["phase"] not in {"published", "app_deployed"} or state["context_digest"] != digest(context)
        or state["run_plan_digest"] != digest(run.model_dump()) or state["image_id"] != context["image_id"]
        or not re.fullmatch(r"[a-z0-9]+\.azurecr\.io/advisor-bench@sha256:[a-f0-9]{64}", state["image_reference"])
    ):
        raise fail("benchmark_published_image_approval_required")
    require_permissions(cli, group_id("aca"), [
        "Microsoft.Resources/deployments/write", "Microsoft.ManagedIdentity/userAssignedIdentities/write",
        "Microsoft.Storage/storageAccounts/blobServices/containers/write", "Microsoft.Authorization/roleAssignments/write",
        *(["Microsoft.App/containerApps/write"] if stage == "App" else []),
    ])
    parameters = _parameters(data, state, selected, aca, functions, api, stage)
    if run is not None:
        parameters["targetEndpoint"] = run.target_url
    parameter_file = private.json({"parameters": {key: {"value": value} for key, value in parameters.items()}})
    sealed_template = private.directory / "benchmark-template.bicep"
    with sealed_template.open("xb") as stream:
        stream.write(template_bytes)
    template = sealed_template
    name = "advisor-bench-" + aca["run_id"] + "-" + stage.lower()
    cli.run(deployment_arguments("bench", "validate", template, parameter_file, name), "benchmark_template_validate")
    preview = cli.run(deployment_arguments("bench", "what-if", template, parameter_file, name), "benchmark_template_whatif")
    allowed = validate_preview(preview.get("changes"), aca, state, stage, selected["registry"]["id"], selected["container_id"])
    report["resource_ids"] = sorted(allowed)
    if cli.mode == "WhatIf":
        return report
    if supplied_confirmation != token:
        raise fail("benchmark_confirmation_required")
    assert_owned_group(cli, aca)
    if digest(load_manifest(paths["aca_manifest"])) != digest(aca):
        raise fail("aca_ledger_changed_before_benchmark_apply")
    aca["discovery_complete"] = False
    write_manifest(paths["aca_manifest"], aca, repo)
    try:
        result = cli.run(deployment_arguments("bench", "create", template, parameter_file, name), "benchmark_apply", mutation=True)
        outputs = {key: value["value"] for key, value in result.get("properties", {}).get("outputs", {}).items()}
        aca = _record(cli, repo, paths, aca, state, selected, name, allowed, outputs)
        aca["discovery_complete"] = True
        write_manifest(paths["aca_manifest"], aca, repo)
    except Exception:
        # Preserve exact, approved partial receipts; uncertain role bindings stay
        # pending and block cleanup/reuse instead of being attributed to app MI.
        aca = load_manifest(paths["aca_manifest"])
        if aca["run_id"] != data["run_id"] or aca["target"] != "aca":
            raise fail("aca_ledger_changed_during_benchmark")
        try:
            partial = cli.run(["deployment", "group", "show", "--resource-group", aca["resource_group"], "--name", name], "benchmark_failure_receipts")
            receipts = {item["id"] for item in partial.get("properties", {}).get("outputResources", [])}
            if not receipts <= allowed:
                raise fail("unapproved_benchmark_receipt")
            resources = {value for value in receipts if "/providers/microsoft.authorization/roleassignments/" not in value.lower()}
            roles = receipts - resources
            aca["owned_resource_ids"] = sorted(set(aca["owned_resource_ids"]) | resources)
            known_roles = {role["id"] for role in aca["role_assignments"]}
            aca["pending_role_assignment_ids"] = sorted(set(aca["pending_role_assignment_ids"]) | (roles - known_roles))
        finally:
            aca["discovery_complete"] = False
            write_manifest(paths["aca_manifest"], aca, repo)
            state["phase"], state["last_deployment_name"] = "failed", name
            _save_state(paths["state_file"], state, repo)
        raise
    if stage == "App":
        verify_benchmark_app(cli, state, selected, aca, functions, api)
    state["phase"] = "bootstrapped" if stage == "Bootstrap" else "app_deployed"
    _save_state(paths["state_file"], state, repo)
    assert_owned_group(cli, aca)
    return report | {key: state[key] for key in (
        "identity_id", "identity_client_id", "identity_principal_id", "planned_app_id",
        "planned_app_name", "planned_endpoint", "app_endpoint", "image_reference",
    )}


def verify_benchmark_app(cli, state, selected, aca, functions, api):
    app = cli.run(["resource", "show", "--ids", state["planned_app_id"], "--api-version", "2025-01-01"], "benchmark_app_readback")
    props = app.get("properties", {})
    identity = app.get("identity", {})
    template = props.get("template", {})
    containers = template.get("containers", [])
    if (
        identity.get("type") != "UserAssigned" or set(identity.get("userAssignedIdentities", {})) != {state["identity_id"]}
        or props.get("environmentId") != selected["environment"]["id"] or len(containers) != 1
        or containers[0].get("image") != state["image_reference"]
        or containers[0].get("resources", {}).get("cpu") != .5
        or containers[0].get("resources", {}).get("memory") != "1Gi"
        or template.get("scale", {}).get("minReplicas") != 1
        or template.get("scale", {}).get("maxReplicas") != 1
        or props.get("configuration", {}).get("activeRevisionsMode") != "Single"
    ):
        raise fail("benchmark_app_configuration_mismatch")
    ingress = props.get("configuration", {}).get("ingress", {})
    endpoint = "https://" + str(ingress.get("fqdn", ""))
    if endpoint != state["planned_endpoint"] or ingress.get("allowInsecure") is not False or ingress.get("external") is not True:
        raise fail("benchmark_endpoint_readback_mismatch")
    env = {item["name"]: item.get("value") for item in containers[0].get("env", [])}
    expected = {
        "AZURE_CLIENT_ID": state["identity_client_id"], "WEB_CONCURRENCY": "1", "BENCHMARK_AUTOSTART": "false",
        "ADVISOR_BLOB_SERVICE_URL": aca["outputs"]["blob_url"], "BENCHMARK_RESULTS_CONTAINER": "benchmark-results",
        "BENCHMARK_AUDIENCE": api["graph"]["client_id"], "BENCHMARK_REQUIRED_ROLE": "Benchmark.Invoke",
        "BENCHMARK_OPERATOR_CLIENT_IDS": state["identity_client_id"], "BENCHMARK_OPERATOR_PRINCIPAL_IDS": state["identity_principal_id"],
        "BENCHMARK_WORKLOAD_CLIENT_IDS": ",".join(value["outputs"]["identity_client_id"] for value in (aca, functions)),
        "BENCHMARK_WORKLOAD_PRINCIPAL_IDS": ",".join(value["outputs"]["identity_principal_id"] for value in (aca, functions)),
    }
    if any(env.get(key) != value for key, value in expected.items()):
        raise fail("benchmark_app_identity_settings_mismatch")
    auth = cli.run(["resource", "show", "--ids", state["planned_app_id"] + "/authConfigs/current", "--api-version", "2025-01-01"], "benchmark_auth_readback")
    properties = auth.get("properties", {})
    aad = properties.get("identityProviders", {}).get("azureActiveDirectory", {})
    if (properties.get("platform", {}).get("enabled") is not True
        or properties.get("globalValidation", {}).get("unauthenticatedClientAction") != "Return401"
        or aad.get("registration", {}).get("clientId") != api["graph"]["client_id"]
        or aad.get("validation", {}).get("allowedAudiences") != [api["graph"]["client_id"]]):
        raise fail("benchmark_auth_readback_mismatch")
    policy = aad.get("validation", {}).get("defaultAuthorizationPolicy", {})
    if (
        set(policy.get("allowedApplications", [])) != {state["identity_client_id"], *(value["outputs"]["identity_client_id"] for value in (aca, functions))}
        or set(policy.get("allowedPrincipals", {}).get("identities", [])) != {state["identity_principal_id"], *(value["outputs"]["identity_principal_id"] for value in (aca, functions))}
        or properties.get("httpSettings", {}).get("requireHttps") is not True
    ):
        raise fail("benchmark_auth_allowlist_mismatch")
    state["app_endpoint"] = endpoint


def control_benchmark(path, repo, cli, action, supplied_confirmation):
    from advisor_deploy.operations import confirmation, require_permissions
    data, paths, aca, functions, api, state, selected = _prepare(path, repo, cli)
    if state["phase"] != "app_deployed" or action not in {"start", "status"} or "context" not in paths:
        raise fail("benchmark_deployed_control_required")
    context, run = _verify_context(paths["context"], repo, state, aca, functions, api)
    if digest(context) != state["context_digest"] or digest(run.model_dump()) != state["run_plan_digest"]:
        raise fail("benchmark_control_plan_changed")
    verify_benchmark_app(cli, state, selected, aca, functions, api)
    require_permissions(cli, state["planned_app_id"], ["Microsoft.App/containerApps/read", "Microsoft.App/containerApps/exec/action"])
    command = "python -m advisor_deploy.bench.control --plan /app/run.json " + action
    exec_arguments = [
        "containerapp", "exec", "--subscription", SUBSCRIPTION,
        "--resource-group", aca["resource_group"], "--name", state["planned_app_name"],
        "--container", "driver-receiver", "--command", "sh",
    ]
    token = confirmation("BENCH-CONTROL", "aca", aca["run_id"], [action, state["image_reference"], state["run_plan_digest"]])
    report = {"ok": True, "mode": cli.mode, "action": action, "confirmation": token,
              "application_validation": "not_run", "command": command, "exec_arguments": exec_arguments,
              "identity_client_id": state["identity_client_id"], "control_invoked": False}
    if cli.mode == "WhatIf":
        return report
    if supplied_confirmation != token:
        raise fail("benchmark_control_confirmation_required")
    # Azure CLI exposes an interactive console, not a documented unattended
    # command runner. Do not fabricate a control result from captured console
    # output. The operator opens this exact owned console and runs the fixed
    # command; only the helper uses the driver MI's application token.
    return report | {"status": "awaiting_interactive_exec"}

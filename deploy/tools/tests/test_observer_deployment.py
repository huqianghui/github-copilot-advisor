"""Bounded observer deployment guards, independent of harness implementation."""

import copy
from types import SimpleNamespace
import json
from uuid import uuid4

import pytest

from advisor_deploy.manifest import ROLES, SUBSCRIPTION, group_id, new_manifest


GROUP = group_id("aca")
IDENTITY = GROUP + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-bench-fixture"
APP = GROUP + "/providers/Microsoft.App/containerApps/ca-bench-fixture"
REGISTRY = GROUP + "/providers/Microsoft.ContainerRegistry/registries/owned"
STORAGE = GROUP + "/providers/Microsoft.Storage/storageAccounts/ownedstate"
CONTAINER = STORAGE + "/blobServices/default/containers/benchmark-results"
ENVIRONMENT = GROUP + "/providers/Microsoft.App/managedEnvironments/owned"


def ownership():
    value = new_manifest("aca", str(uuid4()), "fixture", "a" * 64, "b" * 64)
    value["owned_resource_ids"] = [GROUP, REGISTRY, STORAGE, ENVIRONMENT]
    return value


def test_bootstrap_preview_forbids_main_resource_changes_and_new_accounts():
    from advisor_deploy.benchmark_deploy import validate_preview
    ledger = ownership()
    allowed = validate_preview(
        [{"resourceId": IDENTITY, "changeType": "Create"},
         {"resourceId": CONTAINER, "changeType": "Create"}],
        ledger, None, "Bootstrap", REGISTRY, CONTAINER,
    )
    assert allowed == {IDENTITY, CONTAINER}
    for identifier in (STORAGE, REGISTRY, ENVIRONMENT, GROUP + "/providers/Microsoft.Storage/storageAccounts/newstate"):
        with pytest.raises(ValueError):
            validate_preview([{"resourceId": identifier, "changeType": "Modify"}],
                             ledger, None, "Bootstrap", REGISTRY, CONTAINER)
    with pytest.raises(ValueError):
        validate_preview([{"resourceId": APP, "changeType": "Create"}],
                         ledger, None, "Bootstrap", REGISTRY, CONTAINER)


def test_resource_id_only_deploy_changes_require_previous_observer_ownership():
    from advisor_deploy.benchmark_deploy import validate_preview

    ledger = ownership()
    ledger["owned_resource_ids"] += [IDENTITY, CONTAINER]
    role = REGISTRY + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid4())
    state = {"resource_ids": [IDENTITY, CONTAINER], "role_assignment_ids": [role]}
    changes = [{"resourceId": value, "changeType": "Deploy"} for value in (IDENTITY, CONTAINER, role)]
    changes += [{"resourceId": STORAGE, "changeType": "Ignore"},
                {"resourceId": APP, "changeType": "Create"}]
    assert validate_preview(changes, ledger, state, "App", REGISTRY, CONTAINER) == {
        IDENTITY, CONTAINER, role, APP,
    }
    for identifier in (STORAGE, REGISTRY, IDENTITY + "-unrecorded", APP):
        with pytest.raises(ValueError):
            validate_preview([{"resourceId": identifier, "changeType": "Deploy"}],
                             ledger, state, "App", REGISTRY, CONTAINER)


def test_only_receipt_proven_driver_resources_and_roles_are_appended():
    from advisor_deploy.benchmark_deploy import append_owned_receipts
    ledger = ownership()
    original = copy.deepcopy(ledger)
    principal = str(uuid4())
    assignment = CONTAINER + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid4())
    role = {"id": assignment, "scope": CONTAINER, "identity_id": IDENTITY, "principal_id": principal,
            "role_definition_id": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{ROLES['blob']}"}
    updated = append_owned_receipts(ledger, {IDENTITY, CONTAINER, assignment}, {IDENTITY, CONTAINER, assignment},
                                    [role], IDENTITY, principal, REGISTRY, CONTAINER)
    assert set(updated["owned_resource_ids"]) == set(original["owned_resource_ids"]) | {IDENTITY, CONTAINER}
    assert updated["role_assignments"][-1]["identity_id"] == IDENTITY
    assert updated["role_assignments"][-1]["principal_id"] == principal
    assert ledger == original
    for key in ("outputs", "summary", "config_digest", "artifact_digest", "environment_name"):
        assert updated[key] == original[key]
    with pytest.raises(ValueError, match="unapproved_benchmark_receipt"):
        append_owned_receipts(ledger, {IDENTITY, STORAGE}, {IDENTITY}, [],
                              IDENTITY, principal, REGISTRY, CONTAINER)


def test_driver_role_cannot_be_bound_to_main_app_identity_or_wrong_scope():
    from advisor_deploy.benchmark_deploy import append_owned_receipts
    ledger = ownership()
    principal = str(uuid4())
    assignment = REGISTRY + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid4())
    role = {"id": assignment, "scope": REGISTRY, "identity_id": IDENTITY, "principal_id": str(uuid4()),
            "role_definition_id": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{ROLES['pull']}"}
    with pytest.raises(ValueError, match="benchmark_role_binding_mismatch"):
        append_owned_receipts(ledger, {IDENTITY, assignment}, {IDENTITY, assignment},
                              [role], IDENTITY, principal, REGISTRY, CONTAINER)


def run_bindings():
    driver_client, driver_principal, api_id, tenant = (str(uuid4()) for _ in range(4))
    aca = {"status": "deployed", "artifact_digest": "a" * 64, "config_digest": "b" * 64,
           "tenant_id": tenant, "outputs": {"app_id": GROUP + "/providers/Microsoft.App/containerApps/main",
                "endpoint": "https://target.example.net", "identity_client_id": str(uuid4()),
                "identity_principal_id": str(uuid4()), "blob_url": "https://ownedstate.blob.core.windows.net/"}}
    functions = {"status": "deployed", "artifact_digest": "c" * 64, "config_digest": "d" * 64,
                 "outputs": {"app_id": "functions-id", "identity_client_id": str(uuid4()), "identity_principal_id": str(uuid4())}}
    state = {"identity_id": IDENTITY, "identity_client_id": driver_client, "identity_principal_id": driver_principal,
             "planned_endpoint": "https://observer.example.net"}
    api = {"graph": {"client_id": api_id}}
    run = SimpleNamespace(
        local_test=False, expected_target_id=aca["outputs"]["app_id"], target_url=aca["outputs"]["endpoint"],
        callback_url=state["planned_endpoint"] + "/bench/replies",
        deployment=SimpleNamespace(artifact_sha256=aca["artifact_digest"], config_sha256=aca["config_digest"]),
        auth=SimpleNamespace(tenant_id=tenant, audience=api_id, scope=f"api://{api_id}/.default",
            driver_client_id=driver_client, operator_client_ids=[driver_client], operator_principal_ids=[driver_principal],
            target_client_ids=[aca["outputs"]["identity_client_id"], functions["outputs"]["identity_client_id"]],
            target_principal_ids=[aca["outputs"]["identity_principal_id"], functions["outputs"]["identity_principal_id"]]),
        results=SimpleNamespace(account_url=aca["outputs"]["blob_url"], container="benchmark-results"),
    )
    return run, state, aca, functions, api


def test_cloud_run_plan_is_bound_to_bootstrapped_driver_and_owned_target():
    from advisor_deploy.benchmark_deploy import validate_run_bindings
    run, state, aca, functions, api = run_bindings()
    validate_run_bindings(run, state, aca, functions, api)
    run.auth.operator_client_ids = [str(uuid4())]
    with pytest.raises(ValueError, match="benchmark_plan_identity_or_storage_mismatch"):
        validate_run_bindings(run, state, aca, functions, api)


@pytest.mark.parametrize("change", ["artifact", "callback", "storage", "driver", "local"])
def test_benchmark_image_cannot_embed_unrelated_or_stale_plan(change):
    from advisor_deploy.benchmark_deploy import validate_run_bindings
    run, state, aca, functions, api = run_bindings()
    if change == "artifact":
        run.deployment.artifact_sha256 = "e" * 64
    elif change == "callback":
        run.callback_url = "https://unrelated.example.net/bench/replies"
    elif change == "storage":
        run.results.account_url = "https://unrelated.blob.core.windows.net/"
    elif change == "driver":
        run.auth.driver_client_id = str(uuid4())
    else:
        run.local_test = True
    with pytest.raises(ValueError):
        validate_run_bindings(run, state, aca, functions, api)


def test_benchmark_readback_rejects_drifted_image_and_operator_allowlist():
    from advisor_deploy.benchmark_deploy import verify_benchmark_app
    run, state, aca, functions, api = run_bindings()
    state |= {"planned_app_id": APP, "image_reference": "owned.azurecr.io/advisor-bench@sha256:" + "c" * 64}
    selected = {"environment": {"id": ENVIRONMENT}}
    env = {
        "AZURE_CLIENT_ID": state["identity_client_id"], "WEB_CONCURRENCY": "1", "BENCHMARK_AUTOSTART": "false",
        "ADVISOR_BLOB_SERVICE_URL": aca["outputs"]["blob_url"], "BENCHMARK_RESULTS_CONTAINER": "benchmark-results",
        "BENCHMARK_AUDIENCE": api["graph"]["client_id"], "BENCHMARK_REQUIRED_ROLE": "Benchmark.Invoke",
        "BENCHMARK_OPERATOR_CLIENT_IDS": state["identity_client_id"],
        "BENCHMARK_OPERATOR_PRINCIPAL_IDS": state["identity_principal_id"],
        "BENCHMARK_WORKLOAD_CLIENT_IDS": ",".join(run.auth.target_client_ids),
        "BENCHMARK_WORKLOAD_PRINCIPAL_IDS": ",".join(run.auth.target_principal_ids),
    }
    app = {"identity": {"type": "UserAssigned", "userAssignedIdentities": {IDENTITY: {}}},
           "properties": {"environmentId": ENVIRONMENT,
              "configuration": {"activeRevisionsMode": "Single", "ingress": {
                  "fqdn": "observer.example.net", "allowInsecure": False, "external": True}},
              "template": {"scale": {"minReplicas": 1, "maxReplicas": 1}, "containers": [{
                  "image": state["image_reference"], "resources": {"cpu": .5, "memory": "1Gi"},
                  "env": [{"name": key, "value": value} for key, value in env.items()]}]}}}
    auth = {"properties": {"platform": {"enabled": True},
        "globalValidation": {"unauthenticatedClientAction": "Return401"},
        "httpSettings": {"requireHttps": True},
        "identityProviders": {"azureActiveDirectory": {
            "registration": {"clientId": api["graph"]["client_id"]},
            "validation": {"allowedAudiences": [api["graph"]["client_id"]],
                "defaultAuthorizationPolicy": {
                    "allowedApplications": [state["identity_client_id"], *run.auth.target_client_ids],
                    "allowedPrincipals": {"identities": [state["identity_principal_id"], *run.auth.target_principal_ids]}}}}}}}

    class Readback:
        def run(self, arguments, phase):
            return app if phase == "benchmark_app_readback" else auth

    verify_benchmark_app(Readback(), state, selected, aca, functions, api)
    resources = app["properties"]["template"]["containers"][0]["resources"]
    resources["ephemeralStorage"] = "2Gi"
    scale = app["properties"]["template"]["scale"]
    scale.update(cooldownPeriod=300, pollingInterval=30, rules=[])
    verify_benchmark_app(Readback(), state, selected, aca, functions, api)
    app["identity"]["userAssignedIdentities"] = {IDENTITY.replace("/resourceGroups/", "/resourcegroups/"): {}}
    app["properties"]["environmentId"] = ENVIRONMENT.lower()
    verify_benchmark_app(Readback(), state, selected, aca, functions, api)
    app["identity"]["userAssignedIdentities"] = {IDENTITY + "-unapproved": {}}
    with pytest.raises(ValueError, match="benchmark_app_configuration_mismatch"):
        verify_benchmark_app(Readback(), state, selected, aca, functions, api)
    app["identity"]["userAssignedIdentities"] = {IDENTITY: {}}
    for section, key, wrong in ((resources, "cpu", 1), (resources, "memory", "2Gi"),
                                (scale, "minReplicas", 0), (scale, "maxReplicas", 2)):
        original = section[key]
        section[key] = wrong
        with pytest.raises(ValueError, match="benchmark_app_configuration_mismatch"):
            verify_benchmark_app(Readback(), state, selected, aca, functions, api)
        section[key] = original
    app["properties"]["template"]["containers"][0]["image"] = "owned.azurecr.io/unapproved:latest"
    with pytest.raises(ValueError, match="benchmark_app_configuration_mismatch"):
        verify_benchmark_app(Readback(), state, selected, aca, functions, api)
    app["properties"]["template"]["containers"][0]["image"] = state["image_reference"]
    auth["properties"]["identityProviders"]["azureActiveDirectory"]["validation"]["defaultAuthorizationPolicy"]["allowedApplications"].append(str(uuid4()))
    with pytest.raises(ValueError, match="benchmark_auth_allowlist_mismatch"):
        verify_benchmark_app(Readback(), state, selected, aca, functions, api)


def test_publish_approves_exact_image_and_checks_in_image_plan_hashes(tmp_path, monkeypatch):
    import advisor_deploy.benchmark_deploy as deployment
    from advisor_deploy.operations import confirmation
    from advisor_deploy.render import digest
    repo = tmp_path
    template = repo / "deploy" / "bench" / "infra" / "main.bicep"
    template.parent.mkdir(parents=True)
    template.write_text("targetScope = 'resourceGroup'\n", encoding="utf-8")
    (repo / "deploy" / "out").mkdir()
    ledger = ownership()
    principal = str(uuid4())
    state = {
        "identity_principal_id": principal, "identity_client_id": str(uuid4()), "image_reference": "",
        "phase": "bootstrapped",
    }
    graph = {"client_id": str(uuid4()), "service_principal_id": str(uuid4()), "role_id": str(uuid4()),
             "grants": [{"id": "grant-" + str(index), "principal_id": value} for index, value in enumerate([principal, str(uuid4()), str(uuid4())])]}
    files = {name: character * 64 for name, character in [
        ("Dockerfile", "a"), (".dockerignore", "b"), ("requirements.txt", "c"), ("run.json", "d"),
        ("runtime-config/benchmark-cases.json", "e"), ("wheels/advisor_shared.whl", "f"), ("wheels/advisor_deploy.whl", "0")]}
    context = {"image_id": "sha256:" + "1" * 64, "base_image_digest": "sha256:" + "2" * 64,
               "linux_imports_verified": True, "files": files}
    run = SimpleNamespace(model_dump=lambda: {"approved": "bounded-plan"})
    paths = {"state_file": repo / "deploy" / "out" / "state.json", "context": repo / "deploy" / "out" / "context"}
    selected = {"registry": {"id": REGISTRY, "name": "owned", "properties": {"loginServer": "owned.azurecr.io"}},
                "environment": {"id": ENVIRONMENT}, "storage": {"id": STORAGE}}
    monkeypatch.setattr(deployment, "_prepare", lambda *args: (
        {"environment_name": "fixture"}, paths, ledger, {}, {"graph": graph}, state, selected))
    monkeypatch.setattr(deployment, "_verify_context", lambda *args: (context, run))
    private = SimpleNamespace(directory=repo / "deploy" / "out" / ".private-fixture")
    private.directory.mkdir()

    class Commands:
        mode = "Apply"
        tamper = True
        calls = []

        def rest(self, method, url, phase, **kwargs):
            if phase == "arm_permissions":
                return {"value": [{"actions": ["*"], "notActions": []}]}
            return {"value": [{"id": item["id"], "principalId": item["principal_id"],
                               "resourceId": graph["service_principal_id"], "appRoleId": graph["role_id"]}
                              for item in graph["grants"]]}

        def run(self, arguments, phase, **kwargs):
            self.calls.append((phase, arguments))
            if phase == "benchmark_docker_readiness":
                return "linux"
            if phase == "benchmark_exact_image":
                return json.dumps([{"Id": context["image_id"], "Os": "linux", "Architecture": "amd64",
                                    "Config": {"User": "10001", "Cmd": ["python", "-m", "advisor_deploy.bench", "run",
                                        "--plan", "/app/run.json", "--output", "/app/results", "--allow-cloud"]}}])
            if phase == "benchmark_image_payload":
                payload = {name: value for name, value in files.items() if name not in {"Dockerfile", ".dockerignore"}}
                if self.tamper:
                    payload["run.json"] = "9" * 64
                return json.dumps(payload)
            if phase == "benchmark_registry_token":
                return {"loginServer": "owned.azurecr.io", "accessToken": "fixture-token"}
            if phase == "benchmark_image_push":
                return "digest: sha256:" + "3" * 64 + " size: 1000"
            if phase == "benchmark_remote_digest":
                assert arguments[-1] == "advisor-bench@sha256:" + "3" * 64
                return {"digest": "sha256:" + "3" * 64}
            return ""

    from hashlib import sha256
    intent = {
        "template": sha256(template.read_bytes()).hexdigest(), "environment": "fixture", "registry": REGISTRY,
        "environment_id": ENVIRONMENT, "storage": STORAGE, "api_client_id": graph["client_id"],
        "context": digest(context), "image_id": context["image_id"], "image_reference": None,
    }
    token = confirmation("BENCH-PUBLISH", "aca", ledger["run_id"], intent)
    cli = Commands()
    with pytest.raises(ValueError, match="benchmark_image_payload_mismatch"):
        deployment.deploy_benchmark(repo / "plan.json", repo, cli, private, "Publish", token)
    assert not any(phase == "benchmark_image_push" for phase, _ in cli.calls)
    cli.tamper = False
    result = deployment.deploy_benchmark(repo / "plan.json", repo, cli, private, "Publish", token)
    assert result["image_reference"] == "owned.azurecr.io/advisor-bench@sha256:" + "3" * 64
    tag = next(arguments for phase, arguments in cli.calls if phase == "benchmark_image_tag")
    assert tag[1] == context["image_id"]
    assert state["run_plan_digest"] == digest(run.model_dump())

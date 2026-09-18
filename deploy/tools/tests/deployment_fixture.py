"""Synthetic Azure responses; executed by an isolated fake CLI, never by Azure."""

import base64
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID, uuid5


SUB = "adca540e-984c-4cab-bc86-5d38bf979841"
TENANT = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"
ROLE_IDS = {
    "blob": "ba92f5b4-2d11-453d-a403-e96b0029c9fe", "owner": "b7e6dc6d-f1e8-4753-8033-0f276bb0955b",
    "queue": "974c5e8b-45b9-4653-ba55-5f855dd0fb88", "pull": "7f951dda-4ed3-4680-a7ca-43fe172d538d",
    "metrics": "3913510d-42f4-4e42-8a64-420c390055eb", "openai": "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
    "search": "1407120a-92aa-4202-b7e9-c0e197c71c8f",
}


def main():
    args = sys.argv[1:]
    state_path = Path(os.environ["ADVISOR_STUB_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    tool = os.environ.get("ADVISOR_STUB_TOOL", "az")
    raw_args = list(args)
    docker_endpoint = None
    if tool == "docker" and args[:1] == ["--host"]:
        docker_endpoint, args = args[1], args[2:]
    event = {"tool": tool, "arguments": args, "raw_arguments": raw_args}
    if tool == "docker":
        event["docker_endpoint"] = docker_endpoint
        event["docker_config"] = os.environ.get("DOCKER_CONFIG")
    for argument in args:
        if argument.startswith("@"):
            path = Path(argument[1:])
            body = json.loads(path.read_text(encoding="utf-8"))
            event["private_file"] = str(path)
            event["secret_seen"] = "fixture-secret" in json.dumps(body)
            if "parameters" in body:
                state["parameters"] = {name: value["value"] for name, value in body["parameters"].items()}
                event["deploy_app"] = state["parameters"].get("deployApp")
    with Path(os.environ["ADVISOR_STUB_LOG"]).open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")

    def option(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    def output(value=None):
        state_path.write_text(json.dumps(state), encoding="utf-8")
        if value is not None:
            print(json.dumps(value))
        raise SystemExit(0)

    def text(value):
        state_path.write_text(json.dumps(state), encoding="utf-8")
        print(value)
        raise SystemExit(0)

    def denied(code="AuthorizationFailed"):
        print(f"ERROR: ({code}) fixture-secret-never-echo", file=sys.stderr)
        raise SystemExit(7)

    target = state.get("target", "aca")
    group = "rg-copilot-advisor-" + ("flex" if target == "functions" else "aca") + "-exp"
    group_id = f"/subscriptions/{SUB}/resourceGroups/{group}"
    identity_id = group_id + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/fixture"
    storage_id = group_id + "/providers/Microsoft.Storage/storageAccounts/stfixture"
    app_id = group_id + ("/providers/Microsoft.Web/sites/fixture" if target == "functions" else "/providers/Microsoft.App/containerApps/fixture")

    if tool == "docker":
        if args[:2] == ["context", "show"]:
            text("desktop-linux")
        if args[:2] == ["context", "inspect"]:
            text("npipe:////./pipe/dockerDesktopLinuxEngine")
        if args and args[0] == "info":
            if state.get("docker_down"):
                denied("DockerUnavailable")
            text("linux")
        if args[:2] == ["image", "inspect"]:
            text("python@sha256:" + "c" * 64)
        if args and args[0] == "login":
            assert sys.stdin.read().strip() == "fixture-acr-token"
            directory = Path(os.environ["DOCKER_CONFIG"])
            (directory / "config.json").write_text('{"auths":{"fixture":{"auth":"fixture-secret"}}}', encoding="utf-8")
        text("synthetic-docker-ok")

    if args[:2] == ["account", "show"]:
        output({"id": state.get("subscription", SUB), "tenantId": TENANT, "state": "Enabled", "environmentName": "AzureCloud"})
    if args[:2] == ["account", "get-access-token"]:
        claims = {"tid": TENANT, "aud": "https://graph.microsoft.com", "exp": int(time.time()) + 3600,
                  "roles": [] if state.get("graph_denied") else ["Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All"]}
        token = "fixture." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fixture"
        output({"accessToken": token})
    if args[:2] == ["provider", "show"]:
        output({"registrationState": "NotRegistered" if state.get("provider_missing") else "Registered"})
    if args[:2] == ["group", "exists"]:
        output(bool(state.get("group_exists")))
    if args[:2] == ["group", "show"]:
        selected = option("--name", group)
        output({"id": f"/subscriptions/{SUB}/resourceGroups/{selected}", "name": selected,
                "tags": {"advisorDeploymentId": state.get("owner", "unrelated")}})
    if args[:2] == ["group", "create"]:
        state.update(group_exists=True, owner=option("--tags").split("=", 1)[1])
        output({"id": group_id, "tags": {"advisorDeploymentId": state["owner"]}})
    if args[:2] == ["group", "update"]:
        state["owner"] = option("--set").split("=", 1)[1]
        output({"id": group_id, "tags": {"advisorDeploymentId": state["owner"]}})
    if args[:2] == ["group", "delete"]:
        state["group_exists"] = False
        state["resources"] = []
        output()
    if args[:2] == ["resource", "list"]:
        prefix = f"/subscriptions/{SUB}/resourceGroups/{option('--resource-group', group)}/"
        output([item for item in state.get("resources", []) if item["id"].startswith(prefix)])
    if args[:2] == ["identity", "show"]:
        output(state["identities"][option("--ids")])
    if args[:2] == ["resource", "show"]:
        identifier = option("--ids")
        if identifier in state.get("resource_details", {}):
            output(state["resource_details"][identifier])
        if identifier == app_id:
            output(state["app"])
        if "/Microsoft.CognitiveServices/" in identifier:
            output({"id": identifier, "location": "southeastasia", "properties": {"endpoint": "https://model.openai.azure.com"}})
        if "/Microsoft.Search/" in identifier:
            output({"id": identifier, "location": "southeastasia", "properties": {"authOptions": {"aadOrApiKey": {"aadAuthFailureMode": "http401"}}}})
    if args[:4] == ["cognitiveservices", "account", "deployment", "show"]:
        output({"properties": {"model": {"name": option("--deployment-name")}}})
    if args[:2] == ["functionapp", "list-flexconsumption-runtimes"]:
        output([{
            "name": "python", "version": "3.12",
            "sku": {
                "skuCode": "FC1",
                "functionAppConfigProperties": {"runtime": {"name": "python", "version": "3.12"}},
                "instanceMemoryMB": [
                    {"size": "512", "isDefault": False}, {"size": "2048", "isDefault": True},
                    {"size": "4096", "isDefault": False},
                ],
                "maximumInstanceCount": {
                    "defaultValue": 100, "lowestMaximumInstanceCount": 1,
                    "highestMaximumInstanceCount": 1000,
                },
            },
        }])
    if args[:3] == ["deployment", "sub", "validate"] or args[:3] == ["deployment", "sub", "what-if"]:
        output({"status": "Succeeded", "changes": []})
    if args[:3] == ["deployment", "group", "validate"]:
        output({"status": "Succeeded"})
    if args[:3] == ["deployment", "group", "what-if"]:
        output({"status": "Succeeded", "changes": state.get("bench_changes", [])})
    if args[:3] == ["deployment", "group", "show"]:
        output({"properties": {"outputResources": [{"id": value} for value in state.get("bench_receipts", [])]}})
    if args[:3] == ["deployment", "group", "create"]:
        parameters = state["parameters"]
        bench_identity = group_id + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-bench-fixture"
        container = storage_id + "/blobServices/default/containers/benchmark-results"
        bench_principal = "77777777-7777-7777-7777-777777777777"
        bench_client = "88888888-8888-8888-8888-888888888888"
        state.setdefault("identities", {})[bench_identity] = {
            "id": bench_identity, "principalId": bench_principal, "clientId": bench_client,
            "tags": {"experiment": "benchmark"},
        }
        added = [
            {"id": bench_identity, "name": "id-bench-fixture", "type": "Microsoft.ManagedIdentity/userAssignedIdentities"},
            {"id": container, "name": "benchmark-results", "type": "Microsoft.Storage/storageAccounts/blobServices/containers"},
        ]
        role_ids = []
        for scope, role in (
            (group_id + "/providers/Microsoft.ContainerRegistry/registries/fixture", ROLE_IDS["pull"]),
            (container, ROLE_IDS["blob"]),
        ):
            identifier = scope + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid5(UUID(bench_principal), scope + role))
            state.setdefault("roles", {})[identifier] = {
                "properties": {"principalId": bench_principal,
                    "roleDefinitionId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{role}"}}
            role_ids.append(identifier)
        for resource in added:
            if resource["id"] not in {item["id"] for item in state["resources"]}:
                state["resources"].append(resource)
        state["bench_receipts"] = [item["id"] for item in added] + role_ids
        values = {
            "planned_app_id": group_id + "/providers/Microsoft.App/containerApps/ca-bench-fixture",
            "planned_app_name": "ca-bench-fixture",
            "planned_endpoint": "https://ca-bench-fixture.fixture.southeastasia.azurecontainerapps.io",
            "receiver_endpoint": "", "driver_endpoint": "", "results_storage_id": storage_id,
            "results_container_id": container, "identity_id": bench_identity,
            "identity_client_id": bench_client, "identity_principal_id": bench_principal,
            "created_role_assignment_ids": role_ids,
        }
        assert parameters["enabled"] is True and parameters["deployApp"] is False
        output({"properties": {"outputs": {key: {"value": value} for key, value in values.items()}}})
    if args[:3] == ["deployment", "sub", "show"]:
        if state.get("receipts_missing"):
            output({"properties": {}})
        output({"properties": {"outputResources": [{"id": item["id"]} for item in state.get("resources", [])] +
                             [{"id": item} for item in state.get("roles", {})]}})
    if args[:3] == ["deployment", "sub", "create"]:
        parameters = state["parameters"]
        has_app = target == "functions" or parameters["deployApp"]
        types = ["Microsoft.ManagedIdentity/userAssignedIdentities", "Microsoft.Storage/storageAccounts",
                 "Microsoft.OperationalInsights/workspaces"]
        types += ["Microsoft.Web/serverfarms", "Microsoft.Insights/components"] if target == "functions" else [
            "Microsoft.App/managedEnvironments", "Microsoft.ContainerRegistry/registries"]
        if has_app:
            types += ["Microsoft.Web/sites" if target == "functions" else "Microsoft.App/containerApps", "Microsoft.BotService/botServices"]
        resources = []
        for kind in types:
            name = "stfixture" if kind.endswith("storageAccounts") else "fixture"
            resources.append({"id": group_id + "/providers/" + kind + "/" + name, "name": name, "type": kind})
        state["resources"] = resources
        scopes = [
            (storage_id, ROLE_IDS["owner" if target == "functions" else "blob"]),
            (storage_id, ROLE_IDS["queue"]),
            (group_id + "/providers/" + ("Microsoft.Insights/components" if target == "functions" else "Microsoft.ContainerRegistry/registries") + "/fixture",
             ROLE_IDS["metrics" if target == "functions" else "pull"]),
        ]
        for kind in ("openai", "search"):
            if parameters[kind + "AuthMode"] == "entra":
                scopes.append((parameters[kind + "ResourceId"], ROLE_IDS[kind]))
        state["roles"] = {}
        for scope, role in scopes:
            identifier = scope + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid5(UUID(PRINCIPAL), scope + role))
            state["roles"][identifier] = {"properties": {"principalId": PRINCIPAL,
                "roleDefinitionId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{role}"}}
        settings = {
            "AZURE_CLIENT_ID": CLIENT, "ADVISOR_ENTRA_CREDENTIAL": "managed_identity", "ADVISOR_STORAGE_AUTH_MODE": "entra",
            "AZURE_OPENAI_CHAT_DEPLOYMENT": parameters["modelDeploymentName"],
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": parameters["embeddingDeploymentName"],
            "ADVISOR_WORKER_PARALLELISM": str(parameters["workerParallelism"]),
            "AZURE_OPENAI_AUTH_MODE": parameters["openaiAuthMode"], "AZURE_SEARCH_AUTH_MODE": parameters["searchAuthMode"],
            "AdvisorQueue__credential": "managedidentity", "AdvisorQueue__clientId": CLIENT,
            "AdvisorQueue__queueServiceUri": "https://stfixture.queue.core.windows.net/",
            "AzureWebJobsStorage__credential": "managedidentity", "AzureWebJobsStorage__clientId": CLIENT,
            "AzureWebJobsStorage__blobServiceUri": "https://stfixture.blob.core.windows.net/",
            "AzureWebJobsStorage__queueServiceUri": "https://stfixture.queue.core.windows.net/",
            "FUNCTIONS_WORKER_PROCESS_COUNT": "1",
            "AzureFunctionsJobHost__extensions__queues__batchSize": str(parameters["workerParallelism"]),
            "AzureFunctionsJobHost__extensions__queues__newBatchThreshold": "0",
        } | parameters["runtimeSettings"]
        state["settings"] = [{"name": name, "value": value} for name, value in settings.items()]
        props = {"functionAppConfig": {"runtime": {"name": "python", "version": "3.12"},
                  "scaleAndConcurrency": {"instanceMemoryMB": parameters.get("instanceMemoryMB"), "maximumInstanceCount": parameters["maxInstances"], "alwaysReady": [],
                                          "triggers": {"http": {"perInstanceConcurrency": parameters["workerParallelism"]}}}}}
        if target == "aca":
            cpu, memory = {"quarter": (.25, "0.5Gi"), "half": (.5, "1Gi"), "one": (1, "2Gi"), "two": (2, "4Gi")}[parameters["resourceProfile"]]
            props = {"template": {"scale": {"minReplicas": parameters["minReplicas"], "maxReplicas": parameters["maxInstances"]},
                                  "containers": [{"image": "fixture.azurecr.io/" + parameters["image"], "resources": {"cpu": cpu, "memory": memory}, "env": state["settings"]}]}}
        state["app"] = {"id": app_id, "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity_id: {}}}, "properties": props}
        outputs = {
            "target": target, "resource_group": group, "app_id": app_id if has_app else "", "app_name": "fixture" if has_app else "",
            "endpoint": "https://fixture.example.net" if has_app else "", "storage_id": storage_id,
            "blob_url": "https://stfixture.blob.core.windows.net/", "queue_url": "https://stfixture.queue.core.windows.net/",
            "identity_id": identity_id, "identity_client_id": CLIENT, "identity_principal_id": PRINCIPAL,
            "bot_id": group_id + "/providers/Microsoft.BotService/botServices/fixture" if has_app else "",
            "created_role_assignment_ids": list(state["roles"]),
        }
        state["owner"] = ""
        state["outputs"] = outputs
        if state.get("apply_fail"):
            state_path.write_text(json.dumps(state), encoding="utf-8")
            denied()
        output({"properties": {"outputs": {name: {"value": value} for name, value in outputs.items()}}})
    if args[:5] == ["functionapp", "deployment", "source", "config-zip", "--resource-group"]:
        output({"status": "Succeeded"})
    if args[:4] == ["functionapp", "config", "appsettings", "list"]:
        output(state["settings"])
    if args[:2] == ["containerapp", "show"]:
        output(state["app"])
    if args[:2] == ["containerapp", "update"]:
        state["app"]["properties"]["template"]["scale"]["minReplicas"] = int(option("--min-replicas"))
        output(state["app"])
    if args[:3] == ["role", "assignment", "delete"]:
        state["roles"].pop(option("--ids"), None)
        output()
    if args[:2] == ["acr", "login"]:
        output({"loginServer": "fixture.azurecr.io", "accessToken": "fixture-acr-token"})
    if args[:3] == ["acr", "repository", "show"]:
        output({"digest": "sha256:" + "d" * 64})
    if args and args[0] == "rest":
        url, method = option("--url"), option("--method")
        if "/permissions?" in url:
            output({"value": [] if state.get("permissions_denied") else [{"actions": ["*"], "notActions": []}]})
        if ".search.windows.net/indexes/" in url:
            output({"fields": [{"name": "content_vector", "dimensions": state.get("dimensions", 3072)}]})
        if "/usages?" in url:
            output({"value": [{"name": {"value": "ManagedEnvironmentConsumptionCores"}, "limit": 50, "currentValue": 0}]})
        identifier = url.removeprefix("https://management.azure.com").split("?")[0]
        if identifier in state.get("roles", {}):
            output(state["roles"][identifier])
        if "/providers/Microsoft.Authorization/roleAssignments/" in identifier:
            denied("ResourceNotFound")
        if "/applications?$filter=" in url:
            output({"value": state.get("applications", [])})
        if url.endswith("/applications") and method == "post":
            body = json.loads(Path(option("--body")[1:]).read_text(encoding="utf-8"))
            app = body | {"id": "44444444-4444-4444-4444-444444444444", "appId": "55555555-5555-5555-5555-555555555555"}
            state["applications"] = [app]
            output(app)
        if "/applications/" in url:
            app = state["applications"][0]
            if method == "patch":
                app.update(json.loads(Path(option("--body")[1:]).read_text(encoding="utf-8")))
                output()
            if method == "delete":
                state["applications"] = []
                output()
            output(app)
        if "/servicePrincipals?$filter=" in url:
            output({"value": state.get("service_principals", [])})
        if url.endswith("/servicePrincipals") and method == "post":
            body = json.loads(Path(option("--body")[1:]).read_text(encoding="utf-8"))
            principal = body | {"id": "66666666-6666-6666-6666-666666666666"}
            state["service_principals"] = [principal]
            output(principal)
        if "/appRoleAssignedTo" in url:
            if method == "get":
                output({"value": state.get("grants", [])})
            if method == "post":
                body = json.loads(Path(option("--body")[1:]).read_text(encoding="utf-8"))
                grant = body | {"id": "grant-" + body["principalId"]}
                state.setdefault("grants", []).append(grant)
                output(grant)
            if method == "delete":
                state["grants"] = [item for item in state.get("grants", []) if item["id"] != url.split("/")[-1]]
                output()
        if "/servicePrincipals/" in url:
            if method == "delete":
                state["service_principals"] = []
                output()
            output(state["service_principals"][0])
    print("Unsupported synthetic command", file=sys.stderr)
    raise SystemExit(90)


if __name__ == "__main__":
    main()

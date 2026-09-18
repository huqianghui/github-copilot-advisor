"""Offline contracts against actual Bicep compiler output, never source text.

Run with the workspace interpreter: python -m pytest deploy/tests -q.
Compilation uses stdout, so no generated templates or cloud resources are written.
"""

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
OUTPUTS = {
    "target", "resource_group", "app_id", "app_name", "endpoint",
    "storage_id", "blob_url", "queue_url", "identity_id",
    "identity_client_id", "identity_principal_id", "bot_id",
    "created_role_assignment_ids",
}


@lru_cache
def compile_template(relative_path):
    source = ROOT / relative_path
    assert source.is_file(), f"Missing infrastructure template: {relative_path}"
    az = shutil.which("az")
    assert az, "Azure CLI with Bicep must be installed before running offline tests"
    completed = subprocess.run(
        [az, "bicep", "build", "--file", str(source), "--stdout"],
        capture_output=True, text=True, encoding="utf-8", check=False,
        timeout=240,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture
def compiled_infra():
    return lambda target: compile_template(f"deploy/{target}/infra/main.bicep")


def resources(template):
    collection = template.get("resources", [])
    for resource in collection.values() if isinstance(collection, dict) else collection:
        yield resource
        if resource["type"].lower() == "microsoft.resources/deployments":
            yield from resources(resource["properties"].get("template", {}))


def of_type(template, resource_type):
    return [
        resource for resource in resources(template)
        if resource["type"].lower() == resource_type.lower() and not resource.get("existing")
    ]


def only(template, resource_type):
    matches = of_type(template, resource_type)
    assert len(matches) == 1, (resource_type, len(matches))
    return matches[0]


def default(template, name):
    return template["parameters"][name]["defaultValue"]


def module(template, name):
    return next(
        item for item in resources(template)
        if item["type"] == "Microsoft.Resources/deployments" and item["name"] == name
    )


def body(template, name):
    return module(template, name)["properties"]["template"]


def argument(template, module_name, parameter):
    return module(template, module_name)["properties"]["parameters"][parameter]["value"]


def constant(value):
    if isinstance(value, str) and value.startswith("[json('") and value.endswith("')]"):
        return json.loads(value[7:-3])
    return value


def arm_arguments(expression):
    """Split a compiled ARM function's arguments without splitting nested calls."""
    expression = expression.removeprefix("[").removesuffix("]")
    content = expression[expression.index("(") + 1:-1]
    depth, quoted, start = 0, False, 0
    arguments = []
    index = 0
    while index < len(content):
        character = content[index]
        if character == "'":
            if quoted and index + 1 < len(content) and content[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if character in "([":
                depth += 1
            elif character in ")]":
                depth -= 1
            elif character == "," and depth == 0:
                arguments.append(content[start:index].strip())
                start = index + 1
        index += 1
    arguments.append(content[start:].strip())
    assert depth == 0 and not quoted
    return arguments


def arm_object(expression):
    assert expression.startswith("createObject(")
    parts = arm_arguments(expression)
    assert len(parts) % 2 == 0
    return {
        key[1:-1]: value[1:-1] if value.startswith("'") else f"[{value}]"
        for key, value in zip(parts[::2], parts[1::2], strict=True)
    }


def test_flex_defaults_are_not_legacy_consumption(compiled_infra):
    flex = compiled_infra("functions")
    assert default(flex, "instanceMemoryMB") == 512
    assert default(flex, "runtimeVersion") == "3.12"
    assert default(flex, "maxInstances") == 40
    assert default(flex, "workerParallelism") == 4
    assert only(flex, "Microsoft.Web/serverfarms")["sku"] == {
        "name": "FC1", "tier": "FlexConsumption",
    }


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_outputs_are_the_exact_nonsecret_manifest_contract(compiled_infra, target):
    template = compiled_infra(target)
    assert set(template["outputs"]) == OUTPUTS
    assert all(value["type"] in {"string", "array"} for value in template["outputs"].values())
    assert template["parameters"]["secretSettings"]["type"] == "secureObject"
    assert not any("listKeys(" in json.dumps(value) for value in template["outputs"].values())


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_state_is_private_keyless_hot_lrs_with_no_table(compiled_infra, target):
    template = compiled_infra(target)
    storage = only(template, "Microsoft.Storage/storageAccounts")
    assert storage["sku"]["name"] == "Standard_LRS"
    assert storage["kind"] == "StorageV2"
    props = storage["properties"]
    assert props["accessTier"] == "Hot"
    assert props["allowSharedKeyAccess"] is False
    assert props["allowBlobPublicAccess"] is False
    assert props["minimumTlsVersion"] == "TLS1_2"
    assert props["supportsHttpsTrafficOnly"] is True
    containers = of_type(template, "Microsoft.Storage/storageAccounts/blobServices/containers")
    assert containers and all(item["properties"]["publicAccess"] == "None" for item in containers)
    assert of_type(template, "Microsoft.Storage/storageAccounts/queueServices/queues")
    assert not of_type(template, "Microsoft.Storage/storageAccounts/tableServices")


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_bot_is_f0_uami_with_teams_channel(compiled_infra, target):
    template = compiled_infra(target)
    bot = only(template, "Microsoft.BotService/botServices")
    assert bot["sku"]["name"] == "F0"
    assert bot["properties"]["msaAppType"] == "UserAssignedMSI"
    assert bot["properties"]["msaAppMSIResourceId"]
    assert only(template, "Microsoft.BotService/botServices/channels")["properties"] == {
        "channelName": "MsTeamsChannel", "properties": {"isEnabled": True},
    }
    assert only(template, "Microsoft.ManagedIdentity/userAssignedIdentities")


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_experiment_is_region_isolated_and_benchmark_off(compiled_infra, target):
    template = compiled_infra(target)
    assert default(template, "location") == "southeastasia"
    assert default(template, "benchmarkEnabled") is False
    assert default(template, "benchmarkAudience") == ""
    assert default(template, "benchmarkAllowedPrincipalIds") == []
    types = {r["type"].lower() for r in resources(template)}
    assert not any("virtualnetworks" in kind or "redis" in kind or "servicebus" in kind for kind in types)


def test_aca_has_new_basic_registry_and_bootstrap_flag(compiled_infra):
    aca = compiled_infra("aca")
    registry = only(aca, "Microsoft.ContainerRegistry/registries")
    assert registry["sku"]["name"] == "Basic"
    assert registry["properties"]["adminUserEnabled"] is False
    assert default(aca, "deployApp") is False
    assert default(aca, "minReplicas") == 0
    assert aca["parameters"]["minReplicas"]["allowedValues"] == [0, 1]
    app = only(aca, "Microsoft.App/containerApps")
    assert app["properties"]["configuration"]["activeRevisionsMode"] == "Single"
    container = app["properties"]["template"]["containers"][0]
    assert default(aca, "resourceProfile") == "quarter"
    assert container["resources"] == "[variables('resourceProfiles')[parameters('resourceProfile')]]"
    assert {probe["type"] for probe in container["probes"]} == {"Startup", "Readiness", "Liveness"}


def test_benchmark_is_one_temporary_opt_in_clock_observer(compiled_infra):
    bench = compiled_infra("bench")
    assert default(bench, "enabled") is False
    assert default(bench, "deployApp") is False
    apps = of_type(bench, "Microsoft.App/containerApps")
    assert len(apps) == 1
    assert all(app["condition"] == "[and(parameters('enabled'), parameters('deployApp'))]" for app in apps)
    identity = only(bench, "Microsoft.ManagedIdentity/userAssignedIdentities")
    assert "copy" not in identity
    assert not of_type(bench, "Microsoft.Storage/storageAccounts")
    results = only(bench, "Microsoft.Storage/storageAccounts/blobServices/containers")
    assert "parameters('storageAccountName')" in results["name"]
    assert "benchmark-results" in results["name"]
    for app in apps:
        assert app["properties"]["configuration"]["ingress"]["external"] is True
        assert app["properties"]["template"]["scale"] == {"minReplicas": 1, "maxReplicas": 1}
        containers = app["properties"]["template"]["containers"]
        assert len(containers) == 1
        env = {item["name"]: item["value"] for item in containers[0]["env"]}
        assert env["WEB_CONCURRENCY"] == "1"
        assert env["BENCHMARK_AUTOSTART"] == "false"
    assert bench["outputs"]["receiver_endpoint"]["value"] == bench["outputs"]["driver_endpoint"]["value"]
    assert not any(resource["type"].startswith("Microsoft.Graph") for resource in resources(bench))


@pytest.mark.parametrize(
    ("target", "expected_group"),
    [("aca", "rg-copilot-advisor-aca-exp"), ("functions", "rg-copilot-advisor-flex-exp")],
)
def test_root_modules_use_only_their_own_experiment_group(compiled_infra, target, expected_group):
    template = compiled_infra(target)
    assert template["variables"]["groupName"] == expected_group
    for name in ("identity", "state", "monitoring", "settings", "app", "bot"):
        assert module(template, name)["resourceGroup"] == "[variables('groupName')]"
    assert "groupName" in template["variables"]["suffix"]
    group = only(template, "Microsoft.Resources/resourceGroups")
    assert "target" in group["name"]


def test_flex_keeps_avm_managed_deployment_storage_and_no_always_ready(compiled_infra):
    template = compiled_infra("functions")
    api = body(template, "app")
    config = argument(api, "flex-site", "functionAppConfig")
    assert config["runtime"] == {"name": "python", "version": "[parameters('runtimeVersion')]"}
    assert config["scaleAndConcurrency"] == {
        "instanceMemoryMB": "[parameters('instanceMemoryMB')]",
        "maximumInstanceCount": "[parameters('maxInstances')]",
        "alwaysReady": [],
        "triggers": {"http": {"perInstanceConcurrency": "[parameters('workerParallelism')]"}},
    }
    storage = config["deployment"]["storage"]
    assert storage["type"] == "blobContainer"
    assert "deployment" in storage["value"]
    assert storage["authentication"] == {
        "type": "UserAssignedIdentity",
        "userAssignedIdentityResourceId": "[parameters('identityId')]",
    }
    assert argument(api, "flex-site", "virtualNetworkSubnetId") is None
    assert argument(api, "flex-site", "siteConfig")["alwaysOn"] is False
    assert argument(api, "flex-site", "httpsOnly") is True
    assert argument(template, "state", "includeDeploymentContainer") is True
    assert argument(api, "flex-site", "enableTelemetry") is False
    assert only(body(api, "flex-site"), "Microsoft.Web/sites")


def test_flex_binding_settings_use_explicit_uami_for_both_connections(compiled_infra):
    template = compiled_infra("functions")
    expression = argument(body(template, "app"), "flex-site", "appSettingsKeyValuePairs")
    settings = arm_object(arm_arguments(expression)[-1])
    for connection in ("AzureWebJobsStorage", "AdvisorQueue"):
        assert settings[f"{connection}__credential"] == "managedidentity"
        assert settings[f"{connection}__clientId"] == "[parameters('identityClientId')]"
        assert "primaryEndpoints.queue" in settings[f"{connection}__queueServiceUri"]
    assert "primaryEndpoints.blob" in settings["AzureWebJobsStorage__blobServiceUri"]
    assert not any("table" in name.lower() for name in settings)
    assert settings["FUNCTIONS_WORKER_PROCESS_COUNT"] == "1"
    assert settings["ADVISOR_MAINTENANCE_SCHEDULE"] == "0 */5 * * * *"
    assert settings["AzureFunctionsJobHost__extensions__queues__batchSize"] == "[string(parameters('workerParallelism'))]"
    assert settings["AzureFunctionsJobHost__extensions__queues__newBatchThreshold"] == "0"
    assert settings["AzureFunctionsJobHost__concurrency__dynamicConcurrencyEnabled"] == "false"
    assert settings["AzureFunctionsJobHost__extensions__queues__messageEncoding"] == "none"


def test_aca_queue_scaler_uses_identity_and_both_scaling_signals(compiled_infra):
    app = only(compiled_infra("aca"), "Microsoft.App/containerApps")
    scale = app["properties"]["template"]["scale"]
    assert scale["minReplicas"] == "[parameters('minReplicas')]"
    assert scale["maxReplicas"] == "[parameters('maxInstances')]"
    http, queue = scale["rules"]
    assert http["http"]["metadata"]["concurrentRequests"] == "[string(parameters('workerParallelism'))]"
    assert queue["custom"]["identity"] == "[parameters('identityId')]"
    assert queue["custom"]["type"] == "azure-queue"
    assert queue["custom"]["metadata"]["queueLength"] == "[string(parameters('workerParallelism'))]"
    assert queue["custom"]["metadata"]["queueName"] == "advisor-incoming"
    assert app["properties"]["configuration"]["registries"][0]["identity"] == "[parameters('identityId')]"
    assert module(compiled_infra("aca"), "app")["condition"] == "[parameters('deployApp')]"
    assert module(compiled_infra("aca"), "bot")["condition"] == "[parameters('deployApp')]"


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_only_immutable_objects_have_age_deletion(compiled_infra, target):
    policy = only(compiled_infra(target), "Microsoft.Storage/storageAccounts/managementPolicies")
    rules = policy["properties"]["policy"]["rules"]
    actual = {
        prefix: rule["definition"]["actions"]["baseBlob"]["delete"]
        for rule in rules for prefix in rule["definition"]["filters"]["prefixMatch"]
    }
    assert actual == {
        "advisor-history/history/": {"daysAfterCreationGreaterThan": 30},
        "advisor-jobs/payload/": {"daysAfterCreationGreaterThan": 1},
        "advisor-jobs/results/": {"daysAfterCreationGreaterThan": 1},
        "advisor-jobs/terminal-metadata/": {"daysAfterCreationGreaterThan": 7},
    }
    assert not any("context/" in prefix or "leases/" in prefix for prefix in actual)


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_external_roles_are_resource_scoped_readers_not_owners(compiled_infra, target):
    template = compiled_infra(target)
    role_ids = {
        "openai": "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
        "search": "1407120a-92aa-4202-b7e9-c0e197c71c8f",
    }
    for dependency, role_id in role_ids.items():
        deployment = next(
            item for item in resources(template)
            if item["type"] == "Microsoft.Resources/deployments"
            and item.get("properties", {}).get("parameters", {}).get("dependency", {}).get("value") == dependency
        )
        assert dependency + "ResourceId" in deployment["subscriptionId"]
        assert dependency + "ResourceId" in deployment["resourceGroup"]
        nested = deployment["properties"]["template"]
        assert role_id in nested["variables"].values()
        for role in of_type(nested, "Microsoft.Authorization/roleAssignments"):
            assert role.get("scope"), "External roles must never use the resource group as implicit scope"
            assert role["properties"]["principalType"] == "ServicePrincipal"
            assert "subscriptionResourceId(" in role["properties"]["roleDefinitionId"]
        assert deployment["condition"] == f"[equals(parameters('{dependency}AuthMode'), 'entra')]"


@pytest.mark.parametrize(
    ("target", "role_module", "expected"),
    [
        ("aca", "state-roles", {"ba92f5b4-2d11-453d-a403-e96b0029c9fe", "974c5e8b-45b9-4653-ba55-5f855dd0fb88"}),
        ("functions", "host-roles", {"b7e6dc6d-f1e8-4753-8033-0f276bb0955b", "974c5e8b-45b9-4653-ba55-5f855dd0fb88", "3913510d-42f4-4e42-8a64-420c390055eb"}),
    ],
)
def test_state_and_host_roles_match_the_required_minimum(compiled_infra, target, role_module, expected):
    role_template = body(compiled_infra(target), role_module)
    values = role_template["variables"].values()
    actual = set()
    for value in values:
        actual.update(value if isinstance(value, list) else [value])
    assert actual == expected
    for role in of_type(role_template, "Microsoft.Authorization/roleAssignments"):
        assert role.get("scope")
        assert "subscriptionResourceId(" in role["properties"]["roleDefinitionId"]


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_auth_modes_and_nonsecret_configuration_reach_host(compiled_infra, target):
    template = compiled_infra(target)
    settings = body(template, "settings")["outputs"]["settings"]["value"]
    assert settings["ADVISOR_ENTRA_CREDENTIAL"] == "managed_identity"
    assert settings["ADVISOR_STORAGE_AUTH_MODE"] == "entra"
    assert settings["AZURE_CLIENT_ID"] == "[parameters('identityClientId')]"
    assert settings["CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE"] == "UserManagedIdentity"
    assert settings["CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED"] == "False"
    assert settings["BENCHMARK_AUTH_MODE"] == "entra"
    assert settings["BENCHMARK_AUDIENCE"] == "[parameters('benchmarkAudience')]"
    for parameter in ("runtimeConfigPath", "workerParallelism", "benchmarkAllowedPrincipalIds", "benchmarkAllowedClientIds"):
        assert argument(template, "settings", parameter) == f"[parameters('{parameter}')]"
    assert argument(template, "app", "secretSettings") == "[parameters('secretSettings')]"
    assert body(template, "app")["parameters"]["secretSettings"]["type"] == "secureObject"


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_secret_settings_are_allowlisted_and_cannot_override_auth(compiled_infra, target):
    app = body(compiled_infra(target), "app")
    assert set(app["variables"]["allowedSecretNames"]) == {
        "AZURE_OPENAI_API_KEY", "AZURE_SEARCH_API_KEY", "GITHUB_TOKEN",
        "TAVILY_API_KEY", "BRAVE_API_KEY",
    }
    selection = app["variables"]["acceptedSecrets"]
    assert "allowedSecretNames" in selection
    assert "AZURE_OPENAI_AUTH_MODE" in selection
    assert "AZURE_SEARCH_AUTH_MODE" in selection


def test_benchmark_ingress_requires_entra_not_anonymous_access(compiled_infra):
    template = compiled_infra("bench")
    auth_configs = of_type(template, "Microsoft.App/containerApps/authConfigs")
    assert len(auth_configs) == 1
    for auth in auth_configs:
        assert auth["condition"] == "[and(parameters('enabled'), parameters('deployApp'))]"
        props = auth["properties"]
        assert props["platform"]["enabled"] is True
        assert props["globalValidation"]["unauthenticatedClientAction"] == "Return401"
        assert props["httpSettings"]["requireHttps"] is True
        provider = props["identityProviders"]["azureActiveDirectory"]
        assert provider["enabled"] is True
        assert provider["registration"]["clientId"] == "[parameters('apiClientId')]"
        assert provider["validation"]["allowedAudiences"] == ["[parameters('audience')]"]
        assert provider["validation"]["defaultAuthorizationPolicy"]["allowedPrincipals"]["identities"] == "[union(parameters('operatorPrincipalIds'), parameters('workloadPrincipalIds'))]"
        assert provider["validation"]["defaultAuthorizationPolicy"]["allowedApplications"] == "[union(parameters('operatorClientIds'), parameters('workloadClientIds'))]"
    for app in of_type(template, "Microsoft.App/containerApps"):
        env = {item["name"]: item["value"] for item in app["properties"]["template"]["containers"][0]["env"]}
        assert env["BENCHMARK_AUTH_MODE"] == "entra"
        assert env["BENCHMARK_REQUIRED_ROLE"] == "Benchmark.Invoke"
        assert env["BENCHMARK_AUDIENCE"] == "[parameters('audience')]"
        assert env["BENCHMARK_TARGET_AUDIENCE"] == "[parameters('audience')]"
        for route in ("OPERATOR", "WORKLOAD"):
            for claim, parameter in (("PRINCIPAL", "PrincipalIds"), ("CLIENT", "ClientIds")):
                assert env[f"BENCHMARK_{route}_{claim}_IDS"] == f"[join(parameters('{route.lower()}{parameter}'), ',')]"


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_monitoring_is_small_and_has_explicit_retention(compiled_infra, target):
    template = compiled_infra(target)
    logs = only(template, "Microsoft.OperationalInsights/workspaces")
    assert logs["properties"]["retentionInDays"] == 30
    assert logs["properties"]["sku"]["name"] == "PerGB2018"
    monitoring = body(template, "monitoring")
    insights = only(monitoring, "Microsoft.Insights/components")
    assert insights["properties"]["DisableLocalAuth"] is True
    assert insights["properties"]["RetentionInDays"] == 30
    assert default(monitoring, "enableApplicationInsights") is False


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_host_uses_existing_chat_and_yaml_config_environment_names(compiled_infra, target):
    settings = body(compiled_infra(target), "settings")["outputs"]["settings"]["value"]
    assert settings["AZURE_OPENAI_CHAT_DEPLOYMENT"] == "[parameters('modelDeploymentName')]"
    assert settings["ESCALATION_CONFIG"] == "[format('{0}/escalation.yaml', parameters('runtimeConfigPath'))]"
    assert settings["DIAGNOSTICS_CONFIG"] == "[format('{0}/diagnostics.yaml', parameters('runtimeConfigPath'))]"
    assert settings["WEB_CONCURRENCY"] == "1"


def test_benchmark_cannot_accept_blank_registration_configuration(compiled_infra):
    template = compiled_infra("bench")
    for name in ("apiClientId",):
        assert template["parameters"][name]["minLength"] == 36
        assert template["parameters"][name]["maxLength"] == 36
    for name in ("audience",):
        assert template["parameters"][name]["minLength"] == 1
    assert not {"receiverApiClientId", "driverApiClientId", "receiverAudience", "driverAudience", "targetAudience"} & template["parameters"].keys()


def test_flex_accepts_current_maximum_instance_range(compiled_infra):
    template = compiled_infra("functions")
    assert default(template, "maxInstances") == 40
    for current in (template, body(template, "app")):
        assert current["parameters"]["maxInstances"]["minValue"] == 1
        assert current["parameters"]["maxInstances"]["maxValue"] == 1000


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_bot_jwt_issuer_validation_is_enabled(compiled_infra, target):
    settings = body(compiled_infra(target), "settings")["outputs"]["settings"]["value"]
    assert settings["CONNECTIONS__SERVICE_CONNECTION__SETTINGS__VALIDATE_ISSUER"] == "True"


def test_functions_retains_user_timing_logs_without_sdk_debug(compiled_infra):
    expression = argument(body(compiled_infra("functions"), "app"), "flex-site", "appSettingsKeyValuePairs")
    settings = arm_object(arm_arguments(expression)[-1])
    assert settings["AzureFunctionsJobHost__logging__logLevel__Function"] == "Information"
    assert settings["AzureFunctionsJobHost__logging__logLevel__Host.Results"] == "Information"
    assert settings["AzureFunctionsJobHost__logging__logLevel__default"] == "Warning"
    assert not any(value == "Debug" for value in settings.values())


@pytest.mark.parametrize(
    ("profile", "cpu", "memory"),
    [("quarter", 0.25, "0.5Gi"), ("half", 0.5, "1Gi"), ("one", 1, "2Gi"), ("two", 2, "4Gi")],
)
def test_aca_profile_selection_is_explicit_and_uses_valid_pairs(compiled_infra, profile, cpu, memory):
    template = compiled_infra("aca")
    app = body(template, "app")
    assert default(template, "resourceProfile") == "quarter"
    for current in (template, app):
        assert current["parameters"]["resourceProfile"]["allowedValues"] == ["quarter", "half", "one", "two"]
    assert argument(template, "app", "resourceProfile") == "[parameters('resourceProfile')]"
    pair = app["variables"]["resourceProfiles"][profile]
    assert constant(pair["cpu"]) == cpu
    assert pair["memory"] == memory
    container = only(app, "Microsoft.App/containerApps")["properties"]["template"]["containers"][0]
    assert container["resources"] == "[variables('resourceProfiles')[parameters('resourceProfile')]]"


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_approved_runtime_budgets_and_logging_reach_both_hosts(compiled_infra, target):
    template = compiled_infra(target)
    assert default(template, "runtimeSettings") == {}
    assert argument(template, "settings", "runtimeSettings") == "[parameters('runtimeSettings')]"
    settings = body(template, "settings")["outputs"]["settings"]["value"]
    for name in ("KB_SEARCH_BUDGET_SECONDS", "GITHUB_SEARCH_BUDGET_SECONDS", "WEB_SEARCH_BUDGET_SECONDS",
                 "WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS", "ADVISOR_LOG_LEVEL", "ADVISOR_LOG_FORMAT"):
        assert "runtimeSettings" in settings[name]
        assert name in settings[name]
    assert settings["ADVISOR_ENTRA_CREDENTIAL"] == "managed_identity"
    assert settings["CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE"] == "UserManagedIdentity"


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_opt_in_dataset_path_reaches_runtime_settings(compiled_infra, target):
    settings = body(compiled_infra(target), "settings")["outputs"]["settings"]["value"]
    assert settings["BENCHMARK_CASES_PATH"] == (
        "[if(parameters('benchmarkEnabled'), "
        "format('{0}/benchmark-cases.json', parameters('runtimeConfigPath')), '')]"
    )


def test_aca_benchmark_identity_is_the_actual_resource_not_caller_settings(compiled_infra):
    app = body(compiled_infra("aca"), "app")
    expression = app["variables"]["protectedSettings"]
    arguments = arm_arguments(expression)
    assert arguments[0] == "parameters('settings')"
    assert arm_object(arguments[-1])["BENCHMARK_TARGET_ID"] == (
        "[resourceId('Microsoft.App/containerApps', parameters('name'))]"
    )
    container = only(app, "Microsoft.App/containerApps")["properties"]["template"]["containers"][0]
    assert "variables('settingsEnv')" in container["env"]
    settings_env = next(value for value in app["variables"]["copy"] if value["name"] == "settingsEnv")
    assert settings_env["count"] == "[length(items(variables('protectedSettings')))]"
    assert settings_env["input"] == {
        "name": "[items(variables('protectedSettings'))[copyIndex('settingsEnv')].key]",
        "value": "[string(items(variables('protectedSettings'))[copyIndex('settingsEnv')].value)]",
    }


def test_flex_benchmark_identity_is_the_actual_resource_not_caller_settings(compiled_infra):
    expression = argument(body(compiled_infra("functions"), "app"), "flex-site", "appSettingsKeyValuePairs")
    arguments = arm_arguments(expression)
    assert "parameters('settings')" in arguments[:-1]
    assert arm_object(arguments[-1])["BENCHMARK_TARGET_ID"] == (
        "[resourceId('Microsoft.Web/sites', parameters('name'))]"
    )

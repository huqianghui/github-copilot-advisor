"""Guarded command definitions; default execution is read-only WhatIf."""

from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5
import argparse
import base64
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time

from advisor_deploy.config import SUBSCRIPTION, TENANT, _resource_id
from advisor_deploy.manifest import (
    GROUPS, ROLES, arm_parts, experiment_resource, fail, group_id, guid, load_manifest,
    new_manifest, read_json, timestamp, validate_cleanup_scope, validate_manifest, validate_role, write_manifest,
    is_provider_auxiliary,
)
from advisor_deploy.package import BASE_IMAGE, MANIFEST, validate_package
from advisor_deploy.render import PrivateFiles, command_environment, digest, load_plan, parameters_file, verify_current_artifact


ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"
OWNER_TAG = "advisorDeploymentId"
FLEX_SCALE_GROUPS = ("http", "process_queue", "maintenance")


class CliFailure(ValueError):
    def __init__(self, phase: str, code: str = "command_failed", exit_code: int = 1):
        self.phase, self.code, self.exit_code = phase, code, exit_code
        super().__init__(f"deployment guard: {phase}:{code}")


def confirmation(kind: str, target: str, run_id: str, intent: object) -> str:
    return f"{kind}:{target}:{run_id}:{digest(intent)[:16]}"


class Cli:
    def __init__(self, mode: str, repo: Path, private: PrivateFiles):
        self.mode, self.repo, self.private = mode, repo, private
        self.az_path = os.environ.get("ADVISOR_AZ_COMMAND") or shutil.which("az")
        self.docker_path = os.environ.get("ADVISOR_DOCKER_COMMAND") or shutil.which("docker")
        self._docker_environment = command_environment()
        self._docker_endpoint = None

    def _resolve_docker_endpoint(self, prefix: list[str]) -> str:
        if self._docker_endpoint is not None:
            return self._docker_endpoint
        environment = dict(self._docker_environment)
        context = environment.get("DOCKER_CONTEXT", "")
        endpoint = environment.get("DOCKER_HOST", "")

        def discover(arguments):
            try:
                result = subprocess.run(
                    [*prefix, *arguments], cwd=self.repo, env=environment,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise CliFailure("docker_context", "unavailable_or_timeout") from None
            if result.returncode or not result.stdout.strip():
                raise CliFailure("docker_context", "resolution_failed", result.returncode or 1)
            return result.stdout.strip()

        # Docker's explicit context environment takes precedence over HOST;
        # otherwise HOST takes precedence over the saved default context.
        if context:
            environment.pop("DOCKER_HOST", None)
        elif not endpoint:
            context = discover(["context", "show"])
        if context:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", context):
                raise CliFailure("docker_context", "invalid_context")
            endpoint = discover(["context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"])
        local_pipe = bool(re.fullmatch(r"npipe:/{2,4}\./pipe/[A-Za-z0-9_.-]+", endpoint))
        local_socket = (
            endpoint.startswith("unix:///") and not endpoint.startswith("unix:////")
            and not any(value in endpoint for value in ("\\", "?", "#", "%", "\x00"))
            and all(part not in {".", "..", ""} for part in endpoint[len("unix:///"):].split("/"))
        )
        if not endpoint or len(endpoint) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in endpoint) or not (local_pipe or local_socket):
            raise CliFailure("docker_context", "local_endpoint_required")
        self._docker_endpoint = endpoint
        return endpoint

    def run(self, arguments: list[str], phase: str, *, mutation=False, empty=False, raw=False, tool="az", stdin=None, timeout=None):
        if timeout is not None and (type(timeout) not in {int, float} or not 0 < timeout <= 1200):
            raise fail("invalid_command_timeout")
        if mutation and self.mode != "Apply":
            raise fail("mutation_requires_explicit_apply")
        if arguments[:2] in (["account", "set"], ["provider", "register"]):
            raise fail("automatic_context_or_provider_change_forbidden")
        executable = self.az_path if tool == "az" else self.docker_path
        if not executable:
            raise CliFailure(phase, "cli_missing")
        prefix = [executable]
        if Path(executable).suffix.lower() == ".py":
            prefix = [sys.executable, executable]
        elif tool == "az" and os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
            # Avoid cmd.exe interpreting metacharacters in otherwise valid paths.
            candidates = [Path(executable).parent.parent / "python.exe", Path(executable).parent / "python.exe"]
            interpreter = next((candidate for candidate in candidates if candidate.is_file()), None)
            if interpreter is None:
                raise CliFailure(phase, "batch_cli_requires_direct_python_launcher")
            prefix = [str(interpreter), "-IB", "-m", "azure.cli"]
        args = [*prefix, *arguments]
        if tool == "az":
            if arguments[:2] != ["account", "show"]:
                args += ["--subscription", SUBSCRIPTION]
            args += ["--only-show-errors", "--output", "json"]
        environment = command_environment()
        if tool == "docker":
            if not arguments or arguments[0].startswith("-"):
                raise CliFailure(phase, "docker_routing_override_forbidden")
            endpoint = self._resolve_docker_endpoint(prefix)
            args = [*prefix, "--host", endpoint, *arguments]
            environment = dict(self._docker_environment)
            for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
                environment.pop(name, None)
            if (self.private.directory / "docker").exists():
                environment["DOCKER_CONFIG"] = str(self.private.directory / "docker")
        try:
            result = subprocess.run(
                args, cwd=self.repo, env=environment, input=stdin, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout if timeout is not None else (1200 if mutation else 120),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CliFailure(phase, "unavailable_or_timeout") from None
        if result.returncode:
            match = re.search(r"\((AuthorizationFailed|Forbidden|ResourceNotFound|SubscriptionNotRegistered|QuotaExceeded|RoleAssignmentExists)\)", result.stderr)
            raise CliFailure(phase, match[1] if match else "command_failed", result.returncode)
        if raw:
            return result.stdout.strip()
        if not result.stdout.strip():
            if empty:
                return None
            raise CliFailure(phase, "empty_response")
        try:
            parsed = json.loads(result.stdout)
            if parsed is None and not empty:
                raise CliFailure(phase, "empty_response")
            return parsed
        except json.JSONDecodeError:
            raise CliFailure(phase, "malformed_json") from None

    def rest(self, method: str, url: str, phase: str, *, body=None, resource=None, headers=None, empty=False, timeout=None):
        arguments = ["rest", "--method", method, "--url", url]
        if body is not None:
            arguments += ["--body", "@" + str(self.private.json(body))]
        if resource:
            arguments += ["--resource", resource]
        if headers:
            arguments += ["--headers", "@" + str(self.private.json(headers)), "--skip-authorization-header"]
        return self.run(arguments, phase, mutation=method.lower() not in {"get", "head"}, empty=empty,
                        **({"timeout": timeout} if timeout is not None else {}))


def deployment_arguments(target: str, action: str, template: Path, parameters: Path, name: str) -> list[str]:
    if target not in {"aca", "functions", "bench"} or action not in {"validate", "what-if", "create"}:
        raise fail("invalid_deployment_scope")
    arguments = ["deployment", "group" if target == "bench" else "sub", action, "--name", name]
    arguments += ["--resource-group", GROUPS["aca"]] if target == "bench" else ["--location", "southeastasia"]
    if target == "bench" and action == "create":
        arguments += ["--mode", "Incremental"]
    if action == "what-if":
        arguments += ["--no-pretty-print", "--result-format", "ResourceIdOnly"]
    return arguments + ["--template-file", str(template), "--parameters", "@" + str(parameters)]


def account(cli: Cli) -> dict:
    result = cli.run(["account", "show"], "account")
    if not isinstance(result, dict) or result.get("id", "").lower() != SUBSCRIPTION:
        raise fail("active_subscription_mismatch")
    if result.get("environmentName", "AzureCloud") != "AzureCloud" or result.get("state", "Enabled") != "Enabled":
        raise fail("unsupported_cloud_or_subscription")
    if str(result.get("tenantId", "")).lower() != TENANT:
        raise fail("active_tenant_mismatch")
    guid(result.get("tenantId"))
    return result


def allowed_action(entries: list[dict], action: str) -> bool:
    return any(
        any(fnmatch.fnmatchcase(action.lower(), pattern.lower()) for pattern in entry.get("actions", []))
        and not any(fnmatch.fnmatchcase(action.lower(), pattern.lower()) for pattern in entry.get("notActions", []))
        for entry in entries
    )


def require_permissions(cli: Cli, scope: str, actions: list[str]) -> None:
    data = cli.rest("get", f"{ARM}{scope}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01", "arm_permissions")
    if not isinstance(data, dict) or not all(allowed_action(data.get("value", []), action) for action in actions):
        raise fail("arm_permissions_unverified")


def inventory(cli: Cli, manifest: dict) -> list[dict]:
    values = cli.run(["resource", "list", "--resource-group", manifest["resource_group"]], "resource_inventory")
    if not isinstance(values, list):
        raise fail("invalid_resource_inventory")
    return [value for value in values if str(value.get("type", "")).lower() not in {
        "microsoft.resources/deployments", "microsoft.authorization/roleassignments",
    }]


def assert_owned_group(cli: Cli, manifest: dict, *, permit_absent=False) -> bool:
    exists = cli.run(["group", "exists", "--name", manifest["resource_group"]], "group_exists")
    if exists is not True:
        if exists is False and permit_absent:
            return False
        raise fail("owned_group_missing")
    data = cli.run(["group", "show", "--name", manifest["resource_group"]], "group_ownership")
    if data.get("tags", {}).get(OWNER_TAG) != manifest["run_id"] or group_id(manifest["target"]).lower() not in {value.lower() for value in manifest["owned_resource_ids"]}:
        raise fail("resource_group_ownership_collision")
    known = {value.lower() for value in manifest["owned_resource_ids"]}
    if any(str(value.get("id", "")).lower() not in known for value in inventory(cli, manifest)):
        raise fail("unrecorded_resource_collision")
    return True


def _runtime_record(value):
    if not isinstance(value, list):
        raise fail("flex_runtime_metadata_invalid")
    matches = []
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("version"), str):
            raise fail("flex_runtime_metadata_invalid")
        if row["name"] != "python" or row["version"] != "3.12":
            continue
        sku = row.get("sku")
        if not isinstance(sku, dict) or not isinstance(sku.get("skuCode"), str):
            raise fail("flex_runtime_metadata_invalid")
        if sku["skuCode"] != "FC1":
            continue
        properties = sku.get("functionAppConfigProperties")
        runtime = properties.get("runtime") if isinstance(properties, dict) else None
        if not isinstance(runtime, dict) or not isinstance(runtime.get("name"), str) or not isinstance(runtime.get("version"), str):
            raise fail("flex_runtime_metadata_invalid")
        if runtime["name"] != row["name"] or runtime["version"] != row["version"]:
            raise fail("flex_runtime_metadata_inconsistent")
        matches.append(sku)
    if not matches:
        raise fail("flex_runtime_capacity_unverified")
    if len(matches) != 1:
        raise fail("flex_runtime_metadata_ambiguous")
    sku = matches[0]
    bounds = sku.get("maximumInstanceCount")
    fields = ("lowestMaximumInstanceCount", "highestMaximumInstanceCount", "defaultValue")
    if not isinstance(bounds, dict) or any(type(bounds.get(name)) is not int for name in fields):
        raise fail("flex_runtime_metadata_invalid")
    low, high, default = (bounds[name] for name in fields)
    if not 1 <= low <= default <= high:
        raise fail("flex_runtime_metadata_inconsistent")
    memories = sku.get("instanceMemoryMB")
    if not isinstance(memories, list) or not memories:
        raise fail("flex_runtime_metadata_invalid")
    supported = set()
    for memory in memories:
        if (
            not isinstance(memory, dict) or not isinstance(memory.get("size"), str)
            or not re.fullmatch(r"[1-9][0-9]*", memory["size"])
            or type(memory.get("isDefault")) is not bool
        ):
            raise fail("flex_runtime_metadata_invalid")
        size = int(memory["size"])
        if size in supported:
            raise fail("flex_runtime_metadata_inconsistent")
        supported.add(size)
    return low, high, supported


def require_capacity(plan, cli: Cli, approved_evidence: str | None, *, bootstrap_only=False) -> None:
    if plan.target != "functions":
        # New ACA environment quota is not measurable before creation.
        return
    runtimes = cli.run(["functionapp", "list-flexconsumption-runtimes", "--location", "southeastasia", "--runtime", "python"], "flex_runtime")
    low, high, supported_memory = _runtime_record(runtimes)
    settings = plan.config.parameters
    if type(settings["maxInstances"]) is not int or not low <= settings["maxInstances"] <= high:
        raise fail("flex_runtime_capacity_unverified")
    if type(settings["instanceMemoryMB"]) is not int or settings["instanceMemoryMB"] not in supported_memory:
        raise fail("flex_runtime_memory_unsupported")
    if bootstrap_only:
        return
    # HTTP routes share one group; the queue and timer each scale separately.
    # Revisit this list whenever another non-HTTP trigger is added.
    projected_memory_mb = len(FLEX_SCALE_GROUPS) * settings["maxInstances"] * settings["instanceMemoryMB"]
    # The published default and generic Microsoft.Web VM quota are not evidence
    # of this subscription's available Flex capacity. Require fresh approved data.
    evidence = plan.capacity_evidence
    if not isinstance(evidence, dict) or set(evidence) != {
        "subscription_id", "location", "kind", "limit_mb", "used_mb", "observed_at", "source",
    } or approved_evidence != digest(evidence):
        raise fail("flex_capacity_evidence_unconfirmed")
    if evidence["subscription_id"] != SUBSCRIPTION or evidence["location"] != "southeastasia" or evidence["kind"] != "flex-regional-memory":
        raise fail("flex_capacity_evidence_scope")
    try:
        observed = datetime.fromisoformat(evidence["observed_at"])
        age = datetime.now(timezone.utc) - observed
        valid_age = timedelta(0) <= age <= timedelta(hours=1)
    except (TypeError, ValueError):
        valid_age = False
    if (
        not valid_age or evidence["source"] not in {"Flex Consumption Quota diagnostic", "Microsoft support confirmed quota"}
        or type(evidence["limit_mb"]) is not int or type(evidence["used_mb"]) is not int
        or evidence["used_mb"] < 0
        or evidence["limit_mb"] - evidence["used_mb"] < projected_memory_mb
    ):
        raise fail("flex_capacity_insufficient_or_stale")


def dependency_checks(plan, cli: Cli) -> None:
    parameters = plan.config.parameters
    for kind in ("openai", "search"):
        identifier = parameters[kind + "ResourceId"]
        mode = parameters[kind + "AuthMode"]
        resource_type = {"openai": "Microsoft.CognitiveServices/accounts", "search": "Microsoft.Search/searchServices"}[kind]
        _resource_id(identifier, resource_type, "dependency", auth_mode=mode)
        name = identifier.split("/")[-1]
        endpoint = parameters[kind + "Endpoint"].rstrip("/")
        expected = f"https://{name}.{'openai.azure.com' if kind == 'openai' else 'search.windows.net'}"
        if mode == "key":
            if endpoint != expected:
                raise fail("dependency_endpoint_mismatch")
            continue
        data = cli.run(["resource", "show", "--ids", identifier], kind + "_resource")
        if str(data.get("id", "")).lower() != identifier.lower():
            raise fail("dependency_resource_mismatch")
        if str(data.get("location", "")).lower().replace(" ", "") != "southeastasia":
            raise fail("dependency_region_mismatch")
        properties = data.get("properties", {})
        if mode == "key" and properties.get("disableLocalAuth"):
            raise fail("explicit_key_auth_disabled")
        if mode == "entra":
            require_permissions(cli, identifier, ["Microsoft.Authorization/roleAssignments/read", "Microsoft.Authorization/roleAssignments/write"])
            if kind == "search" and not (properties.get("disableLocalAuth") or "aadOrApiKey" in (properties.get("authOptions") or {})):
                raise fail("search_entra_auth_unverified")
        name = arm_parts(identifier)[-1]
        endpoint = parameters[kind + "Endpoint"].rstrip("/")
        expected = f"https://{name}.{'openai.azure.com' if kind == 'openai' else 'search.windows.net'}"
        trusted = {expected, str(properties.get("endpoint", "")).rstrip("/")}
        if endpoint not in trusted:
            raise fail("dependency_endpoint_mismatch")
    if parameters["openaiAuthMode"] == "key":
        _key_model_probes(plan, cli)
    else:
        model = arm_parts(parameters["openaiResourceId"])
        for deployment_name in ("modelDeploymentName", "embeddingDeploymentName"):
            data = cli.run([
                "cognitiveservices", "account", "deployment", "show", "--resource-group", model[4],
                "--name", model[-1], "--deployment-name", parameters[deployment_name],
            ], "model_deployment")
            if not isinstance(data, dict) or not data.get("properties", {}).get("model"):
                raise fail("model_deployment_unverified")
    headers = None
    resource = "https://search.azure.com"
    if parameters["searchAuthMode"] == "key":
        headers = {"api-key": plan.config.secret_settings["AZURE_SEARCH_API_KEY"]}
        resource = None
    schema = cli.rest(
        "get", parameters["searchEndpoint"].rstrip("/") + "/indexes/" + parameters["searchIndexName"] + "?api-version=2024-07-01",
        "search_schema", headers=headers, resource=resource,
    )
    vector_fields = [field for field in schema.get("fields", []) if field.get("dimensions") or field.get("vectorSearchDimensions")]
    if not vector_fields or any(field.get("dimensions", field.get("vectorSearchDimensions")) != 3072 for field in vector_fields):
        raise fail("search_vector_dimensions_mismatch")


def _key_model_probes(plan, cli: Cli) -> None:
    parameters = plan.config.parameters
    headers = cli.private.json({"api-key": plan.config.secret_settings["AZURE_OPENAI_API_KEY"],
                                "Content-Type": "application/json"})
    base = parameters["openaiEndpoint"].rstrip("/") + "/openai/deployments/"
    version = parameters["runtimeSettings"]["AZURE_OPENAI_API_VERSION"]
    probes = (
        ("embedding_key_probe", parameters["embeddingDeploymentName"], "embeddings",
         {"input": "Deployment readiness probe."}),
        ("model_key_probe", parameters["modelDeploymentName"], "chat/completions",
         {"messages": [{"role": "user", "content": "Reply OK."}], "max_completion_tokens": 8}),
    )
    for phase, deployment, operation, body in probes:
        payload = cli.private.json(body)
        # Readiness inference is data-plane work, not an ARM or directory mutation.
        result = cli.run([
            "rest", "--method", "post", "--url", base + deployment + "/" + operation + "?api-version=" + version,
            "--headers", "@" + str(headers), "--body", "@" + str(payload), "--skip-authorization-header",
        ], phase, mutation=False)
        if not isinstance(result, dict):
            raise fail("model_data_plane_unverified")
        if phase == "embedding_key_probe":
            rows = result.get("data")
            vector = rows[0].get("embedding") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
            if not isinstance(vector, list) or len(vector) != 3072:
                raise fail("embedding_dimensions_mismatch")
        elif not isinstance(result.get("choices"), list) or not result["choices"]:
            raise fail("model_data_plane_unverified")


def preflight(plan, cli: Cli, *, approved_evidence=None, bootstrap_only=False) -> tuple[dict, dict]:
    if bootstrap_only and (
        plan.target != "functions" or plan.config.parameters["instanceMemoryMB"] != 512
        or plan.config.parameters["benchmarkEnabled"]
    ):
        raise fail("flex_bootstrap_requires_empty_512mb_profile")
    context = account(cli)
    if plan.config.parameters["benchmarkEnabled"]:
        if plan.benchmark_manifest is None:
            raise fail("benchmark_registration_evidence_required")
        benchmark = load_manifest(plan.benchmark_manifest)
        graph = benchmark["graph"]
        parameters = plan.config.parameters
        if benchmark["target"] != "benchmark" or benchmark["tenant_id"] != context["tenantId"] or not graph:
            raise fail("benchmark_registration_evidence_mismatch")
        if any(parameters[name] != graph["client_id"] for name in (
            "benchmarkApiClientId", "benchmarkAudience", "benchmarkReceiverAudience",
        )):
            raise fail("benchmark_v2_audience_mismatch")
        _graph_owner(cli, benchmark)
        grants = cli.rest("get", GRAPH + "/servicePrincipals/" + graph["service_principal_id"] + "/appRoleAssignedTo", "benchmark_grant_evidence")
        if grants.get("@odata.nextLink") or len(graph["grants"]) != 3 or len(grants.get("value", [])) != 3:
            raise fail("benchmark_driver_grant_missing")
        assert_known_graph_grants(grants["value"], graph)
        if len(parameters["benchmarkAllowedPrincipalIds"]) != 1 or len(parameters["benchmarkAllowedClientIds"]) != 1:
            raise fail("benchmark_only_driver_may_submit")
        driver = next((item for item in graph["grants"] if item["principal_id"] == parameters["benchmarkAllowedPrincipalIds"][0]), None)
        if driver is None:
            raise fail("benchmark_driver_grant_missing")
        identity = cli.run(["identity", "show", "--ids", driver["identity_id"]], "benchmark_driver_identity")
        if (identity.get("tags", {}).get("experiment") != "benchmark"
            or identity.get("clientId") != parameters["benchmarkAllowedClientIds"][0]
            or identity.get("principalId") != driver["principal_id"]):
            raise fail("benchmark_only_driver_may_submit")
    existing = load_manifest(plan.manifest_path) if plan.manifest_path.exists() else None
    if bootstrap_only and existing and existing["outputs"].get("app_id"):
        raise fail("flex_bootstrap_requires_unpublished_infrastructure")
    if existing and (existing["run_id"], existing["target"], existing["environment_name"]) != (
        plan.run_id, plan.target, plan.config.parameters["environmentName"]
    ):
        raise fail("manifest_ownership_collision")
    if existing and (not existing["discovery_complete"] or existing["pending_role_assignment_ids"]):
        raise fail("ownership_discovery_incomplete")
    exists = cli.run(["group", "exists", "--name", plan.group], "group_exists")
    if exists and not existing:
        raise fail("resource_group_ownership_collision")
    manifest = existing or new_manifest(plan.target, plan.run_id, plan.config.parameters["environmentName"],
                                        plan.artifact.content_sha256, plan.configuration_digest)
    if exists:
        assert_owned_group(cli, manifest)
    elif exists is not False:
        raise fail("invalid_group_exists_response")
    if manifest["tenant_id"] and manifest["tenant_id"] != context["tenantId"]:
        raise fail("tenant_changed")
    namespaces = ["Microsoft.Resources", "Microsoft.Storage", "Microsoft.ManagedIdentity", "Microsoft.Authorization",
                  "Microsoft.OperationalInsights", "Microsoft.Insights", "Microsoft.BotService"]
    namespaces += ["Microsoft.App", "Microsoft.ContainerRegistry"] if plan.target == "aca" else ["Microsoft.Web"]
    for namespace in namespaces:
        provider = cli.run(["provider", "show", "--namespace", namespace], "provider_registration")
        if provider.get("registrationState") != "Registered":
            raise fail("provider_registration_required")
    require_permissions(cli, f"/subscriptions/{SUBSCRIPTION}", ["Microsoft.Resources/deployments/write"])
    scope = group_id(plan.target) if exists else f"/subscriptions/{SUBSCRIPTION}"
    require_permissions(cli, scope, [
        "Microsoft.Resources/subscriptions/resourceGroups/write", "Microsoft.Authorization/roleAssignments/read",
        "Microsoft.Authorization/roleAssignments/write", "Microsoft.ManagedIdentity/userAssignedIdentities/write",
        "Microsoft.Storage/storageAccounts/write", "Microsoft.BotService/botServices/write",
        "Microsoft.OperationalInsights/workspaces/write", "Microsoft.Insights/components/write",
        *(["Microsoft.App/managedEnvironments/write", "Microsoft.App/containerApps/write", "Microsoft.ContainerRegistry/registries/write"]
          if plan.target == "aca" else ["Microsoft.Web/serverfarms/write", "Microsoft.Web/sites/write"]),
    ])
    if not bootstrap_only:
        dependency_checks(plan, cli)
    require_capacity(plan, cli, approved_evidence, bootstrap_only=bootstrap_only)
    manifest["tenant_id"] = context["tenantId"]
    intent = [plan.artifact.content_sha256, plan.configuration_digest]
    if plan.target == "functions":
        intent.append("bootstrap_only" if bootstrap_only else "full")
    token = confirmation("APPLY", plan.target, plan.run_id, intent)
    report = {
        "ok": True, "target": plan.target, "scope": "subscription", "resource_group": plan.group,
        "confirmation": token, "artifact_digest": plan.artifact.content_sha256,
        "application_validation": "not_run",
        "pending_gates": ["application_identity_data_plane", "functional_acceptance"],
    }
    if plan.target == "aca" and not exists:
        report["pending_gates"].append("new_environment_vcpu_quota")
    if plan.target == "functions":
        report["phase"] = "bootstrap_only" if bootstrap_only else "full"
        report["regional_capacity"] = "unknown_deferred" if bootstrap_only else "confirmed"
        if bootstrap_only:
            report["pending_gates"] += ["flex_regional_capacity", "dependency_data_plane"]
        projected_memory_mb = len(FLEX_SCALE_GROUPS) * plan.config.parameters["maxInstances"] * plan.config.parameters["instanceMemoryMB"]
        report["capacity_projection"] = {
            "scale_groups": list(FLEX_SCALE_GROUPS), "memory_mb": projected_memory_mb,
            "equivalent_cores": projected_memory_mb / 2048, "capacity_reserved": False,
        }
    return report, manifest


def _receipt_deployment_args(identifier, allowed_groups, *, operations=False):
    if not isinstance(identifier, str):
        raise fail("nested_deployment_scope")
    prefix = rf"/subscriptions/{SUBSCRIPTION}"
    match = re.fullmatch(prefix + r"/providers/Microsoft.Resources/deployments/([A-Za-z0-9_.()-]+)",
                         identifier, re.IGNORECASE)
    group = None
    if match:
        scope, name = "sub", match[1]
    else:
        parts = arm_parts(identifier)
        if (len(parts) != 9 or [p.lower() for p in parts[6:8]] != ["microsoft.resources", "deployments"]
                or parts[4].lower() not in allowed_groups):
            raise fail("nested_deployment_scope")
        scope, group, name = "group", parts[4], parts[8]
    args = ["deployment", "operation", scope, "list"] if operations else ["deployment", scope, "show"]
    args += ["--name", name]
    if group:
        args += ["--resource-group", group]
    # Neither request/response bodies nor outputs/parameters enter recovery logs.
    query = ("[].{operationId:operationId,properties:{provisioningOperation:properties.provisioningOperation,"
             "provisioningState:properties.provisioningState,targetResource:properties.targetResource}}"
             if operations else "{id:id,properties:{provisioningState:properties.provisioningState,outputResources:properties.outputResources}}")
    return args + ["--query", query]


def _record_receipts(plan, cli: Cli, manifest: dict, deployment_name: str, *, persist=True, receipt_ids=None) -> None:
    dependencies = {
        plan.config.parameters[kind + "ResourceId"].lower()
        for kind in ("openai", "search") if plan.config.parameters[kind + "AuthMode"] == "entra"
    }
    allowed_groups = {plan.group.lower()} | {arm_parts(identifier)[4].lower() for identifier in dependencies}
    root = f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Resources/deployments/{deployment_name}"
    queue = [(root, 0)]
    owned = {value.lower(): value for value in manifest["owned_resource_ids"]}
    pending = {value.lower(): value for value in manifest["pending_role_assignment_ids"]}
    seen = set()
    manifest["discovery_complete"] = False

    def save():
        manifest["owned_resource_ids"] = sorted(owned.values())
        resolved = {role["id"].lower() for role in manifest["role_assignments"]}
        manifest["pending_role_assignment_ids"] = sorted(v for k, v in pending.items() if k not in resolved)
        manifest["updated_at"] = timestamp()
        if persist:
            write_manifest(plan.manifest_path, manifest, plan.repository)

    def read(args, phase, identifier):
        try:
            return cli.run(args, phase)
        except Exception as error:
            error.receipt_deployment = identifier
            raise

    try:
        while queue:
            deployment_id, depth = queue.pop()
            if deployment_id.lower() in seen:
                continue
            seen.add(deployment_id.lower())
            if depth > 16 or len(seen) > 500:
                raise fail("deployment_receipt_limit")
            args = _receipt_deployment_args(deployment_id, allowed_groups)
            deployment = read(args, "deployment_receipts", deployment_id)
            if not isinstance(deployment, dict) or (
                deployment.get("id") and deployment["id"].lower() != deployment_id.lower()
            ):
                raise fail("deployment_receipt_identity_mismatch")
            properties = deployment.get("properties")
            if not isinstance(properties, dict):
                raise fail("deployment_receipts_unavailable")
            state = properties.get("provisioningState")
            if state is not None and state not in {"Succeeded", "Failed", "Canceled", "Cancelled"}:
                raise fail("deployment_receipts_not_terminal")
            receipts = properties.get("outputResources")
            if receipts is not None and not isinstance(receipts, list):
                raise fail("deployment_receipts_unavailable")
            targets = [(item.get("id") if isinstance(item, dict) else None) for item in receipts or []]
            if receipts is None or state in {"Failed", "Canceled", "Cancelled"}:
                operations = read(_receipt_deployment_args(deployment_id, allowed_groups, operations=True),
                                  "deployment_operation_receipts", deployment_id)
                if not isinstance(operations, list) or len(operations) > 2000:
                    raise fail("deployment_operations_unavailable")
                for operation in operations:
                    entry = operation.get("properties") if isinstance(operation, dict) else None
                    if not isinstance(entry, dict):
                        raise fail("deployment_operation_invalid")
                    if entry.get("provisioningOperation") != "Create":
                        continue
                    target = entry.get("targetResource")
                    identifier = target.get("id") if isinstance(target, dict) else None
                    if not isinstance(identifier, str):
                        raise fail("deployment_operation_target_missing")
                    nested = "/providers/microsoft.resources/deployments/" in identifier.lower()
                    # Failed nested deployments may contain successful creates.
                    # Read/Action and failed leaf writes never prove ownership.
                    if nested or entry.get("provisioningState") == "Succeeded":
                        targets.append(identifier)
            for identifier in targets:
                if not isinstance(identifier, str):
                    raise fail("deployment_receipt_invalid")
                if "/providers/microsoft.resources/deployments/" in identifier.lower():
                    _receipt_deployment_args(identifier, allowed_groups)
                    queue.append((identifier, depth + 1))
                elif "/providers/microsoft.authorization/roleassignments/" in identifier.lower():
                    parts = arm_parts(identifier)
                    scope = "/".join(parts[:-4])
                    if [p.lower() for p in parts[-4:-1]] != ["providers", "microsoft.authorization", "roleassignments"]:
                        raise fail("receipt_role_scope_mismatch")
                    guid(parts[-1])
                    if scope.lower() not in dependencies and arm_parts(scope)[4].lower() != plan.group.lower():
                        raise fail("receipt_role_scope_mismatch")
                    pending.setdefault(identifier.lower(), identifier)
                else:
                    experiment_resource(identifier)
                    if arm_parts(identifier)[4].lower() != plan.group.lower():
                        raise fail("receipt_group_mismatch")
                    if receipt_ids is not None:
                        receipt_ids.add(identifier)
                    # This exact auxiliary always requires its separate provider
                    # provenance; an ordinary ARM receipt cannot bypass it.
                    if not is_provider_auxiliary(identifier):
                        owned.setdefault(identifier.lower(), identifier)
                if len(owned) + len(pending) > 500:
                    raise fail("deployment_receipt_limit")
            save()
        unknown = sorted(resource["id"] for resource in inventory(cli, manifest) if resource["id"].lower() not in owned)
        if unknown:
            error = fail("unrecorded_resource_collision")
            error.unrecorded_resource_ids = unknown
            raise error
        manifest["discovery_complete"] = True
    finally:
        save()


def _record_roles(plan, cli: Cli, manifest: dict, *, partial=False, persist=True) -> None:
    outputs = manifest["outputs"]
    identities = {}
    if partial:
        for identifier in manifest["owned_resource_ids"]:
            if "/providers/microsoft.managedidentity/userassignedidentities/" not in identifier.lower():
                continue
            identity = cli.run(["identity", "show", "--ids", identifier, "--query",
                                "{id:id,principalId:principalId,clientId:clientId,tenantId:tenantId}"], "receipt_identity_readback")
            if identity.get("id", "").lower() != identifier.lower() or identity.get("tenantId", "").lower() != manifest["tenant_id"]:
                raise fail("receipt_identity_mismatch")
            principal = guid(identity.get("principalId"))
            guid(identity.get("clientId"))
            if principal in identities:
                raise fail("receipt_identity_ambiguous")
            identities[principal] = identifier
    for identifier in list(manifest["pending_role_assignment_ids"]):
        value = cli.rest("get", ARM + identifier + "?api-version=2022-04-01", "role_readback")
        properties = value.get("properties", {})
        principal = properties.get("principalId")
        identity_id = identities.get(str(principal).lower()) if partial else outputs["identity_id"]
        if not identity_id or (not partial and principal != outputs["identity_principal_id"]):
            raise fail("role_principal_mismatch")
        record = {
            "id": identifier, "scope": "/".join(identifier.split("/")[:-4]),
            "identity_id": identity_id, "principal_id": principal,
            "role_definition_id": properties.get("roleDefinitionId"),
        }
        if partial and arm_parts(record["scope"])[4].lower() == plan.group.lower() and (
            record["scope"].lower() not in {v.lower() for v in manifest["owned_resource_ids"]}
        ):
            raise fail("receipt_role_scope_not_owned")
        validate_role(record, manifest["owned_resource_ids"])
        manifest["role_assignments"] = [role for role in manifest["role_assignments"] if role["id"] != identifier] + [record]
        manifest["pending_role_assignment_ids"].remove(identifier)
        if persist:
            write_manifest(plan.manifest_path, manifest, plan.repository)
    if not partial:
        require_runtime_roles(plan, manifest)


def reconcile_receipts(path, repo, cli, deployment_name=None, supplied_confirmation=None):
    from copy import deepcopy
    from types import SimpleNamespace
    from advisor_deploy.paths import has_link
    if not path.is_relative_to(repo / "deploy" / "out") or ".." in path.parts or has_link(path):
        raise fail("reconcile_manifest_scope")
    original = load_manifest(path)
    if original["target"] not in {"aca", "functions"} or original["status"] not in {"failed", "provisioning", "infrastructure_ready"}:
        raise fail("reconcile_partial_deployment_required")
    context = account(cli)
    if original["tenant_id"] != context["tenantId"]:
        raise fail("tenant_changed")
    expected = f"advisor-{original['target']}-{original['run_id']}-"
    deployment_name = deployment_name or expected + "infra"
    if deployment_name not in {expected + suffix for suffix in (("infra", "app") if original["target"] == "aca" else ("infra",))}:
        raise fail("reconcile_deployment_scope")
    group = cli.run(["group", "show", "--name", original["resource_group"]], "reconcile_group_ownership")
    if (group.get("id", "").lower() != group_id(original["target"]).lower()
            or group.get("tags", {}).get(OWNER_TAG) != original["run_id"]
            or group_id(original["target"]).lower() not in {v.lower() for v in original["owned_resource_ids"]}):
        raise fail("resource_group_ownership_collision")
    parameters = {}
    for kind in ("openai", "search"):
        parameters[kind + "AuthMode"] = original["summary"].get(kind + "_auth", "key")
        if parameters[kind + "AuthMode"] == "entra":
            parameters[kind + "ResourceId"] = original["dependencies"][kind]
    plan = SimpleNamespace(repository=repo, manifest_path=path, group=original["resource_group"],
                           target=original["target"], config=SimpleNamespace(parameters=parameters))
    proposed = deepcopy(original)
    unknown = []
    receipt_ids = set()
    try:
        _record_receipts(plan, cli, proposed, deployment_name, persist=False, receipt_ids=receipt_ids)
    except ValueError as error:
        if str(error) != "deployment guard: unrecorded_resource_collision" or not getattr(error, "unrecorded_resource_ids", None):
            raise
        unknown = error.unrecorded_resource_ids
    from advisor_deploy.provider_auxiliary import prove_auxiliary, save_provenance
    proofs, auxiliary_errors, remaining = [], [], []
    for identifier in unknown:
        if not is_provider_auxiliary(identifier):
            remaining.append(identifier)
            continue
        try:
            proof = prove_auxiliary(cli, proposed, receipt_ids, identifier)
        except (ValueError, CliFailure) as error:
            auxiliary_errors.append({"resource_id": identifier, **_recovery_error(error)})
            remaining.append(identifier)
        else:
            proofs.append(proof)
            proposed["owned_resource_ids"].append(identifier)
    unknown = remaining
    proposed["owned_resource_ids"].sort()
    proposed["discovery_complete"] = not unknown
    _record_roles(plan, cli, proposed, partial=True, persist=False)
    intent = [deployment_name, digest(original), proposed["owned_resource_ids"], proposed["role_assignments"],
              unknown, proofs, auxiliary_errors]
    token = confirmation("RECONCILE", original["target"], original["run_id"], intent)
    report = {"ok": True, "mode": cli.mode, "confirmation": token, "deployment_name": deployment_name,
              "status": original["status"], "resource_count": len(proposed["owned_resource_ids"]),
              "role_count": len(proposed["role_assignments"]), "discovery_complete": proposed["discovery_complete"],
              "resume_allowed": proposed["discovery_complete"] and not proposed["pending_role_assignment_ids"],
              "blockers": ["unrecorded_resource_collision"] if unknown else [], "unrecorded_resource_ids": unknown,
              "provider_auxiliary_evidence": proofs, "provider_auxiliary_errors": auxiliary_errors,
              "application_validation": "not_run", "azure_mutations": False}
    if cli.mode == "Apply":
        if supplied_confirmation != token:
            raise fail("reconcile_confirmation_required")
        if read_json(path) != original:
            raise fail("reconcile_manifest_changed")
        for proof in proofs:
            save_provenance(path, proposed, proof, repo)
        write_manifest(path, proposed, repo)
    return report


def require_runtime_roles(plan, manifest: dict) -> None:
    outputs = manifest["outputs"]
    params = plan.config.parameters
    expected = {
        (outputs["storage_id"].lower(), ROLES["blob"] if plan.target == "aca" else ROLES["blob_owner"]),
        (outputs["storage_id"].lower(), ROLES["queue"]),
    }
    kind, role = ("microsoft.containerregistry/registries", ROLES["pull"]) if plan.target == "aca" else (
        "microsoft.insights/components", ROLES["metrics"])
    scopes = [identifier for identifier in manifest["owned_resource_ids"] if f"/providers/{kind}/" in identifier.lower()]
    if len(scopes) != 1:
        raise fail("required_application_roles_missing")
    expected.add((scopes[0].lower(), role))
    for dependency in ("openai", "search"):
        if params[dependency + "AuthMode"] == "entra":
            expected.add((params[dependency + "ResourceId"].lower(), ROLES[dependency]))
    current = {
        (record["scope"].lower(), record["role_definition_id"].split("/")[-1].lower())
        for record in manifest["role_assignments"] if record["id"] in outputs["created_role_assignment_ids"]
    }
    if not expected <= current:
        raise fail("required_application_roles_missing")


def _recovery_error(error, phase=None):
    code = error.code if isinstance(error, CliFailure) else (
        str(error).removeprefix("deployment guard: ") if str(error).startswith("deployment guard: ") else "recovery_failed"
    )
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_ :.-]{1,160}", code):
        code = "recovery_failed"
    result = {"phase": phase or (error.phase if isinstance(error, CliFailure) else "guard"), "code": code}
    if getattr(error, "receipt_deployment", None):
        result["deployment_id"] = error.receipt_deployment
    return result


def _save_recovery_report(plan, manifest, deployment_name, primary, errors):
    from advisor_deploy.paths import has_link
    path = plan.manifest_path.with_suffix(".recovery.json")
    if not path.is_relative_to(plan.repository / "deploy" / "out") or ".." in path.parts or has_link(path):
        raise fail("recovery_report_scope")
    pending = path.with_name(path.name + "." + uuid4().hex + ".next")
    value = {
        "format": "advisor-receipt-recovery-v1", "deployment_name": deployment_name,
        "run_id": plan.run_id, "primary_error": _recovery_error(primary) if primary else None,
        "recovery_errors": errors, "discovery_complete": manifest["discovery_complete"],
        "resource_count": len(manifest["owned_resource_ids"]),
        "pending_role_count": len(manifest["pending_role_assignment_ids"]), "observed_at": timestamp(),
    }
    try:
        with pending.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _infra_apply(plan, cli: Cli, manifest: dict, parameters: Path, suffix: str) -> None:
    # This caller has already proved ownership (or created the new group).
    # The current RG Bicep module replaces tags, so restore the ownership marker
    # in finally; never use this path to adopt an unmarked preexisting group.
    deployment_name = f"advisor-{plan.target}-{plan.run_id}-{suffix}"
    manifest["discovery_complete"] = False
    write_manifest(plan.manifest_path, manifest, plan.repository)
    primary = None
    try:
        result = cli.run(deployment_arguments(plan.target, "create",
            plan.repository / "deploy" / plan.target / "infra" / "main.bicep", parameters,
            deployment_name), "infrastructure_apply", mutation=True)
        values = result.get("properties", {}).get("outputs")
        if not isinstance(values, dict):
            raise fail("deployment_outputs_unavailable")
        outputs = {name: value["value"] for name, value in values.items()}
        _record_receipts(plan, cli, manifest, deployment_name)
        manifest["outputs"] = outputs
        manifest["pending_role_assignment_ids"] = sorted(set(manifest["pending_role_assignment_ids"]) | set(outputs.get("created_role_assignment_ids", [])))
        write_manifest(plan.manifest_path, manifest, plan.repository)
        _record_roles(plan, cli, manifest)
    except BaseException as error:
        primary = error
        raise
    finally:
        failures = []
        try:
            cli.run(["group", "update", "--name", plan.group, "--set", f"tags.{OWNER_TAG}={plan.run_id}"], "restore_ownership_tag", mutation=True)
        except Exception as error:
            failures.append((_recovery_error(error, "restore_ownership_tag"), error))
        if not manifest["discovery_complete"]:
            try:
                _record_receipts(plan, cli, manifest, deployment_name)
            except Exception as error:
                failures.append((_recovery_error(error, "receipt_recovery"), error))
        if primary is not None or failures:
            diagnostics = [item for item, _ in failures]
            try:
                _save_recovery_report(plan, manifest, deployment_name, primary, diagnostics)
            except Exception as error:
                diagnostics.append(_recovery_error(error, "recovery_report"))
                if primary is None and not failures:
                    failures.append((diagnostics[-1], error))
            if primary is not None:
                primary.recovery_errors = diagnostics
            elif failures:
                failures[0][1].recovery_errors = diagnostics
                raise failures[0][1]


def verify_app(plan, cli: Cli, manifest: dict) -> None:
    outputs = manifest["outputs"]
    if not outputs.get("app_id"):
        raise fail("application_missing")
    version = "2025-01-01" if plan.target == "aca" else "2024-04-01"
    app = cli.run(["resource", "show", "--ids", outputs["app_id"], "--api-version", version], "application_readback")
    identities = app.get("identity", {}).get("userAssignedIdentities", {})
    if {outputs["identity_id"].lower()} != {key.lower() for key in identities} or app.get("identity", {}).get("type") != "UserAssigned":
        raise fail("application_identity_mismatch")
    properties = app.get("properties", {})
    params = plan.config.parameters
    if plan.target == "aca":
        scale = properties.get("template", {}).get("scale", {})
        if scale.get("minReplicas") != params["minReplicas"] or scale.get("maxReplicas") != params["maxInstances"]:
            raise fail("aca_scale_mismatch")
        container = properties.get("template", {}).get("containers", [{}])[0]
        if container.get("image") != manifest["image_reference"]:
            raise fail("immutable_image_readback_mismatch")
        sizes = {"quarter": (0.25, "0.5Gi"), "half": (0.5, "1Gi"), "one": (1, "2Gi"), "two": (2, "4Gi")}
        size = container.get("resources", {})
        if (size.get("cpu"), size.get("memory")) != sizes[params["resourceProfile"]]:
            raise fail("aca_size_mismatch")
        settings = {item["name"]: item.get("value") for item in container.get("env", [])}
    else:
        config = properties.get("functionAppConfig", {})
        scale = config.get("scaleAndConcurrency", {})
        if config.get("runtime") != {"name": "python", "version": "3.12"} or (
            scale.get("instanceMemoryMB") != params["instanceMemoryMB"]
            or scale.get("maximumInstanceCount") != params["maxInstances"] or scale.get("alwaysReady", [])
            or scale.get("triggers", {}).get("http", {}).get("perInstanceConcurrency") != params["workerParallelism"]
        ):
            raise fail("flex_configuration_mismatch")
        settings = {item["name"]: item.get("value") for item in cli.run([
            "functionapp", "config", "appsettings", "list", "--resource-group", plan.group,
            "--name", outputs["app_name"],
        ], "function_settings_readback")}
        for name, expected in {
            "AdvisorQueue__credential": "managedidentity", "AdvisorQueue__clientId": outputs["identity_client_id"],
            "AdvisorQueue__queueServiceUri": outputs["queue_url"],
            "AzureWebJobsStorage__credential": "managedidentity", "AzureWebJobsStorage__clientId": outputs["identity_client_id"],
            "AzureWebJobsStorage__blobServiceUri": outputs["blob_url"], "AzureWebJobsStorage__queueServiceUri": outputs["queue_url"],
            "AzureFunctionsJobHost__extensions__queues__batchSize": str(params["workerParallelism"]),
            "AzureFunctionsJobHost__extensions__queues__newBatchThreshold": "0",
        }.items():
            if settings.get(name) != expected:
                raise fail("function_binding_configuration_mismatch")
    expected = {
        "AZURE_CLIENT_ID": outputs["identity_client_id"],
        "ADVISOR_ENTRA_CREDENTIAL": "managed_identity", "ADVISOR_STORAGE_AUTH_MODE": "entra",
        "AZURE_OPENAI_CHAT_DEPLOYMENT": params["modelDeploymentName"],
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": params["embeddingDeploymentName"],
        "ADVISOR_WORKER_PARALLELISM": str(params["workerParallelism"]),
        "AZURE_OPENAI_AUTH_MODE": params["openaiAuthMode"], "AZURE_SEARCH_AUTH_MODE": params["searchAuthMode"],
    } | params["runtimeSettings"]
    if any(settings.get(name) != value for name, value in expected.items()):
        raise fail("application_settings_mismatch")


def build_current_image(plan, cli: Cli, tag: str):
    if not plan.artifact_path.is_relative_to(plan.repository / "deploy" / "out"):
        raise fail("aca_context_must_be_owned_output")
    if cli.run(["info", "--format", "{{.OSType}}"], "docker_platform", tool="docker", raw=True) != "linux":
        raise fail("linux_docker_engine_required")
    cli.run(["pull", "--platform", "linux/amd64", BASE_IMAGE], "base_image_pull", tool="docker", raw=True, mutation=True)
    reference = cli.run(["image", "inspect", BASE_IMAGE, "--format", "{{index .RepoDigests 0}}"],
                        "base_image_digest", tool="docker", raw=True)
    if not re.fullmatch(r"(?:docker.io/library/)?python@sha256:[a-f0-9]{64}", reference):
        raise fail("base_image_digest_unverified")
    artifact = replace(plan.artifact, base_image_digest=reference.split("@")[1], linux_imports_verified=False)
    manifest_path = plan.artifact_path / MANIFEST
    manifest_path.write_text(json.dumps(artifact.to_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    cli.run(["build", "--platform", "linux/amd64", "--build-arg", "BASE_IMAGE=" + reference,
             "--tag", tag, "--file", str(plan.artifact_path / "Dockerfile"), str(plan.artifact_path)],
            "image_build", tool="docker", raw=True, mutation=True)
    probe = (
        "import advisor_shared,advisor_agent.factory,teams_adapter.__main__\n"
        "from advisor_agent.factory import build_advisor\n"
        "try:\n build_advisor(channel_name='teams')\n"
        "except ValueError as error:\n assert 'AZURE_SEARCH_ENDPOINT' in str(error)\n"
        "else:\n raise AssertionError('missing configuration accepted')\n"
    )
    cli.run(["run", "--rm", "--network", "none", "--platform", "linux/amd64", "--entrypoint", "python", tag, "-c", probe],
            "image_import_check", tool="docker", raw=True, mutation=True)
    artifact = replace(artifact, linux_imports_verified=True)
    manifest_path.write_text(json.dumps(artifact.to_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return validate_package(plan.artifact_path)


def deploy(plan, cli: Cli, private: PrivateFiles, supplied_confirmation: str | None, approved_evidence=None, *, bootstrap_only=False) -> dict:
    report, manifest = preflight(plan, cli, approved_evidence=approved_evidence, bootstrap_only=bootstrap_only)
    template = plan.repository / "deploy" / plan.target / "infra" / "main.bicep"
    params = parameters_file(plan, private)
    name = f"advisor-{plan.target}-{plan.run_id}"
    cli.run(deployment_arguments(plan.target, "validate", template, params, name), "template_validate")
    cli.run(deployment_arguments(plan.target, "what-if", template, params, name), "template_what_if")
    if cli.mode == "WhatIf":
        stages = (["empty-infrastructure"] if bootstrap_only else
                  ["bootstrap", "build/push", "immutable-app"] if plan.target == "aca" else ["infrastructure", "OneDeploy-remote-build"])
        return report | {"mode": "WhatIf", "planned_stages": stages}
    if supplied_confirmation != report["confirmation"]:
        raise fail("apply_confirmation_required")
    if plan.target == "aca" and plan.config.build_mode == "local":
        if cli.run(["info", "--format", "{{.OSType}}"], "docker_readiness", tool="docker", raw=True) != "linux":
            raise CliFailure("docker_readiness", "linux_engine_required")
    manifest.update(artifact_digest=plan.artifact.content_sha256, config_digest=plan.configuration_digest,
                    summary=plan.summary(), status="provisioning", updated_at=timestamp())
    manifest["dependencies"] = {"openai": plan.config.parameters["openaiResourceId"], "search": plan.config.parameters["searchResourceId"]}
    write_manifest(plan.manifest_path, manifest, plan.repository)
    try:
        exists = cli.run(["group", "exists", "--name", plan.group], "group_recheck")
        if exists is False:
            created = cli.run(["group", "create", "--name", plan.group, "--location", "southeastasia",
                               "--tags", f"{OWNER_TAG}={plan.run_id}"], "group_create", mutation=True)
            if created.get("tags", {}).get(OWNER_TAG) != plan.run_id:
                raise fail("created_group_ownership_unverified")
            manifest["owned_resource_ids"] = [group_id(plan.target)]
            write_manifest(plan.manifest_path, manifest, plan.repository)
        else:
            assert_owned_group(cli, manifest)
        _infra_apply(plan, cli, manifest, params, "infra")
        if bootstrap_only:
            verify_app(plan, cli, manifest)
            manifest.update(status="infrastructure_ready", updated_at=timestamp())
            write_manifest(plan.manifest_path, manifest, plan.repository)
            from advisor_deploy.paths import has_link
            record_path = plan.manifest_path.with_suffix(".bootstrap.json")
            if has_link(record_path):
                raise fail("bootstrap_record_link_forbidden")
            record = private.json({
                "format": "advisor-flex-bootstrap-v1", "phase": "bootstrap_only", "run_id": plan.run_id,
                "status": "infrastructure_ready", "regional_capacity": "unknown_deferred",
                "code_publication": "not_run", "dependency_data_plane": "not_run",
                "app_id": manifest["outputs"]["app_id"], "artifact_digest": manifest["artifact_digest"],
                "config_digest": manifest["config_digest"], "observed_at": timestamp(),
            })
            record.replace(record_path)
            return report | {"mode": "Apply", "status": "infrastructure_ready",
                             "app_id": manifest["outputs"]["app_id"], "code_publication": "not_run"}
        manifest["status"] = "infrastructure_ready"
        write_manifest(plan.manifest_path, manifest, plan.repository)
        plan.artifact = verify_current_artifact(plan.artifact_path, plan.repository, plan.config)
        if plan.target == "aca":
            # Available quota must be checked for the now-created environment
            # before image publication/starting application replicas.
            resources = inventory(cli, manifest)
            environment = next(value for value in resources if value["type"].lower() == "microsoft.app/managedenvironments")
            usage = cli.rest("get", ARM + environment["id"] + "/usages?api-version=2025-01-01", "aca_environment_quota")
            cores = {"quarter": .25, "half": .5, "one": 1, "two": 2}[plan.config.parameters["resourceProfile"]]
            if not any("consumption" in str(item.get("name", {})).lower() and
                       item.get("limit", 0) - item.get("currentValue", 0) >= cores * plan.config.parameters["maxInstances"]
                       for item in usage.get("value", [])):
                raise fail("aca_environment_quota_unverified")
            registry = next(value for value in resources if value["type"].lower() == "microsoft.containerregistry/registries")
            if plan.config.build_mode == "acr":
                from advisor_deploy.remote_acr import TASK_ACTIONS, evidence_path, publish_image
                require_permissions(cli, registry["id"], TASK_ACTIONS)
                proof = publish_image(
                    cli, context=plan.artifact_path, files=plan.artifact.files,
                    binding={"artifact_sha256": plan.artifact.content_sha256,
                             "lock_sha256": plan.artifact.lock_sha256, "config_sha256": plan.configuration_digest},
                    registry=registry["name"], kind="aca", evidence_path=evidence_path(plan.manifest_path),
                )
                plan.artifact = verify_current_artifact(plan.artifact_path, plan.repository, plan.config)
                if proof["binding"]["artifact_sha256"] != plan.artifact.content_sha256:
                    raise fail("acr_artifact_changed")
                manifest["image_reference"] = proof["image_reference"]
            else:
                local_tag = "advisor-local:" + plan.run_id
                # Rebuild current verified bytes. An older true flag is not proof.
                plan.artifact = build_current_image(plan, cli, local_tag)
                plan.artifact = verify_current_artifact(plan.artifact_path, plan.repository, plan.config)
                manifest["artifact_digest"] = plan.artifact.content_sha256
                write_manifest(plan.manifest_path, manifest, plan.repository)
                login = cli.run(["acr", "login", "--name", registry["name"], "--expose-token"], "acr_token", mutation=True)
                server = login.get("loginServer", "")
                if not re.fullmatch(r"[a-z0-9]+\.azurecr\.io", server) or not isinstance(login.get("accessToken"), str):
                    raise fail("invalid_registry_login")
                (private.directory / "docker").mkdir()
                cli.run(["login", server, "--username", "00000000-0000-0000-0000-000000000000", "--password-stdin"],
                        "docker_login", mutation=True, tool="docker", raw=True, stdin=login["accessToken"] + "\n")
                image = "advisor:" + plan.run_id
                cli.run(["tag", local_tag, server + "/" + image], "image_tag", mutation=True, tool="docker", raw=True)
                cli.run(["push", server + "/" + image], "image_push", mutation=True, tool="docker", raw=True)
                value = cli.run(["acr", "repository", "show", "--name", registry["name"], "--image", image], "image_digest")
                immutable = value.get("digest", "")
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", immutable):
                    raise fail("registry_digest_unverified")
                manifest["image_reference"] = server + "/advisor@" + immutable
            write_manifest(plan.manifest_path, manifest, plan.repository)
            params = parameters_file(plan, private, deploy_app=True, image=manifest["image_reference"].split("/", 1)[1])
            _infra_apply(plan, cli, manifest, params, "app")
        else:
            manifest["status"] = "publishing"
            write_manifest(plan.manifest_path, manifest, plan.repository)
            cli.run([
                "functionapp", "deployment", "source", "config-zip", "--resource-group", plan.group,
                "--name", manifest["outputs"]["app_name"], "--src", str(plan.artifact_path),
                "--build-remote", "true", "--timeout", "600",
            ], "function_onedeploy", mutation=True)
        verify_app(plan, cli, manifest)
        manifest["status"] = "deployed"
        manifest["updated_at"] = timestamp()
        write_manifest(plan.manifest_path, manifest, plan.repository)
        return report | {"mode": "Apply", "status": "deployed", "application_validation": "not_run",
                         "pending_gates": ["application_identity_data_plane", "functional_acceptance"]}
    except Exception:
        manifest["status"] = "failed"
        manifest["updated_at"] = timestamp()
        write_manifest(plan.manifest_path, manifest, plan.repository)
        raise


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if not isinstance(claims, dict):
            raise ValueError()
        return claims
    except (IndexError, ValueError, TypeError, AttributeError):
        raise fail("graph_permission_evidence_unavailable") from None


def graph_permissions(cli: Cli, context: dict) -> None:
    token = cli.run(["account", "get-access-token", "--resource-type", "ms-graph"], "graph_permission_token")
    claims = _claims(token.get("accessToken", ""))
    if claims.get("tid") != context["tenantId"] or str(claims.get("aud", "")).rstrip("/") not in {
        "https://graph.microsoft.com", "00000003-0000-0000-c000-000000000000",
    } or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= time.time():
        raise fail("graph_token_scope_unverified")
    required = {"Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All"}
    if claims.get("scp"):
        scopes = set(claims["scp"].split())
        if not (required <= scopes or "Directory.AccessAsUser.All" in scopes):
            raise fail("graph_delegated_permissions_missing")
        roles = cli.rest("get", GRAPH + "/me/transitiveMemberOf/microsoft.graph.directoryRole?$select=displayName,roleTemplateId", "graph_directory_roles")
        if roles.get("@odata.nextLink") or not any(
            role.get("roleTemplateId") and role.get("displayName") in {
                "Global Administrator", "Application Administrator", "Cloud Application Administrator",
            } for role in roles.get("value", [])
        ):
            raise fail("graph_directory_role_unverified")
    elif not required <= set(claims.get("roles", [])):
        raise fail("graph_application_permissions_missing")


def _graph_owner(cli: Cli, manifest: dict, *, incomplete=False) -> tuple[dict, dict | None]:
    graph = manifest["graph"]
    app = cli.rest("get", GRAPH + "/applications/" + graph["application_id"], "benchmark_application")
    expected_role = {
        "id": graph["role_id"], "allowedMemberTypes": ["Application"], "value": "Benchmark.Invoke", "isEnabled": True,
    }
    roles = app.get("appRoles", [])
    if (
        "advisorDeploymentId:" + manifest["run_id"] not in app.get("tags", [])
        or app.get("appId") != graph["client_id"] or app.get("signInAudience") != "AzureADMyOrg"
        or app.get("api", {}).get("requestedAccessTokenVersion") != 2
        or len(roles) != 1 or any(roles[0].get(key) != value for key, value in expected_role.items())
    ):
        raise fail("benchmark_application_ownership_or_configuration")
    if not incomplete and app.get("identifierUris") != ["api://" + graph["client_id"]]:
        raise fail("benchmark_audience_mismatch")
    principal = None
    if graph["service_principal_id"]:
        principal = cli.rest("get", GRAPH + "/servicePrincipals/" + graph["service_principal_id"], "benchmark_service_principal")
        if principal.get("appId") != graph["client_id"]:
            raise fail("benchmark_service_principal_mismatch")
    return app, principal


def assert_known_graph_grants(items: list[dict], graph: dict) -> None:
    known = {grant["id"]: grant for grant in graph["grants"]}
    for item in items:
        if item.get("id") not in known:
            raise fail("unrecorded_graph_grant")
        record = known[item["id"]]
        if (item.get("principalId"), item.get("resourceId"), item.get("appRoleId")) != (
            record["principal_id"], graph["service_principal_id"], graph["role_id"],
        ):
            raise fail("graph_grant_changed")


def ensure_benchmark(path: Path, repo: Path, cli: Cli, stage: str, supplied_confirmation: str | None) -> dict:
    plan = read_json(path)
    if set(plan) - {"format", "run_id", "manifest", "principals"} or not {"format", "run_id", "manifest"} <= plan.keys() or plan["format"] != "advisor-benchmark-plan-v1":
        raise fail("invalid_benchmark_plan")
    run_id = guid(plan["run_id"])
    manifest_path = (path.parent / plan["manifest"]).absolute()
    context = account(cli)
    graph_permissions(cli, context)
    existed = manifest_path.exists()
    manifest = load_manifest(manifest_path) if existed else new_manifest("benchmark", run_id, "copilot-advisor-bench", "0" * 64, digest(plan))
    if manifest["target"] != "benchmark" or manifest["run_id"] != run_id:
        raise fail("benchmark_manifest_collision")
    if manifest["tenant_id"] and manifest["tenant_id"] != context["tenantId"]:
        raise fail("tenant_changed")
    manifest["tenant_id"] = context["tenantId"]
    principals = plan.get("principals", {})
    if not isinstance(principals, dict) or set(principals) - {"driver", "aca", "functions"}:
        raise fail("invalid_benchmark_principals")
    resolved = {}
    assignments = None
    if stage == "Grant":
        if set(principals) != {"driver", "aca", "functions"} or not manifest["graph"] or not manifest["graph"]["service_principal_id"]:
            raise fail("benchmark_registration_or_principals_missing")
        _graph_owner(cli, manifest)
        for label, item in principals.items():
            if not isinstance(item, dict) or set(item) != {"manifest", "identity_id"}:
                raise fail("invalid_principal_reference")
            source = load_manifest((path.parent / item["manifest"]).absolute())
            if source["tenant_id"] != context["tenantId"]:
                raise fail("principal_tenant_mismatch")
            assert_owned_group(cli, source)
            validate_cleanup_scope([item["identity_id"]], source["owned_resource_ids"])
            if "/providers/microsoft.managedidentity/userassignedidentities/" not in item["identity_id"].lower():
                raise fail("benchmark_principal_must_be_owned_uami")
            identity = cli.run(["identity", "show", "--ids", item["identity_id"]], "benchmark_principal")
            expected_purpose = {"driver": "benchmark", "aca": "aca", "functions": "flex"}[label]
            if identity.get("tags", {}).get("experiment") != expected_purpose:
                raise fail("benchmark_identity_purpose_mismatch")
            resolved[label] = {"identity_id": item["identity_id"], "principal_id": guid(identity.get("principalId"))}
        if len({item["principal_id"] for item in resolved.values()}) != 3:
            raise fail("benchmark_principals_must_be_distinct")
        assignments = cli.rest("get", GRAPH + "/servicePrincipals/" + manifest["graph"]["service_principal_id"] + "/appRoleAssignedTo", "benchmark_grants")
        if assignments.get("@odata.nextLink"):
            raise fail("benchmark_grants_unverified")
        assert_known_graph_grants(assignments.get("value", []), manifest["graph"])
    name = "copilot-advisor-bench-" + run_id
    graph = manifest["graph"]
    candidate = None
    if graph and graph["application_id"]:
        _graph_owner(cli, manifest, incomplete=stage == "Register")
    else:
        values = cli.rest("get", GRAPH + f"/applications?$filter=displayName eq '{name}'&$select=id,appId,tags", "benchmark_name_collision")
        matches = values.get("value", [])
        if values.get("@odata.nextLink") or len(matches) > 1:
            raise fail("benchmark_name_collision")
        if matches:
            if not existed or manifest["status"] not in {"provisioning", "failed"} or "advisorDeploymentId:" + run_id not in matches[0].get("tags", []):
                raise fail("benchmark_name_collision")
            candidate = matches[0]
    token = confirmation("APPLY", "benchmark", run_id, [stage, resolved])
    report = {"ok": True, "mode": cli.mode, "stage": stage, "confirmation": token, "role": "Benchmark.Invoke",
              "application_validation": "not_run"}
    if cli.mode == "WhatIf":
        return report
    if supplied_confirmation != token:
        raise fail("apply_confirmation_required")
    if graph is None:
        graph = {
            "application_id": "", "client_id": "", "service_principal_id": "",
            "role_id": str(uuid5(UUID(run_id), "Benchmark.Invoke")), "grants": [],
        }
        manifest["graph"] = graph
    manifest["status"] = "provisioning"
    write_manifest(manifest_path, manifest, repo)
    try:
        if stage == "Register":
            if not graph["application_id"]:
                app = candidate or cli.rest("post", GRAPH + "/applications", "benchmark_app_create", body={
                    "displayName": name, "signInAudience": "AzureADMyOrg",
                    "tags": ["copilot-advisor", "advisorDeploymentId:" + run_id],
                    "api": {"requestedAccessTokenVersion": 2},
                    "appRoles": [{
                        "id": graph["role_id"], "allowedMemberTypes": ["Application"],
                        "displayName": "Benchmark.Invoke", "description": "Invoke this isolated experiment API.",
                        "value": "Benchmark.Invoke", "isEnabled": True,
                    }],
                })
                graph["application_id"], graph["client_id"] = guid(app.get("id")), guid(app.get("appId"))
                write_manifest(manifest_path, manifest, repo)
            _graph_owner(cli, manifest, incomplete=True)
            cli.rest("patch", GRAPH + "/applications/" + graph["application_id"], "benchmark_audience", body={
                "identifierUris": ["api://" + graph["client_id"]],
            }, empty=True)
            if not graph["service_principal_id"]:
                existing = cli.rest("get", GRAPH + f"/servicePrincipals?$filter=appId eq '{graph['client_id']}'", "benchmark_sp_lookup")
                if existing.get("@odata.nextLink") or len(existing.get("value", [])) > 1:
                    raise fail("benchmark_service_principal_collision")
                principal = existing["value"][0] if existing.get("value") else cli.rest(
                    "post", GRAPH + "/servicePrincipals", "benchmark_sp_create",
                    body={"appId": graph["client_id"], "appRoleAssignmentRequired": True},
                )
                graph["service_principal_id"] = guid(principal.get("id"))
                write_manifest(manifest_path, manifest, repo)
            _graph_owner(cli, manifest)
        else:
            assert assignments is not None
            for principal in resolved.values():
                matches = [item for item in assignments.get("value", []) if item.get("principalId") == principal["principal_id"]]
                if len(matches) > 1 or any(item.get("appRoleId") != graph["role_id"] for item in matches):
                    raise fail("unrelated_benchmark_grant")
                item = matches[0] if matches else cli.rest(
                    "post", GRAPH + "/servicePrincipals/" + graph["service_principal_id"] + "/appRoleAssignedTo",
                    "benchmark_role_grant", body={
                        "principalId": principal["principal_id"], "resourceId": graph["service_principal_id"], "appRoleId": graph["role_id"],
                    },
                )
                record = {"id": item["id"], "principal_id": principal["principal_id"], "identity_id": principal["identity_id"],
                          "resource_id": graph["service_principal_id"], "role_id": graph["role_id"]}
                if not any(grant["id"] == record["id"] for grant in graph["grants"]):
                    graph["grants"].append(record)
                    write_manifest(manifest_path, manifest, repo)
            readback = cli.rest("get", GRAPH + "/servicePrincipals/" + graph["service_principal_id"] + "/appRoleAssignedTo", "benchmark_grant_readback")
            if readback.get("@odata.nextLink") or len(readback.get("value", [])) != 3:
                raise fail("benchmark_grant_readback_incomplete")
            assert_known_graph_grants(readback["value"], graph)
        manifest["status"] = "infrastructure_ready"
        manifest["updated_at"] = timestamp()
        write_manifest(manifest_path, manifest, repo)
        return report | {"client_id": graph["client_id"], "audience": graph["client_id"],
                         "scope": "api://" + graph["client_id"] + "/.default",
                         "service_principal_id": graph["service_principal_id"]}
    except Exception:
        manifest["status"] = "failed"
        write_manifest(manifest_path, manifest, repo)
        raise


def set_mode(path: Path, repo: Path, cli: Cli, mode: str, supplied_confirmation: str | None) -> dict:
    manifest = load_manifest(path)
    if manifest["target"] != "aca" or not manifest["outputs"].get("app_id"):
        raise fail("mode_requires_owned_aca_app")
    context = account(cli)
    if context["tenantId"] != manifest["tenant_id"]:
        raise fail("tenant_changed")
    assert_owned_group(cli, manifest)
    app_id = manifest["outputs"]["app_id"]
    require_permissions(cli, app_id, ["Microsoft.App/containerApps/write"])
    before = cli.run(["containerapp", "show", "--resource-group", manifest["resource_group"], "--name", manifest["outputs"]["app_name"]], "mode_readback")
    desired = 1 if mode == "Resident" else 0
    token = confirmation("SCALE", "aca", manifest["run_id"], [app_id, desired])
    report = {"ok": True, "mode": cli.mode, "desired_min_replicas": desired, "confirmation": token}
    if cli.mode == "WhatIf":
        return report
    if supplied_confirmation != token:
        raise fail("scale_confirmation_required")
    template = before.get("properties", {}).get("template", {})
    if template.get("scale", {}).get("minReplicas") == desired:
        return report | {"no_op": True}
    cli.run(["containerapp", "update", "--resource-group", manifest["resource_group"], "--name", manifest["outputs"]["app_name"],
             "--min-replicas", str(desired)], "mode_update", mutation=True)
    after = cli.run(["containerapp", "show", "--resource-group", manifest["resource_group"], "--name", manifest["outputs"]["app_name"]], "mode_readback")
    expected = json.loads(json.dumps(template))
    expected.setdefault("scale", {})["minReplicas"] = desired
    if after.get("properties", {}).get("template") != expected:
        raise fail("mode_readback_changed_other_settings")
    manifest["last_control_action"] = {"kind": "scale-mode", "min_replicas": desired, "at": timestamp()}
    manifest["updated_at"] = timestamp()
    write_manifest(path, manifest, repo)
    return report | {"no_op": False}


def cleanup(path: Path, repo: Path, cli: Cli, supplied_confirmation: str | None, traffic_stopped: bool, in_flight: str | None) -> dict:
    manifest = load_manifest(path)
    context = account(cli)
    if manifest["tenant_id"] and context["tenantId"] != manifest["tenant_id"]:
        raise fail("tenant_changed")
    if not manifest["discovery_complete"] or manifest["pending_role_assignment_ids"]:
        raise fail("ownership_discovery_incomplete")
    identifiers = []
    live_roles = []
    if group_id(manifest["target"]) in manifest["owned_resource_ids"]:
        if assert_owned_group(cli, manifest, permit_absent=True):
            require_permissions(cli, group_id(manifest["target"]), ["Microsoft.Resources/subscriptions/resourceGroups/delete"])
            identifiers.append(group_id(manifest["target"]))
    graph = manifest["graph"]
    if graph and manifest["status"] != "cleaned":
        graph_permissions(cli, context)
        _graph_owner(cli, manifest)
        current = cli.rest("get", GRAPH + "/servicePrincipals/" + graph["service_principal_id"] + "/appRoleAssignedTo", "cleanup_graph_grants")
        if current.get("@odata.nextLink") or {item["id"] for item in current.get("value", [])} != {item["id"] for item in graph["grants"]}:
            raise fail("unrecorded_graph_grants")
        for item in current.get("value", []):
            recorded = next(grant for grant in graph["grants"] if grant["id"] == item["id"])
            if (item.get("principalId"), item.get("resourceId"), item.get("appRoleId")) != (
                recorded["principal_id"], recorded["resource_id"], recorded["role_id"]
            ):
                raise fail("graph_grant_changed")
    if manifest["status"] != "cleaned":
        for scope in {role["scope"] for role in manifest["role_assignments"]}:
            require_permissions(cli, scope, ["Microsoft.Authorization/roleAssignments/read", "Microsoft.Authorization/roleAssignments/delete"])
        for role in manifest["role_assignments"]:
            try:
                value = cli.rest("get", ARM + role["id"] + "?api-version=2022-04-01", "cleanup_role")
            except CliFailure as error:
                if error.code == "ResourceNotFound":
                    continue
                raise
            props = value.get("properties", {})
            if props.get("principalId") != role["principal_id"] or str(props.get("roleDefinitionId", "")).lower() != role["role_definition_id"].lower():
                raise fail("recorded_role_changed")
            live_roles.append(role)
            identifiers.append(role["id"])
    validate_cleanup_scope(identifiers, manifest["owned_resource_ids"], manifest["role_assignments"])
    token = confirmation("DELETE", manifest["target"], manifest["run_id"], [identifiers, graph])
    report = {"ok": True, "mode": cli.mode, "confirmation": token, "resource_ids": identifiers,
              "directory_objects": [] if not graph else [graph["application_id"], graph["service_principal_id"]]}
    if cli.mode == "WhatIf" or manifest["status"] == "cleaned":
        return report | {"no_op": manifest["status"] == "cleaned"}
    if supplied_confirmation != token or not traffic_stopped or in_flight not in {"drain", "abandon"}:
        raise fail("cleanup_confirmation_and_inflight_decision_required")
    for role in live_roles:
        cli.run(["role", "assignment", "delete", "--ids", role["id"]], "cleanup_role_delete", mutation=True, empty=True)
    if graph:
        for grant in list(graph["grants"]):
            cli.rest("delete", GRAPH + "/servicePrincipals/" + graph["service_principal_id"] + "/appRoleAssignedTo/" + grant["id"],
                     "cleanup_graph_grant", empty=True)
            graph["grants"].remove(grant)
            write_manifest(path, manifest, repo)
        cli.rest("delete", GRAPH + "/servicePrincipals/" + graph["service_principal_id"], "cleanup_service_principal", empty=True)
        cli.rest("delete", GRAPH + "/applications/" + graph["application_id"], "cleanup_application", empty=True)
    if group_id(manifest["target"]) in identifiers:
        cli.run(["group", "delete", "--name", manifest["resource_group"], "--yes"], "cleanup_group_delete", mutation=True, empty=True)
        if cli.run(["group", "exists", "--name", manifest["resource_group"]], "cleanup_group_verify") is not False:
            raise fail("group_deletion_not_complete")
    manifest["status"], manifest["updated_at"] = "cleaned", timestamp()
    write_manifest(path, manifest, repo)
    return report | {"status": "cleaned"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="advisor-deploy-operations")
    parser.add_argument("operation", choices=["preflight", "deploy", "identity", "set-mode", "cleanup", "bench-deploy", "bench-control", "bench-close", "reconcile", "network-deploy"])
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--private-directory", type=Path, required=True)
    parser.add_argument("--parameters-file", type=Path)
    parser.add_argument("--outputs-file", type=Path)
    parser.add_argument("--target", choices=["aca", "functions"])
    parser.add_argument("--mode", choices=["WhatIf", "Apply"], default="WhatIf")
    parser.add_argument("--confirmation")
    parser.add_argument("--deployment-name")
    parser.add_argument("--capacity-evidence-hash")
    parser.add_argument("--bootstrap-only", action="store_true")
    parser.add_argument("--stage", choices=["Register", "Grant"], default="Register")
    parser.add_argument("--scale", choices=["Resident", "ScaleToZero"], default="ScaleToZero")
    parser.add_argument("--traffic-stopped", action="store_true")
    parser.add_argument("--in-flight", choices=["drain", "abandon"])
    parser.add_argument("--bench-stage", choices=["Bootstrap", "Publish", "App"], default="Bootstrap")
    parser.add_argument("--bench-action", choices=["start", "status"], default="status")
    args = parser.parse_args(argv)
    try:
        if args.bootstrap_only and (args.operation != "deploy" or args.target != "functions"):
            raise fail("bootstrap_only_requires_functions_deploy")
        repo = args.repository.resolve()
        private = PrivateFiles(args.private_directory, repo)
        cli = Cli("WhatIf" if args.operation == "preflight" else args.mode, repo, private)
        if args.operation == "bench-close":
            if args.parameters_file is None:
                raise fail("closure_plan_required")
            from advisor_deploy.benchmark_close import close_benchmark
            report = close_benchmark(args.parameters_file.absolute(), repo, cli, private, args.confirmation)
        elif args.operation == "network-deploy":
            if args.parameters_file is None:
                raise fail("network_plan_required")
            from advisor_deploy.network_deploy import deploy_network
            report = deploy_network(args.parameters_file.absolute(), repo, cli, private, args.confirmation)
        elif args.operation in {"bench-deploy", "bench-control"}:
            if args.parameters_file is None:
                raise fail("benchmark_deployment_plan_required")
            from advisor_deploy.benchmark_deploy import control_benchmark, deploy_benchmark
            if args.operation == "bench-deploy":
                report = deploy_benchmark(args.parameters_file.absolute(), repo, cli, private,
                                          args.bench_stage, args.confirmation)
            else:
                report = control_benchmark(args.parameters_file.absolute(), repo, cli,
                                           args.bench_action, args.confirmation)
        elif args.operation in {"preflight", "deploy"}:
            if args.parameters_file is None or args.target is None:
                raise fail("plan_and_target_required")
            plan = load_plan(args.parameters_file.absolute(), repo, args.target)
            if args.operation == "preflight":
                report, _ = preflight(plan, cli, approved_evidence=args.capacity_evidence_hash)
            else:
                report = deploy(plan, cli, private, args.confirmation, args.capacity_evidence_hash,
                                bootstrap_only=args.bootstrap_only)
        elif args.operation == "identity":
            if args.parameters_file is None:
                raise fail("benchmark_plan_required")
            report = ensure_benchmark(args.parameters_file.absolute(), repo, cli, args.stage, args.confirmation)
        else:
            if args.outputs_file is None:
                raise fail("ownership_manifest_required")
            if args.operation == "set-mode":
                report = set_mode(args.outputs_file.absolute(), repo, cli, args.scale, args.confirmation)
            elif args.operation == "reconcile":
                report = reconcile_receipts(args.outputs_file.absolute(), repo, cli, args.deployment_name, args.confirmation)
            else:
                report = cleanup(args.outputs_file.absolute(), repo, cli, args.confirmation, args.traffic_stopped, args.in_flight)
        print(json.dumps(report, sort_keys=True))
        return 0
    except CliFailure as error:
        report = {"ok": False, "error": {"phase": error.phase, "code": error.code, "exit_code": error.exit_code}}
        if getattr(error, "receipt_deployment", None):
            report["receipt_deployment"] = error.receipt_deployment
        if getattr(error, "recovery_errors", None):
            report["recovery_errors"] = error.recovery_errors
    except Exception as error:
        message = str(error)
        code = message.removeprefix("deployment guard: ") if message.startswith("deployment guard: ") else "configuration_or_artifact_rejected"
        report = {"ok": False, "error": {"phase": "guard", "code": code if re.fullmatch(r"[A-Za-z0-9_ :.-]{1,160}", code) else "invalid_input"}}
        if getattr(error, "recovery_errors", None):
            report["recovery_errors"] = error.recovery_errors
    print(json.dumps(report, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

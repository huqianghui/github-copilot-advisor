"""Strict, non-secret ownership records. A record is not permission to widen scope."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
import json
import os
import re

from advisor_deploy.config import SUBSCRIPTION, _resource_id
from advisor_deploy.paths import has_link


GROUPS = {"aca": "rg-copilot-advisor-aca-exp", "functions": "rg-copilot-advisor-flex-exp", "benchmark": "rg-copilot-advisor-aca-exp"}
ROLES = {
    "openai": "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
    "search": "1407120a-92aa-4202-b7e9-c0e197c71c8f",
    "blob": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
    "blob_owner": "b7e6dc6d-f1e8-4753-8033-0f276bb0955b",
    "queue": "974c5e8b-45b9-4653-ba55-5f855dd0fb88",
    "metrics": "3913510d-42f4-4e42-8a64-420c390055eb",
    "pull": "7f951dda-4ed3-4680-a7ca-43fe172d538d",
}
RESOURCE_TYPES = {
    "microsoft.app/containerapps", "microsoft.app/managedenvironments",
    "microsoft.storage/storageaccounts", "microsoft.managedidentity/userassignedidentities",
    "microsoft.containerregistry/registries", "microsoft.operationalinsights/workspaces",
    "microsoft.insights/components", "microsoft.web/sites", "microsoft.web/serverfarms",
    "microsoft.botservice/botservices",
}
OUTPUT_FIELDS = {
    "target", "resource_group", "app_id", "app_name", "endpoint", "storage_id", "blob_url", "queue_url",
    "identity_id", "identity_client_id", "identity_principal_id", "bot_id", "created_role_assignment_ids",
}


def fail(code: str) -> ValueError:
    return ValueError("deployment guard: " + code)


def guid(value: object) -> str:
    if not isinstance(value, str):
        raise fail("invalid_identifier")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise fail("invalid_identifier") from None
    if canonical != value.lower():
        raise fail("invalid_identifier")
    return canonical


def group_id(target: str) -> str:
    if target not in GROUPS:
        raise fail("invalid_target")
    return f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUPS[target]}"


def is_provider_auxiliary(identifier: object) -> bool:
    if not isinstance(identifier, str):
        return False
    prefix, _, name = identifier.rpartition("/")
    return (prefix.lower() == (group_id("functions") + "/providers/microsoft.insights/actiongroups").lower()
            and name == "Application Insights Smart Detection")


def arm_parts(identifier: object) -> list[str]:
    if not isinstance(identifier, str) or (
        not re.fullmatch(r"/[A-Za-z0-9_./()-]+", identifier) and not is_provider_auxiliary(identifier)
    ):
        raise fail("invalid_resource_id")
    parts = identifier.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]) or len(parts) < 5:
        raise fail("invalid_resource_id")
    if parts[1].lower() != "subscriptions" or parts[2].lower() != SUBSCRIPTION or parts[3].lower() != "resourcegroups":
        raise fail("outside_approved_subscription")
    if len(parts) != 5 and (len(parts) < 9 or parts[5].lower() != "providers" or len(parts) % 2 != 1):
        raise fail("invalid_resource_id")
    return parts


def experiment_resource(identifier: object) -> str:
    parts = arm_parts(identifier)
    if parts[4].lower() not in {value.lower() for value in GROUPS.values()}:
        raise fail("outside experiment ownership")
    if is_provider_auxiliary(identifier):
        return str(identifier)
    if len(parts) > 5:
        resource_type = (parts[6] + "/" + parts[7]).lower()
        if resource_type not in RESOURCE_TYPES or "microsoft.authorization" in identifier.lower():
            raise fail("resource_type_not_owned")
    return str(identifier)


def validate_role(role: object, owned_ids: list[str]) -> dict:
    required = {"id", "scope", "principal_id", "role_definition_id", "identity_id"}
    if not isinstance(role, dict) or set(role) != required:
        raise fail("invalid_role_record")
    guid(role["principal_id"])
    identity = experiment_resource(role["identity_id"])
    if "/providers/microsoft.managedidentity/userassignedidentities/" not in identity.lower():
        raise fail("role_identity_type")
    if identity.lower() not in {value.lower() for value in owned_ids}:
        raise fail("role_identity_not_owned")
    scope = role["scope"]
    parts = arm_parts(scope)
    definition = role["role_definition_id"]
    match = re.fullmatch(
        rf"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/([a-f0-9-]{{36}})",
        definition, re.IGNORECASE,
    ) if isinstance(definition, str) else None
    if not match or match[1].lower() not in ROLES.values():
        raise fail("role_not_allowlisted")
    expected = scope + "/providers/Microsoft.Authorization/roleAssignments/"
    if not isinstance(role["id"], str) or not role["id"].lower().startswith(expected.lower()):
        raise fail("role_scope_mismatch")
    guid(role["id"][len(expected):])
    resource_type = (parts[6] + "/" + parts[7]).lower() if len(parts) >= 9 else ""
    role_id = match[1].lower()
    if resource_type == "microsoft.cognitiveservices/accounts":
        allowed = {ROLES["openai"]}
    elif resource_type == "microsoft.search/searchservices":
        allowed = {ROLES["search"]}
    else:
        experiment_resource(scope)
        allowed = {
            "microsoft.storage/storageaccounts": {ROLES["blob"], ROLES["blob_owner"], ROLES["queue"]},
            "microsoft.containerregistry/registries": {ROLES["pull"]},
            "microsoft.insights/components": {ROLES["metrics"]},
        }.get(resource_type, set())
    if role_id not in allowed:
        raise fail("role_scope_type_mismatch")
    return role


def validate_cleanup_scope(requested_ids: list[str], owned_ids: list[str], role_assignments: list[dict] | None = None) -> list[str]:
    if not isinstance(requested_ids, list) or not isinstance(owned_ids, list) or any(
        not isinstance(value, str) for value in requested_ids + owned_ids
    ):
        raise fail("invalid_cleanup_request")
    for identifier in owned_ids:
        experiment_resource(identifier)
    roles = [validate_role(role, owned_ids) for role in role_assignments or []]
    allowed = {value.lower() for value in owned_ids} | {role["id"].lower() for role in roles}
    if len({value.lower() for value in requested_ids}) != len(requested_ids):
        raise fail("duplicate_cleanup_request")
    for identifier in requested_ids:
        if not isinstance(identifier, str) or identifier.lower() not in allowed:
            raise fail("outside experiment ownership")
    return list(requested_ids)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_manifest(target: str, run_id: str, environment_name: str, artifact_digest: str, config_digest: str) -> dict:
    result = {
        "format": "advisor-deployment-v1", "target": target, "run_id": guid(run_id),
        "subscription_id": SUBSCRIPTION, "tenant_id": "", "resource_group": GROUPS.get(target),
        "environment_name": environment_name, "status": "planned",
        "artifact_digest": artifact_digest, "config_digest": config_digest,
        "owned_resource_ids": [], "role_assignments": [], "pending_role_assignment_ids": [], "outputs": {},
        "discovery_complete": True, "image_reference": None,
        "dependencies": {}, "summary": {}, "graph": None, "last_control_action": None,
        "application_validation": "not_run", "created_at": timestamp(), "updated_at": timestamp(),
    }
    return validate_manifest(result)


def validate_manifest(data: object) -> dict:
    try:
        return _validate_manifest(data)
    except (TypeError, AttributeError, KeyError, IndexError):
        raise fail("invalid_manifest_field_type") from None


def _validate_manifest(data: object) -> dict:
    fields = {
        "format", "target", "run_id", "subscription_id", "tenant_id", "resource_group", "environment_name",
        "status", "artifact_digest", "config_digest", "owned_resource_ids", "role_assignments", "pending_role_assignment_ids", "outputs",
        "discovery_complete", "image_reference",
        "dependencies", "summary", "graph", "last_control_action", "application_validation", "created_at", "updated_at",
    }
    if not isinstance(data, dict) or set(data) != fields or data["format"] != "advisor-deployment-v1":
        raise fail("invalid_manifest")
    if not isinstance(data["target"], str) or data["target"] not in GROUPS or data["subscription_id"] != SUBSCRIPTION or data["resource_group"] != GROUPS[data["target"]]:
        raise fail("manifest_scope_mismatch")
    guid(data["run_id"])
    if data["tenant_id"]:
        guid(data["tenant_id"])
    if not isinstance(data["environment_name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", data["environment_name"]):
        raise fail("invalid_environment")
    if data["status"] not in {"planned", "blocked", "provisioning", "infrastructure_ready", "publishing", "deployed", "failed", "cleaned"}:
        raise fail("invalid_manifest_status")
    for field in ("artifact_digest", "config_digest"):
        if not isinstance(data[field], str) or not re.fullmatch(r"[a-f0-9]{64}", data[field]):
            raise fail("invalid_digest")
    for field in ("created_at", "updated_at"):
        try:
            if datetime.fromisoformat(data[field]).utcoffset().total_seconds() != 0:
                raise fail("invalid_timestamp")
        except (ValueError, TypeError, AttributeError):
            raise fail("invalid_timestamp") from None
    owned = data["owned_resource_ids"]
    if not isinstance(owned, list) or len(owned) > 250 or any(not isinstance(value, str) for value in owned):
        raise fail("invalid_owned_resources")
    if len(set(value.lower() for value in owned)) != len(owned):
        raise fail("duplicate_owned_resource")
    for identifier in owned:
        experiment_resource(identifier)
        if arm_parts(identifier)[4].lower() != data["resource_group"].lower():
            raise fail("manifest_group_mismatch")
    if not isinstance(data["role_assignments"], list) or len(data["role_assignments"]) > 100:
        raise fail("invalid_roles")
    for role in data["role_assignments"]:
        validate_role(role, owned)
    pending = data["pending_role_assignment_ids"]
    if not isinstance(pending, list) or len(pending) > 100:
        raise fail("invalid_pending_roles")
    for identifier in pending:
        parts = arm_parts(identifier)
        if len(parts) < 13 or [part.lower() for part in parts[-4:-1]] != ["providers", "microsoft.authorization", "roleassignments"]:
            raise fail("invalid_pending_role")
        guid(parts[-1])
    dependencies = data["dependencies"]
    if not isinstance(dependencies, dict) or set(dependencies) - {"openai", "search"}:
        raise fail("invalid_dependencies")
    if not isinstance(data["summary"], dict):
        raise fail("invalid_summary")
    for kind, identifier in dependencies.items():
        expected = {"openai": "Microsoft.CognitiveServices/accounts", "search": "Microsoft.Search/searchServices"}[kind]
        if not isinstance(identifier, str):
            raise fail("invalid_dependencies")
        try:
            _resource_id(identifier, expected, "dependency",
                         auth_mode=data["summary"].get(kind + "_auth", "entra"))
        except ValueError:
            raise fail("invalid_dependencies") from None
        if identifier.split("/")[4].lower() in {g.lower() for g in GROUPS.values()}:
            raise fail("dependency_must_remain_outside_experiment")
    if not isinstance(data["summary"], dict) or set(data["summary"]) - {
        "location", "profile", "memory_mb", "max_instances", "worker_parallelism", "min_replicas", "openai_auth", "search_auth",
    } or any(type(value) not in {str, int, bool} for value in data["summary"].values()):
        raise fail("invalid_summary")
    outputs = data["outputs"]
    if not isinstance(outputs, dict) or (outputs and set(outputs) != OUTPUT_FIELDS):
        raise fail("invalid_outputs")
    if outputs:
        if outputs["target"] != data["target"] or outputs["resource_group"] != data["resource_group"]:
            raise fail("output_scope_mismatch")
        for field in ("app_id", "storage_id", "identity_id", "bot_id"):
            if outputs[field] and outputs[field].lower() not in {value.lower() for value in owned}:
                raise fail("output_not_owned")
        for field in ("identity_client_id", "identity_principal_id"):
            guid(outputs[field])
        for field in ("endpoint", "blob_url", "queue_url"):
            from advisor_deploy.config import _endpoint
            if outputs[field]:
                _endpoint(outputs[field], "output endpoint")
        if not set(outputs["created_role_assignment_ids"]) <= {role["id"] for role in data["role_assignments"]} | set(pending):
            raise fail("output_role_mismatch")
    if data["application_validation"] != "not_run":
        raise fail("application_acceptance_is_not_this_scripts_claim")
    if type(data["discovery_complete"]) is not bool:
        raise fail("invalid_discovery_state")
    if data["image_reference"] is not None and (
        data["target"] != "aca" or not isinstance(data["image_reference"], str)
        or not re.fullmatch(r"[a-z0-9]+\.azurecr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}", data["image_reference"])
    ):
        raise fail("invalid_image_reference")
    graph = data["graph"]
    if graph is not None:
        if data["target"] != "benchmark" or not isinstance(graph, dict) or set(graph) != {
            "application_id", "client_id", "service_principal_id", "role_id", "grants",
        }:
            raise fail("invalid_graph_ownership")
        for field in ("application_id", "client_id", "service_principal_id", "role_id"):
            if graph[field]:
                guid(graph[field])
        if not isinstance(graph["grants"], list) or len(graph["grants"]) > 3:
            raise fail("invalid_graph_grants")
        for grant in graph["grants"]:
            if not isinstance(grant, dict) or set(grant) != {"id", "principal_id", "resource_id", "role_id", "identity_id"}:
                raise fail("invalid_graph_grant")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", grant["id"]):
                raise fail("invalid_graph_grant_id")
            guid(grant["principal_id"])
            if grant["resource_id"] != graph["service_principal_id"] or grant["role_id"] != graph["role_id"]:
                raise fail("foreign_graph_grant")
            experiment_resource(grant["identity_id"])
    action = data["last_control_action"]
    if action is not None and (
        not isinstance(action, dict) or set(action) != {"at", "kind", "min_replicas"}
        or action["kind"] != "scale-mode" or action["min_replicas"] not in {0, 1}
    ):
        raise fail("invalid_control_action")
    return data


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise fail("duplicate_json_field")
            result[key] = value
        return result
    try:
        if has_link(path.absolute()) or path.stat().st_size > 4 * 1024 * 1024:
            raise fail("unsafe_json_file")
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
        if not isinstance(data, dict):
            raise fail("json_object_required")
        return data
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise fail("unreadable_json") from None


def load_manifest(path: Path) -> dict:
    data = validate_manifest(read_json(path))
    if any(is_provider_auxiliary(value) for value in data["owned_resource_ids"]):
        from advisor_deploy.provider_auxiliary import validate_saved_provenance
        validate_saved_provenance(path, data)
    return data


def write_manifest(path: Path, data: dict, repo: Path) -> None:
    validate_manifest(data)
    path = path.absolute()
    if has_link(path) or not path.is_relative_to(repo / "deploy" / "out") or path.suffix != ".json":
        raise fail("manifest_output_scope")
    if any(is_provider_auxiliary(value) for value in data["owned_resource_ids"]):
        from advisor_deploy.provider_auxiliary import validate_saved_provenance
        validate_saved_provenance(path, data)
    if path.exists():
        previous = load_manifest(path)
        if (previous["run_id"], previous["target"]) != (data["run_id"], data["target"]):
            raise fail("manifest_ownership_collision")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".next")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

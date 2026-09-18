"""Provenance for one Microsoft-generated Flex Smart Detection action group."""

from datetime import datetime, timezone
import json
import os
from uuid import uuid4

from advisor_deploy.config import SUBSCRIPTION, TENANT
from advisor_deploy.manifest import (
    GROUPS, arm_parts, fail, group_id, guid, is_provider_auxiliary, read_json,
)
from advisor_deploy.paths import has_link


SMART_ALERTS_APP = "3af5a1e8-2459-45cb-8683-bcd6cccbcc13"
MICROSOFT_OWNER = "f8cdef31-a31e-4b4a-93e4-5f571e91255a"
WRITE_OPERATION = "microsoft.insights/actiongroups/write"
MAX_EVENTS = 1000


def provenance_path(manifest_path):
    return manifest_path.with_suffix(".provider-auxiliary.json")


def _utc(value):
    try:
        if not isinstance(value, str) or len(value) > 50:
            raise ValueError()
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise fail("provider_aux_timestamp_unverified") from None


def _component(identifier):
    parts = arm_parts(identifier)
    return (len(parts) == 9 and parts[4].lower() == GROUPS["functions"]
            and [value.lower() for value in parts[6:8]] == ["microsoft.insights", "components"])


def _validate_provenance(proof, manifest):
    fields = {"format", "run_id", "subscription_id", "tenant_id", "resource_group", "ledger_created_at",
              "resource_id", "component_ids", "metadata", "activity", "caller"}
    if not isinstance(proof, dict) or set(proof) != fields or proof["format"] != "advisor-provider-auxiliary-v1":
        raise fail("provider_aux_provenance_invalid")
    if (manifest["target"] != "functions" or manifest["resource_group"] != GROUPS["functions"]
            or manifest["subscription_id"] != SUBSCRIPTION or manifest["tenant_id"] != TENANT
            or any(proof[name] != manifest[name] for name in ("run_id", "subscription_id", "tenant_id", "resource_group"))
            or proof["ledger_created_at"] != manifest["created_at"] or not is_provider_auxiliary(proof["resource_id"])):
        raise fail("provider_aux_provenance_scope")
    owned = {value.lower() for value in manifest["owned_resource_ids"]}
    if (group_id("functions").lower() not in owned or not isinstance(proof["component_ids"], list)
            or len(proof["component_ids"]) != 1
            or any(not isinstance(value, str) or value.lower() not in owned or not _component(value)
                   for value in proof["component_ids"])):
        raise fail("provider_aux_component_unverified")
    if (proof["metadata"] != {"location": "global", "group_short_name": "SmartDetect", "enabled": True}
            or proof["metadata"].get("enabled") is not True):
        raise fail("provider_aux_metadata_unverified")
    activity, caller = proof["activity"], proof["caller"]
    if (not isinstance(activity, dict) or set(activity) != {
        "event_id", "correlation_id", "timestamp", "resource_id", "caller_id", "operation", "status",
    } or not isinstance(caller, dict) or set(caller) != {"object_id", "app_id", "owner_tenant_id"}):
        raise fail("provider_aux_activity_unverified")
    if (activity["operation"] != WRITE_OPERATION or activity["status"] != "Succeeded"
            or activity["resource_id"].lower() != proof["resource_id"].lower()
            or not _utc(manifest["created_at"]) < _utc(activity["timestamp"]) <= datetime.now(timezone.utc)):
        raise fail("provider_aux_activity_unverified")
    for value in (activity["event_id"], activity["correlation_id"], activity["caller_id"], caller["object_id"]):
        guid(value)
    if (caller["object_id"] != activity["caller_id"] or caller["app_id"] != SMART_ALERTS_APP
            or caller["owner_tenant_id"] != MICROSOFT_OWNER):
        raise fail("provider_aux_caller_unverified")
    return proof


def validate_saved_provenance(manifest_path, manifest):
    try:
        proof = _validate_provenance(read_json(provenance_path(manifest_path)), manifest)
        resources = [value for value in manifest["owned_resource_ids"] if is_provider_auxiliary(value)]
        if len(resources) != 1 or resources[0].lower() != proof["resource_id"].lower():
            raise fail("provider_aux_provenance_scope")
        return proof
    except (ValueError, KeyError, TypeError, AttributeError):
        raise fail("provider_aux_saved_provenance_unverified") from None


def prove_auxiliary(cli, manifest, receipt_ids, identifier):
    owned = {value.lower() for value in manifest["owned_resource_ids"]}
    receipts = {value.lower() for value in receipt_ids}
    if (manifest["target"] != "functions" or manifest["resource_group"] != GROUPS["functions"]
            or manifest["tenant_id"] != TENANT or not is_provider_auxiliary(identifier)
            or group_id("functions").lower() not in owned or group_id("functions").lower() not in receipts):
        raise fail("provider_aux_fresh_group_unverified")
    components = sorted(value for value in receipt_ids if value.lower() in owned and _component(value))
    if len(components) != 1:
        raise fail("provider_aux_component_unverified")
    metadata = cli.run([
        "resource", "show", "--ids", identifier, "--api-version", "2023-01-01", "--query",
        "{id:id,type:type,location:location,group_short_name:properties.groupShortName,enabled:properties.enabled}",
    ], "provider_aux_resource")
    if (not isinstance(metadata, dict) or str(metadata.get("id", "")).lower() != identifier.lower()
            or str(metadata.get("type", "")).lower() != "microsoft.insights/actiongroups"
            or str(metadata.get("location", "")).lower() != "global"
            or metadata.get("group_short_name") != "SmartDetect" or metadata.get("enabled") is not True):
        raise fail("provider_aux_metadata_unverified")
    now = datetime.now(timezone.utc)
    created = _utc(manifest["created_at"])
    events = cli.run([
        "monitor", "activity-log", "list", "--resource-id", identifier, "--start-time", manifest["created_at"],
        "--end-time", now.isoformat(), "--max-events", str(MAX_EVENTS), "--query",
        "[].{event_id:eventDataId,correlation_id:correlationId,timestamp:eventTimestamp,resource_id:resourceId,"
        "caller:caller,operation:operationName.value,status:status.value}",
    ], "provider_aux_activity")
    if not isinstance(events, list) or len(events) >= MAX_EVENTS:
        raise fail("provider_aux_activity_incomplete")
    candidates = []
    for event in events:
        if not isinstance(event, dict):
            raise fail("provider_aux_activity_unverified")
        if (str(event.get("resource_id", "")).lower() == identifier.lower()
                and str(event.get("operation", "")).lower() == WRITE_OPERATION and event.get("status") == "Succeeded"):
            instant = _utc(event.get("timestamp"))
            if created < instant <= now:
                candidates.append((instant, event))
    if not candidates:
        raise fail("provider_aux_activity_missing")
    # The earliest successful write in the complete creation window determines
    # origin; a later provider update cannot excuse an earlier unrelated caller.
    event = min(candidates, key=lambda value: (value[0], str(value[1].get("event_id"))))[1]
    caller_id = guid(event.get("caller"))
    principal = cli.rest(
        "get", "https://graph.microsoft.com/v1.0/servicePrincipals/" + caller_id +
        "?$select=id,appId,appOwnerOrganizationId", "provider_aux_caller",
    )
    if (not isinstance(principal, dict) or str(principal.get("id", "")).lower() != caller_id
            or str(principal.get("appId", "")).lower() != SMART_ALERTS_APP
            or str(principal.get("appOwnerOrganizationId", "")).lower() != MICROSOFT_OWNER):
        raise fail("provider_aux_caller_unverified")
    proof = {
        "format": "advisor-provider-auxiliary-v1", "run_id": manifest["run_id"],
        "subscription_id": manifest["subscription_id"], "tenant_id": manifest["tenant_id"],
        "resource_group": manifest["resource_group"], "ledger_created_at": manifest["created_at"],
        "resource_id": identifier, "component_ids": components,
        "metadata": {"location": "global", "group_short_name": "SmartDetect", "enabled": True},
        "activity": {"event_id": guid(event.get("event_id")), "correlation_id": guid(event.get("correlation_id")),
                     "timestamp": event["timestamp"], "resource_id": identifier, "caller_id": caller_id,
                     "operation": WRITE_OPERATION, "status": "Succeeded"},
        "caller": {"object_id": caller_id, "app_id": SMART_ALERTS_APP, "owner_tenant_id": MICROSOFT_OWNER},
    }
    return _validate_provenance(proof, manifest)


def save_provenance(manifest_path, manifest, proof, repo):
    path = provenance_path(manifest_path).absolute()
    if has_link(path) or ".." in path.parts or not path.is_relative_to(repo / "deploy" / "out"):
        raise fail("provider_aux_provenance_path")
    _validate_provenance(proof, manifest)
    if path.exists():
        if read_json(path) != proof:
            raise fail("provider_aux_provenance_collision")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid4().hex + ".next")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(proof, stream, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)

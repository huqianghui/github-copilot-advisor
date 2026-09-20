"""Reversible, quiescence-gated closure; never deletes or starts a workload."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from advisor_deploy.manifest import (
    arm_parts, fail, group_id, load_manifest, read_json, timestamp,
)
from advisor_deploy.paths import has_link
from advisor_deploy.render import digest


ARM = "https://management.azure.com"
API = "2025-01-01"


class _Budget:
    def __init__(self, cli):
        self.cli, self.end = cli, None

    def remaining(self):
        value = 120 if self.end is None else self.end - time.monotonic()
        if value <= 0:
            raise fail("closure_timeout")
        return min(120, value)

    def run(self, args, phase, **options):
        return self.cli.run(args, phase, timeout=self.remaining(), **options)

    def get(self, url, phase):
        return self.cli.rest("get", url, phase, timeout=self.remaining())

    def pause(self):
        time.sleep(min(5, self.remaining()))


def _load(path, repo, cli):
    from advisor_deploy.bench.build import verify_context
    from advisor_deploy.remote_acr import evidence_path, verify_evidence
    data = read_json(path)
    if set(data) != {"format", "aca_manifest", "observer_state", "observer_context"} or data["format"] != "advisor-benchmark-close-v1":
        raise fail("closure_plan_fields")
    paths = {}
    for key in ("aca_manifest", "observer_state", "observer_context"):
        if not isinstance(data[key], str):
            raise fail("closure_path")
        joined = (path.parent / data[key]).absolute()
        if has_link(joined):
            raise fail("closure_path")
        paths[key] = joined.resolve()
        if not paths[key].is_relative_to(repo / "deploy" / "out"):
            raise fail("closure_path")
    aca = load_manifest(paths["aca_manifest"])
    state = read_json(paths["observer_state"])
    owned = {identifier.lower() for identifier in aca["owned_resource_ids"]}
    if (aca["target"] != "aca" or not aca["discovery_complete"] or aca["pending_role_assignment_ids"]
            or state.get("format") != "advisor-benchmark-deployment-v1" or state.get("run_id") != aca["run_id"]
            or state.get("phase") not in {"app_deployed", "suspended"}
            or state.get("planned_app_id", "").lower() not in owned
            or state.get("identity_id", "").lower() not in owned
            or state["planned_app_id"] == aca["outputs"]["app_id"]
            or not state.get("planned_app_name", "").startswith("ca-bench-")
            or state["planned_app_id"].lower() != (group_id("aca") + "/providers/Microsoft.App/containerApps/" + state["planned_app_name"]).lower()
            or not {v.lower() for v in state.get("resource_ids", [])} <= owned
            or not set(state.get("role_assignment_ids", [])) <= {r["id"] for r in aca["role_assignments"]}):
        raise fail("closure_ownership_mismatch")
    context = verify_context(paths["observer_context"])
    run = read_json(paths["observer_context"] / "run.json")
    if (digest(context) != state["context_digest"] or digest(run) != state["run_plan_digest"]
            or run.get("expected_target_id") != aca["outputs"]["app_id"]
            or run.get("deployment", {}).get("artifact_sha256") != aca["artifact_digest"]
            or run["deployment"].get("config_sha256") != aca["config_digest"]):
        raise fail("closure_deployed_provenance_mismatch")
    registries = [v for v in aca["owned_resource_ids"] if "/providers/microsoft.containerregistry/registries/" in v.lower()
                  and len(arm_parts(v)) == 9]
    if len(registries) != 1:
        raise fail("closure_registry_ambiguous")
    # Historical sealed artifacts can authorize shutdown, never a new workload.
    # verify_context still verifies its locked files/wheels and historical plan.
    verify_evidence(cli, evidence_path(paths["observer_state"]), kind="bench", files=context["files"],
                    binding={"context_sha256": digest(context), "run_plan_sha256": digest(run),
                             "source_sha256": digest(context["source_sha256"]), "lock_sha256": context["lock_sha256"]},
                    image_reference=state["image_reference"], image_id=state["image_id"],
                    registry=registries[0].rsplit("/", 1)[1])
    return paths, aca, state


def _env(template, name):
    containers = template.get("containers", [])
    if len(containers) != 1:
        raise fail("closure_single_container_required")
    entries = containers[0].get("env", [])
    if len({v.get("name") for v in entries}) != len(entries):
        raise fail("closure_duplicate_environment")
    values = [v for v in entries if v.get("name") == name]
    if len(values) != 1 or "secretRef" in values[0] or not isinstance(values[0].get("value"), str):
        raise fail("closure_literal_flag_required")
    return values[0]


def _stable(app):
    properties = app.get("properties", {})
    configuration = deepcopy(properties.get("configuration", {}))
    if configuration.get("ingress") is None:
        configuration.pop("ingress", None)
    if isinstance(configuration.get("secrets"), list):
        configuration["secrets"].sort(key=lambda v: v["name"])
    template = deepcopy(properties.get("template", {}))
    # Azure assigns a new revision suffix for a revision-scope update.
    template.pop("revisionSuffix", None)
    for container in template.get("containers", []):
        if isinstance(container.get("env"), list):
            container["env"].sort(key=lambda v: v["name"])
    return {"identity": app.get("identity"), "configuration": configuration, "template": template,
            "environmentId": properties.get("environmentId", properties.get("managedEnvironmentId")),
            "workloadProfileName": properties.get("workloadProfileName"), "tags": app.get("tags")}


def _revision_matches(app, revision):
    expected = _stable({"properties": {"template": app}})["template"]
    actual = _stable({"properties": {"template": revision}})["template"]
    if expected.get("customMetricsSettings") is None:
        expected.pop("customMetricsSettings", None)
        if actual.get("customMetricsSettings") is None:
            actual.pop("customMetricsSettings", None)
    if len(expected.get("containers", [])) != len(actual.get("containers", [])):
        return False
    for left, right in zip(expected.get("containers", []), actual.get("containers", [])):
        if "imageType" not in right and "imageType" in left:
            right["imageType"] = left["imageType"]
        if "ephemeralStorage" not in right.get("resources", {}) and "ephemeralStorage" in left.get("resources", {}):
            right.setdefault("resources", {})["ephemeralStorage"] = left["resources"]["ephemeralStorage"]
        if "probes" not in left:
            right.pop("probes", None)
    for key in ("cooldownPeriod", "pollingInterval"):
        if actual.get("scale", {}).get(key) is None:
            if key in expected.get("scale", {}):
                actual.setdefault("scale", {})[key] = expected["scale"][key]
            else:
                actual.get("scale", {}).pop(key, None)
    return actual == expected


def _app(budget, identifier):
    parts = arm_parts(identifier)
    if len(parts) != 9 or [p.lower() for p in parts[6:8]] != ["microsoft.app", "containerapps"]:
        raise fail("closure_app_scope")
    value = budget.run(["containerapp", "show", "--resource-group", parts[4], "--name", parts[8]], "closure_app_readback")
    if value.get("id", "").lower() != identifier.lower():
        raise fail("closure_app_identity_mismatch")
    return value


def _pages(budget, url, app_id):
    values, pages, seen = [], [], set()
    for _ in range(16):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "management.azure.com"
                or not parsed.path.lower().startswith(app_id.lower() + "/revisions") or url in seen):
            raise fail("closure_pagination_scope")
        seen.add(url)
        response = budget.get(url, "closure_revision_evidence")
        if not isinstance(response, dict) or not isinstance(response.get("value"), list):
            raise fail("closure_replica_evidence_incomplete")
        values.extend(response["value"])
        pages.append(response)
        if not response.get("nextLink"):
            return values, pages
        url = response["nextLink"]
        if not isinstance(url, str):
            raise fail("closure_replica_evidence_incomplete")
    raise fail("closure_pagination_limit")


def _snapshot(budget, app_id, *, also=()):
    revisions, _ = _pages(budget, ARM + app_id + "/revisions?api-version=" + API, app_id)
    active, replicas, inactive_quiescent = [], [], True
    for revision in revisions:
        identifier, name, props = revision.get("id", ""), revision.get("name", ""), revision.get("properties", {})
        if (not identifier.lower().startswith(app_id.lower() + "/revisions/")
                or identifier.rsplit("/", 1)[1] != name or type(props.get("active")) is not bool):
            raise fail("closure_revision_scope")
        if props["active"]:
            active.append(revision)
        elif props.get("runningState") not in {"Stopped", "ScaledToZero"}:
            inactive_quiescent = False
        if props["active"] or name in also or props.get("runningState") not in {"Stopped", "ScaledToZero"}:
            rows, pages = _pages(budget, ARM + identifier + "/replicas?api-version=" + API, app_id)
            replicas.append({"revision_id": identifier, "replicas": rows, "pages": pages})
    return {"observed_at": timestamp(), "active": active, "replica_lists": replicas,
            "replicas": sum(len(v["replicas"]) for v in replicas), "inactive_quiescent": inactive_quiescent}


def _public_snapshot(snapshot):
    return {
        "observed_at": snapshot["observed_at"], "replicas": snapshot["replicas"],
        "inactive_quiescent": snapshot["inactive_quiescent"],
        "active": [{"id": r["id"], "name": r["name"], "active": r["properties"]["active"],
                    "running_state": r["properties"].get("runningState"),
                    "provisioning_state": r["properties"].get("provisioningState"),
                    "template_sha256": digest(r["properties"].get("template", {}))}
                   for r in snapshot["active"]],
        "replica_lists": [{"revision_id": group["revision_id"], "replicas": [
            {"id": replica.get("id"), "name": replica.get("name"), "containers": [
                {key: container.get(key) for key in ("name", "ready", "started", "restartCount")}
                for container in replica.get("properties", {}).get("containers", [])
            ]} for replica in group["replicas"]]} for group in snapshot["replica_lists"]],
    }


def _quiescence(budget, target_id, observer_id, storage_id):
    samples = []
    for index in range(2):
        target = _snapshot(budget, target_id)
        observer = _snapshot(budget, observer_id)
        queue = budget.get(ARM + storage_id + "/queueServices/default/queues/advisor-incoming?api-version=2023-05-01",
                           "closure_queue_evidence")
        count = queue.get("properties", {}).get("approximateMessageCount")
        if (not target["active"] or target["replicas"] != 0 or not target["inactive_quiescent"]
                or not observer["inactive_quiescent"] or type(count) is not int or count != 0):
            raise fail("closure_target_quiescence_unverified")
        for group in observer["replica_lists"]:
            for replica in group["replicas"]:
                drivers = [v for v in replica.get("properties", {}).get("containers", []) if v.get("name") == "driver-receiver"]
                if len(drivers) != 1 or drivers[0].get("started") is not False or drivers[0].get("ready") is not False:
                    raise fail("closure_observer_quiescence_unverified")
        samples.append({"target": target, "observer": observer, "queue_approximate_count": count})
        if index == 0:
            time.sleep(30)
    for app in ("target", "observer"):
        if {r["id"] for r in samples[0][app]["active"]} != {r["id"] for r in samples[1][app]["active"]}:
            raise fail("closure_revision_changed_during_quiescence")
    return samples


def _probe_access(endpoint, timeout):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = build_opener(NoRedirect())
    statuses = {}
    for name, route in (("benchmark", "/bench/messages"), ("teams", "/api/messages")):
        request = Request(endpoint.rstrip("/") + route, data=b"{}", method="POST",
                          headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=timeout()) as response:
                statuses[name] = response.status
                response.read(1024)
        except HTTPError as error:
            statuses[name] = error.code
            error.close()
    if statuses != {"benchmark": 404, "teams": 401}:
        raise fail("closure_route_readback_unverified")
    return statuses


def close_benchmark(path, repo, cli, private, supplied_confirmation):
    from advisor_deploy.operations import account, assert_owned_group, confirmation, require_permissions
    paths, aca, state = _load(path, repo, cli)
    caller = account(cli)
    if caller["tenantId"] != aca["tenant_id"]:
        raise fail("closure_tenant_mismatch")
    assert_owned_group(cli, aca)
    target_id, observer_id = aca["outputs"]["app_id"], state["planned_app_id"]
    for identifier in (target_id, observer_id):
        require_permissions(cli, identifier, ["Microsoft.App/containerApps/read", "Microsoft.App/containerApps/write"])
    require_permissions(cli, observer_id, ["Microsoft.App/containerApps/revisions/deactivate/action"])
    budget = _Budget(cli)
    target, observer = _app(budget, target_id), _app(budget, observer_id)
    for app, image, identity in (
        (target, aca["image_reference"], aca["outputs"]["identity_id"]),
        (observer, state["image_reference"], state["identity_id"]),
    ):
        props, identities = app["properties"], app.get("identity", {})
        containers = props.get("template", {}).get("containers", [])
        if (len(containers) != 1 or containers[0].get("image") != image or identities.get("type") != "UserAssigned"
                or {v.lower() for v in identities.get("userAssignedIdentities", {})} != {identity.lower()}
                or props.get("configuration", {}).get("activeRevisionsMode") != "Single"):
            raise fail("closure_deployed_app_mismatch")
    if target["properties"]["template"].get("scale", {}).get("minReplicas") != 0:
        raise fail("closure_target_min0_required")
    ingress = target["properties"]["configuration"].get("ingress", {})
    if (ingress.get("external") is not True or ingress.get("allowInsecure") is not False
            or "https://" + ingress.get("fqdn", "") != aca["outputs"]["endpoint"].rstrip("/")):
        raise fail("closure_teams_ingress_mismatch")
    if _env(observer["properties"]["template"], "BENCHMARK_AUTOSTART")["value"].lower() != "false":
        raise fail("closure_observer_autostart_must_be_off")
    flag = _env(target["properties"]["template"], "BENCHMARK_ENABLED")["value"].lower()
    if flag not in {"true", "false"}:
        raise fail("closure_target_flag_invalid")
    quiescence = _quiescence(budget, target_id, observer_id, aca["outputs"]["storage_id"])
    active = quiescence[-1]["observer"]["active"]
    for revision in active:
        if not _revision_matches(observer["properties"]["template"], revision["properties"].get("template", {})):
            raise fail("closure_observer_revision_template_mismatch")
    names = sorted(r["name"] for r in active)
    before_target, before_observer = _stable(target), _stable(observer)
    expected_target, expected_observer = deepcopy(before_target), deepcopy(before_observer)
    _env(expected_target["template"], "BENCHMARK_ENABLED")["value"] = "false"
    expected_observer["configuration"].pop("ingress", None)
    actions = {"target_id": target_id, "target_benchmark_enabled": False, "target_min_replicas": 0,
               "observer_id": observer_id, "observer_ingress_enabled": False,
               "observer_revisions_to_deactivate": names, "observer_autostart": False,
               "resource_deletion": False, "durable_evidence_preserved": True}
    intent = {"actions": actions, "target_before": before_target, "observer_before": before_observer,
              "target_after": expected_target, "observer_after": expected_observer,
              "state": digest(state), "ownership": digest(aca)}
    token = confirmation("BENCH-CLOSE", "aca", aca["run_id"], intent)
    public_quiescence = [{"target": _public_snapshot(v["target"]), "observer": _public_snapshot(v["observer"]),
                          "queue_approximate_count": v["queue_approximate_count"]} for v in quiescence]
    report = {"ok": True, "mode": cli.mode, "confirmation": token, "actions": actions,
              "quiescence_verified": True, "quiescence": public_quiescence, "application_validation": "not_run"}
    if cli.mode == "WhatIf":
        return report
    if supplied_confirmation != token:
        raise fail("closure_confirmation_required")
    if read_json(paths["aca_manifest"]) != aca or read_json(paths["observer_state"]) != state:
        raise fail("closure_ownership_changed")
    record_path = paths["observer_state"].with_suffix(".closure.json")
    if has_link(record_path):
        raise fail("closure_record_link")
    first_started = timestamp()
    previous_record = None
    if record_path.exists():
        previous_record = read_json(record_path)
        if (previous_record.get("format") != "advisor-benchmark-closure-v1"
                or previous_record.get("run_id") != aca["run_id"]
                or previous_record.get("actions", {}).get("target_id") != target_id
                or previous_record.get("actions", {}).get("observer_id") != observer_id):
            raise fail("closure_record_collision")
        first_started = previous_record["started_at"]
    deadline = datetime.fromisoformat(first_started) + timedelta(minutes=20)
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise fail("closure_mutation_window_expired")
    record = {"format": "advisor-benchmark-closure-v1", "run_id": aca["run_id"], "phase": "closing",
              "actions": actions, "confirmation": token, "base_config_digest": aca["config_digest"],
              "target_before_sha256": digest(before_target), "observer_before_sha256": digest(before_observer),
              "quiescence": public_quiescence, "started_at": first_started, "attempt_started_at": timestamp(),
              "mutation_deadline": deadline.isoformat(), "previous_record_sha256": digest(previous_record) if previous_record else None,
              "unresolved_durable_attempts_discarded": False}

    def save():
        private.json(record).replace(record_path)

    budget.end = time.monotonic() + min(1200, remaining)
    save()
    try:
        if observer["properties"]["configuration"].get("ingress") is not None:
            budget.run(["containerapp", "ingress", "disable", "--resource-group", aca["resource_group"],
                        "--name", state["planned_app_name"]], "closure_observer_ingress", mutation=True, empty=True)
        after_observer = _app(budget, observer_id)
        if _stable(after_observer) != expected_observer:
            raise fail("closure_observer_settings_drift")
        if flag != "false":
            budget.run(["containerapp", "update", "--resource-group", aca["resource_group"],
                        "--name", aca["outputs"]["app_name"], "--container-name", target["properties"]["template"]["containers"][0]["name"],
                        "--set-env-vars", "BENCHMARK_ENABLED=false", "--no-wait"],
                       "closure_target_feature", mutation=True, empty=True)
        while True:
            after_target = _app(budget, target_id)
            if after_target["properties"].get("provisioningState") not in {"Updating", "InProgress"}:
                actual = _stable(after_target)
                if actual == before_target and actual != expected_target:
                    budget.pause()
                    continue
                if actual != expected_target:
                    raise fail("closure_target_settings_drift")
                current = _snapshot(budget, target_id)
                if current["active"] and all(
                    _env(r["properties"].get("template", {}), "BENCHMARK_ENABLED")["value"].lower() == "false"
                    for r in current["active"]
                ):
                    break
            budget.pause()
        current = _snapshot(budget, observer_id)
        if sorted(r["name"] for r in current["active"]) != names:
            raise fail("closure_observer_revision_changed")
        for name in names:
            budget.run(["containerapp", "revision", "deactivate", "--resource-group", aca["resource_group"],
                        "--name", state["planned_app_name"], "--revision", name],
                       "closure_revision_deactivate", mutation=True, empty=True)
        record["route_checks"] = _probe_access(aca["outputs"]["endpoint"], budget.remaining)
        while True:
            target_final = _snapshot(budget, target_id)
            observer_final = _snapshot(budget, observer_id, also=names)
            if (not observer_final["active"] and observer_final["replicas"] == 0
                    and observer_final["inactive_quiescent"] and target_final["inactive_quiescent"]):
                break
            budget.pause()
        if (_stable(_app(budget, target_id)) != expected_target
                or _stable(_app(budget, observer_id)) != expected_observer):
            raise fail("closure_final_settings_drift")
        queue = budget.get(ARM + aca["outputs"]["storage_id"] + "/queueServices/default/queues/advisor-incoming?api-version=2023-05-01",
                           "closure_queue_final")
        if queue.get("properties", {}).get("approximateMessageCount") != 0:
            raise fail("closure_final_queue_not_empty")
        state["phase"] = "suspended"
        private.json(state).replace(paths["observer_state"])
        record.update(phase="closed", completed_at=timestamp(), target_after_sha256=digest(expected_target),
                      observer_after_sha256=digest(expected_observer), target_final=_public_snapshot(target_final),
                      observer_final=_public_snapshot(observer_final),
                      queue_approximate_count=0, remaining_reenable_requires_separate_approval=True)
        save()
        return report | {"status": "closed", "target_replicas": target_final["replicas"], "observer_replicas": 0,
                         "observer_active_revisions": [], "route_checks": record["route_checks"]}
    except Exception as primary:
        record.update(phase="blocked", stopped_at=timestamp(), current_state_requires_readback=True)
        try:
            save()
        except Exception:
            primary.add_note("Closure record persistence failed; original error retained.")
        raise

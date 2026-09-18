"""Only the verified Microsoft Smart Detection auxiliary can cross this guard."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from advisor_deploy.manifest import SUBSCRIPTION, experiment_resource, load_manifest, write_manifest
from test_receipt_recovery import recovery_case, GROUP, MONITOR


AUX = GROUP + "/providers/microsoft.insights/actiongroups/Application Insights Smart Detection"
CALLER = "84d398d3-ef00-4ec1-8dd1-cb8864b2d933"
APP_ID = "3af5a1e8-2459-45cb-8683-bcd6cccbcc13"
MICROSOFT = "f8cdef31-a31e-4b4a-93e4-5f571e91255a"


@pytest.fixture
def auxiliary_case(recovery_case):
    case = recovery_case
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    case.ledger["created_at"] = created.isoformat()
    write_manifest(case.path, case.ledger, case.plan.repository)
    case.metadata = {"id": AUX, "type": "microsoft.insights/actiongroups", "location": "global",
                     "group_short_name": "SmartDetect", "enabled": True}
    case.events = [{
        "event_id": str(uuid4()), "correlation_id": "9cb15f87-3f2d-4bb3-8d9a-4943b8bf92d5",
        "timestamp": (created + timedelta(minutes=2)).isoformat(), "resource_id": AUX.lower(),
        "caller": CALLER, "operation": "Microsoft.Insights/actionGroups/write", "status": "Succeeded",
    }]
    case.principal = {"id": CALLER, "appId": APP_ID, "appOwnerOrganizationId": MICROSOFT,
                      "displayName": "Azure Smart Alerts"}

    class AuxiliaryCli(type(case.cli)):
        def run(self, args, phase, **options):
            if args[:2] == ["resource", "list"]:
                return super().run(args, phase, **options) + [{"id": AUX, "type": "microsoft.insights/actiongroups"}]
            if args[:2] == ["resource", "show"]:
                self.calls.append((args, phase, options))
                assert args[args.index("--ids") + 1] == AUX
                return deepcopy(case.metadata)
            if args[:3] == ["monitor", "activity-log", "list"]:
                self.calls.append((args, phase, options))
                assert args[args.index("--resource-id") + 1] == AUX
                assert args[args.index("--start-time") + 1] == case.ledger["created_at"]
                assert "--end-time" in args and "--max-events" in args
                return deepcopy(case.events)
            return super().run(args, phase, **options)

        def rest(self, method, url, phase, **options):
            if url.startswith("https://graph.microsoft.com/"):
                assert method == "get" and "/servicePrincipals/" + CALLER in url
                self.calls.append((["graph-read", url], phase, options))
                return deepcopy(case.principal)
            return super().rest(method, url, phase, **options)

    case.cli = AuxiliaryCli()
    case.provenance = case.path.with_suffix(".provider-auxiliary.json")
    return case


def test_only_exact_flex_auxiliary_id_passes_manifest_resource_guard():
    assert experiment_resource(AUX) == AUX
    for identifier in (
        AUX.replace("flex-exp", "aca-exp"), AUX.replace("flex-exp", "other-exp"),
        AUX.replace(SUBSCRIPTION, "11111111-1111-1111-1111-111111111111"),
        AUX.replace("Smart Detection", "Other Detection"), AUX + "/child/x",
        AUX.replace("Application Insights Smart Detection", "Other"),
        AUX.replace("actiongroups", "components"),
    ):
        with pytest.raises(ValueError):
            experiment_resource(identifier)


def test_auxiliary_ownership_requires_durable_provenance_even_for_direct_manifest_write(recovery_case):
    case = recovery_case
    case.ledger["owned_resource_ids"] += [MONITOR, AUX]
    with pytest.raises(ValueError, match="provider_aux"):
        write_manifest(case.path, case.ledger, case.plan.repository)


def test_verified_provider_auxiliary_is_recorded_only_after_provenance_is_durable(auxiliary_case, monkeypatch):
    from advisor_deploy import operations
    case = auxiliary_case
    original_write = operations.write_manifest

    def checked_write(path, value, repo):
        if AUX in value["owned_resource_ids"]:
            assert case.provenance.is_file(), "provenance must exist before ownership is persisted"
        return original_write(path, value, repo)

    monkeypatch.setattr(operations, "write_manifest", checked_write)
    before = case.path.read_bytes()
    preview = operations.reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, None)
    assert preview["resume_allowed"] and preview["blockers"] == []
    assert case.path.read_bytes() == before and not case.provenance.exists()
    case.cli.mode = "Apply"
    operations.reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, preview["confirmation"])
    saved = load_manifest(case.path)
    proof = json.loads(case.provenance.read_text())
    assert AUX in saved["owned_resource_ids"] and saved["discovery_complete"]
    assert proof["resource_id"] == AUX and proof["component_ids"] == [MONITOR]
    assert proof["caller"]["app_id"] == APP_ID and proof["caller"]["owner_tenant_id"] == MICROSOFT
    assert proof["activity"]["correlation_id"] == case.events[0]["correlation_id"]
    assert saved["status"] == "failed" and saved["outputs"] == {}
    assert not any(options.get("mutation") for _, _, options in case.cli.calls)
    case.provenance.unlink()
    with pytest.raises(ValueError, match="provider_aux"):
        load_manifest(case.path)


@pytest.mark.parametrize("defect", [
    "no-activity", "wrong-app", "wrong-owner", "wrong-object", "old-write",
    "failed-write", "wrong-operation", "wrong-event-resource", "wrong-metadata", "truncated",
])
def test_missing_or_wrong_provider_evidence_keeps_auxiliary_unowned(auxiliary_case, defect):
    from advisor_deploy.operations import reconcile_receipts
    case = auxiliary_case
    if defect == "no-activity":
        case.events = []
    elif defect == "wrong-app":
        case.principal["appId"] = str(uuid4())
    elif defect == "wrong-owner":
        case.principal["appOwnerOrganizationId"] = str(uuid4())
    elif defect == "wrong-object":
        case.principal["id"] = str(uuid4())
    elif defect == "old-write":
        case.events[0]["timestamp"] = "2020-01-01T00:00:00Z"
    elif defect == "failed-write":
        case.events[0]["status"] = "Failed"
    elif defect == "wrong-operation":
        case.events[0]["operation"] = "Microsoft.Insights/actionGroups/delete"
    elif defect == "wrong-event-resource":
        case.events[0]["resource_id"] = AUX.replace("flex-exp", "aca-exp")
    elif defect == "wrong-metadata":
        case.metadata["group_short_name"] = "unrelated"
    else:
        case.events *= 1000
    preview = reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, None)
    assert not preview["resume_allowed"] and preview["unrecorded_resource_ids"] == [AUX]
    case.cli.mode = "Apply"
    reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, preview["confirmation"])
    assert AUX not in load_manifest(case.path)["owned_resource_ids"]
    assert not case.provenance.exists()


def test_receipt_owned_component_and_fresh_group_are_required(auxiliary_case):
    from advisor_deploy.provider_auxiliary import prove_auxiliary
    case = auxiliary_case
    for receipts in ({GROUP}, {MONITOR}, set()):
        with pytest.raises(ValueError, match="provider_aux"):
            prove_auxiliary(case.cli, case.ledger, receipts, AUX)


def test_provenance_write_failure_never_updates_ownership(auxiliary_case, monkeypatch):
    from advisor_deploy import operations, provider_auxiliary
    case = auxiliary_case
    preview = operations.reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, None)
    before = case.path.read_bytes()

    def reject(*args, **kwargs):
        raise OSError("fixture write failure")

    monkeypatch.setattr(provider_auxiliary, "save_provenance", reject)
    case.cli.mode = "Apply"
    with pytest.raises(OSError):
        operations.reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, preview["confirmation"])
    assert case.path.read_bytes() == before


@pytest.mark.parametrize("tamper", ["caller", "creation", "resource", "enabled-type"])
def test_saved_provenance_remains_bound_and_typed_when_ledger_is_loaded(auxiliary_case, tamper):
    from advisor_deploy.operations import reconcile_receipts
    case = auxiliary_case
    preview = reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, None)
    case.cli.mode = "Apply"
    reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, preview["confirmation"])
    proof = json.loads(case.provenance.read_text())
    if tamper == "caller":
        proof["caller"]["app_id"] = str(uuid4())
    elif tamper == "creation":
        proof["ledger_created_at"] = "2020-01-01T00:00:00Z"
    elif tamper == "resource":
        proof["resource_id"] = AUX.replace("flex-exp", "aca-exp")
    else:
        proof["metadata"]["enabled"] = 1
    case.provenance.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match="provider_aux"):
        load_manifest(case.path)


def test_valid_auxiliary_does_not_adopt_or_clear_another_unknown_resource(auxiliary_case):
    from advisor_deploy.operations import reconcile_receipts
    case = auxiliary_case
    case.cli.foreign_inventory = True
    preview = reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, None)
    assert not preview["resume_allowed"]
    assert len(preview["unrecorded_resource_ids"]) == 1 and "unrelated" in preview["unrecorded_resource_ids"][0]
    case.cli.mode = "Apply"
    reconcile_receipts(case.path, case.plan.repository, case.cli, case.root_name, preview["confirmation"])
    saved = load_manifest(case.path)
    assert AUX in saved["owned_resource_ids"] and not saved["discovery_complete"]
    assert not any("unrelated" in value for value in saved["owned_resource_ids"])

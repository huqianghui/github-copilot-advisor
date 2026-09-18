import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest


SUB = "adca540e-984c-4cab-bc86-5d38bf979841"
GROUP = f"/subscriptions/{SUB}/resourceGroups/rg-copilot-advisor-aca-exp"
APP = GROUP + "/providers/Microsoft.App/containerApps/test-app"
MODEL = f"/subscriptions/{SUB}/resourceGroups/rg-dependencies/providers/Microsoft.CognitiveServices/accounts/model"


def test_cleanup_accepts_only_recorded_experiment_resources():
    from advisor_deploy.manifest import validate_cleanup_scope
    assert validate_cleanup_scope([APP], [GROUP, APP]) == [APP]
    with pytest.raises(ValueError, match="ownership"):
        validate_cleanup_scope([GROUP + "/providers/Microsoft.App/containerApps/other"], [GROUP, APP])


@pytest.mark.parametrize("identifier", [
    MODEL, GROUP + "/providers/Microsoft.CognitiveServices/accounts/model",
    GROUP + "/../rg-other", GROUP + "?secret=fixture", GROUP.replace(SUB, str(uuid4())),
])
def test_cleanup_rejects_dependencies_even_if_maliciously_claimed(identifier):
    from advisor_deploy.manifest import validate_cleanup_scope
    with pytest.raises(ValueError):
        validate_cleanup_scope([identifier], [identifier])


def test_manifest_is_strict_and_never_accepts_secret_payloads():
    from advisor_deploy.manifest import new_manifest, validate_manifest
    data = new_manifest("aca", str(uuid4()), "fixture", "a" * 64, "b" * 64)
    assert validate_manifest(data)["resource_group"] == "rg-copilot-advisor-aca-exp"
    for name, value in (("secretSettings", {"GITHUB_TOKEN": "fixture-secret"}), ("subscription_id", str(uuid4()))):
        broken = copy.deepcopy(data)
        broken[name] = value
        with pytest.raises(ValueError) as error:
            validate_manifest(broken)
        assert "fixture-secret" not in str(error.value)


def test_external_role_cleanup_requires_scoped_data_role_and_owned_identity():
    from advisor_deploy.manifest import validate_cleanup_scope
    identity = GROUP + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/worker"
    assignment = MODEL + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid4())
    role = {
        "id": assignment, "scope": MODEL, "identity_id": identity, "principal_id": str(uuid4()),
        "role_definition_id": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
    }
    assert validate_cleanup_scope([assignment], [GROUP, identity], [role]) == [assignment]
    role["role_definition_id"] = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
    with pytest.raises(ValueError):
        validate_cleanup_scope([assignment], [GROUP, identity], [role])


def test_publish_requires_current_data_roles_not_old_or_wrong_scope_grants():
    from advisor_deploy.operations import require_runtime_roles
    from advisor_deploy.manifest import ROLES
    storage = GROUP + "/providers/Microsoft.Storage/storageAccounts/state"
    insights = GROUP + "/providers/Microsoft.Insights/components/monitor"
    search = MODEL.replace("Microsoft.CognitiveServices/accounts/model", "Microsoft.Search/searchServices/search")
    plan = SimpleNamespace(target="functions", config=SimpleNamespace(parameters={
        "openaiAuthMode": "entra", "searchAuthMode": "entra", "openaiResourceId": MODEL, "searchResourceId": search,
    }))
    records = [
        {"id": str(index), "scope": scope,
         "role_definition_id": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{role}"}
        for index, (scope, role) in enumerate([
            (storage, ROLES["blob_owner"]), (storage, ROLES["queue"]), (insights, ROLES["metrics"]),
            (MODEL, ROLES["openai"]), (search, ROLES["search"]),
        ])
    ]
    manifest = {"owned_resource_ids": [storage, insights], "role_assignments": records,
                "outputs": {"storage_id": storage, "created_role_assignment_ids": [item["id"] for item in records]}}
    require_runtime_roles(plan, manifest)
    manifest["outputs"]["created_role_assignment_ids"] = []
    with pytest.raises(ValueError, match="required_application_roles_missing"):
        require_runtime_roles(plan, manifest)
    manifest["outputs"]["created_role_assignment_ids"] = [item["id"] for item in records]
    records[-1]["scope"] = search + "-other"
    with pytest.raises(ValueError, match="required_application_roles_missing"):
        require_runtime_roles(plan, manifest)


def test_unknown_directory_grants_cannot_be_adopted_as_created_by_this_run():
    from advisor_deploy.operations import assert_known_graph_grants
    graph = {"service_principal_id": str(uuid4()), "role_id": str(uuid4()), "grants": []}
    item = {"id": "existing-unrelated", "principalId": str(uuid4()),
            "resourceId": graph["service_principal_id"], "appRoleId": graph["role_id"]}
    with pytest.raises(ValueError, match="unrecorded_graph_grant"):
        assert_known_graph_grants([item], graph)


@pytest.mark.parametrize(("field", "value"), [
    ("target", []), ("status", []), ("owned_resource_ids", [None]), ("role_assignments", [None]),
    ("outputs", {"target": "aca"}), ("pending_role_assignment_ids", [17]),
])
def test_malformed_manifest_types_are_bounded_guard_errors(field, value):
    from advisor_deploy.manifest import new_manifest, validate_manifest
    data = new_manifest("aca", str(uuid4()), "fixture", "a" * 64, "b" * 64)
    data[field] = value
    with pytest.raises(ValueError):
        validate_manifest(data)

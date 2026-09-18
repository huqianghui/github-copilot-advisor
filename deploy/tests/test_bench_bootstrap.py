"""Real compiled contracts for the isolated benchmark identity/app split."""

from test_infra_contracts import compile_template, of_type, only


def bench():
    return compile_template("deploy/bench/infra/main.bicep")


def test_benchmark_bootstrap_needs_no_image_or_unknown_driver_allowlist():
    template = bench()
    assert template["parameters"]["enabled"]["defaultValue"] is False
    assert template["parameters"]["deployApp"]["defaultValue"] is False
    assert template["parameters"]["image"]["defaultValue"] == ""
    for field in ("operatorPrincipalIds", "operatorClientIds"):
        assert template["parameters"][field]["defaultValue"] == []
        assert template["parameters"][field].get("minLength", 0) == 0
    identity = only(template, "Microsoft.ManagedIdentity/userAssignedIdentities")
    assert identity["condition"] == "[parameters('enabled')]"
    container = only(template, "Microsoft.Storage/storageAccounts/blobServices/containers")
    assert container["condition"] == "[parameters('enabled')]"
    assert container["properties"]["publicAccess"] == "None"
    assert not of_type(template, "Microsoft.Storage/storageAccounts")
    assert not of_type(template, "Microsoft.App/managedEnvironments")
    assert not of_type(template, "Microsoft.ContainerRegistry/registries")


def test_benchmark_application_and_auth_are_separately_opted_in():
    template = bench()
    for kind in ("Microsoft.App/containerApps", "Microsoft.App/containerApps/authConfigs"):
        assert only(template, kind)["condition"] == "[and(parameters('enabled'), parameters('deployApp'))]"
    app = only(template, "Microsoft.App/containerApps")
    assert app["properties"]["template"]["scale"] == {"minReplicas": 1, "maxReplicas": 1}
    assert len(app["properties"]["template"]["containers"]) == 1
    env = {item["name"]: item["value"] for item in app["properties"]["template"]["containers"][0]["env"]}
    assert env["BENCHMARK_AUTOSTART"] == "false"
    assert env["WEB_CONCURRENCY"] == "1"
    assert env["BENCHMARK_REQUIRED_ROLE"] == "Benchmark.Invoke"


def test_bootstrap_exposes_planned_callback_and_identity_without_claiming_app_exists():
    outputs = bench()["outputs"]
    for name in ("planned_app_id", "planned_app_name", "planned_endpoint", "identity_id",
                 "identity_client_id", "identity_principal_id", "results_container_id"):
        assert outputs[name]["type"] == "string"
        assert "deployApp" not in outputs[name]["value"]
    assert "defaultDomain" in outputs["planned_endpoint"]["value"]
    for name in ("receiver_endpoint", "driver_endpoint"):
        assert "deployApp" in outputs[name]["value"]
    assert outputs["receiver_endpoint"]["value"] == outputs["driver_endpoint"]["value"]

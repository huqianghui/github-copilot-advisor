from types import SimpleNamespace

import pytest

from advisor_deploy.config import load_deploy_config
from advisor_deploy.manifest import new_manifest, validate_manifest
from advisor_deploy.operations import account, dependency_checks


TARGET_SUB = "18827cdf-a4e8-4316-a7ba-a4499139a3ec"
TARGET_TENANT = "4bc8a937-2b77-44c4-8790-dfc0068162f2"
OLD_SUB = "adca540e-984c-4cab-bc86-5d38bf979841"
MODEL = f"/subscriptions/{OLD_SUB}/resourceGroups/rg-ai-services-all/providers/Microsoft.CognitiveServices/accounts/cdc-ai-foundry-southeastasia-res"
SEARCH = f"/subscriptions/{OLD_SUB}/resourceGroups/rg-playground-aiservices/providers/Microsoft.Search/searchServices/cdc-playground-ai-search"


def external_config(config_case):
    return load_deploy_config(config_case(
        AZURE_SUBSCRIPTION_ID=TARGET_SUB,
        AZURE_OPENAI_RESOURCE_ID=MODEL, AZURE_SEARCH_RESOURCE_ID=SEARCH,
        AZURE_OPENAI_ENDPOINT="https://cdc-ai-foundry-southeastasia-res.openai.azure.com",
        AZURE_SEARCH_ENDPOINT="https://cdc-playground-ai-search.search.windows.net",
        AZURE_OPENAI_AUTH_MODE="key", AZURE_SEARCH_AUTH_MODE="key",
        AZURE_OPENAI_API_KEY="fixture-model-key", AZURE_SEARCH_API_KEY="fixture-search-key",
    ), "aca")


def test_explicit_keys_allow_only_approved_external_dependencies(config_case):
    config = external_config(config_case)
    assert config.subscription_id == TARGET_SUB
    assert config.parameters["openaiResourceId"] == MODEL
    assert config.parameters["searchResourceId"] == SEARCH
    assert config.parameters["openaiAuthMode"] == config.parameters["searchAuthMode"] == "key"


@pytest.mark.parametrize("mode", ["entra", "key"])
def test_external_dependency_does_not_accept_unapproved_resource(config_case, mode):
    with pytest.raises(ValueError):
        load_deploy_config(config_case(
            AZURE_SUBSCRIPTION_ID=TARGET_SUB,
            AZURE_OPENAI_RESOURCE_ID=MODEL + "-unapproved",
            AZURE_OPENAI_AUTH_MODE=mode, AZURE_OPENAI_API_KEY="fixture-key",
        ), "aca")


def test_external_entra_mode_is_not_silently_changed_to_key(config_case):
    with pytest.raises(ValueError):
        load_deploy_config(config_case(
            AZURE_SUBSCRIPTION_ID=TARGET_SUB, AZURE_OPENAI_RESOURCE_ID=MODEL,
            AZURE_OPENAI_AUTH_MODE="entra", AZURE_OPENAI_API_KEY="fixture-key",
        ), "aca")


def test_manifest_records_external_dependency_but_never_claims_its_ownership(config_case):
    config = external_config(config_case)
    manifest = new_manifest("aca", "11111111-2222-4333-8444-555555555555",
                            "experiment", "a" * 64, "b" * 64)
    manifest["dependencies"] = {"openai": MODEL, "search": SEARCH}
    manifest["summary"] = {"openai_auth": "key", "search_auth": "key"}
    assert validate_manifest(manifest)["dependencies"]["openai"] == config.parameters["openaiResourceId"]
    manifest["owned_resource_ids"] = [MODEL]
    with pytest.raises(ValueError):
        validate_manifest(manifest)


class KeyProbeCli:
    def __init__(self):
        self.posts = []
        self.private = SimpleNamespace(json=lambda value: "private-request.json")

    def run(self, arguments, phase, **kwargs):
        assert arguments[:3] == ["rest", "--method", "post"], "Key preflight must not query old ARM resources"
        assert "--skip-authorization-header" in arguments
        assert kwargs.get("mutation") is False
        self.posts.append(phase)
        if phase == "embedding_key_probe":
            return {"data": [{"embedding": [0.0] * 3072}]}
        if phase == "model_key_probe":
            return {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        raise AssertionError(phase)

    def rest(self, method, url, phase, **kwargs):
        assert method == "get" and phase == "search_schema"
        assert url.startswith("https://cdc-playground-ai-search.search.windows.net/indexes/")
        assert kwargs["headers"] == {"api-key": "fixture-search-key"}
        return {"fields": [{"name": "content_vector", "dimensions": 3072}]}


def test_key_preflight_uses_real_data_plane_probes_not_old_tenant_arm(config_case):
    cli = KeyProbeCli()
    dependency_checks(SimpleNamespace(config=external_config(config_case)), cli)
    assert cli.posts == ["embedding_key_probe", "model_key_probe"]


def test_wrong_embedding_dimensions_prevent_preflight_acceptance(config_case):
    cli = KeyProbeCli()
    original = cli.run

    def wrong_dimensions(arguments, phase, **kwargs):
        value = original(arguments, phase, **kwargs)
        if phase == "embedding_key_probe":
            value["data"][0]["embedding"] = [0.0] * 1536
        return value

    cli.run = wrong_dimensions
    with pytest.raises(ValueError, match="embedding_dimensions"):
        dependency_checks(SimpleNamespace(config=external_config(config_case)), cli)


def test_current_subscription_with_wrong_tenant_is_rejected():
    cli = SimpleNamespace(run=lambda *_: {
        "id": TARGET_SUB, "tenantId": "00000000-0000-0000-0000-000000000000",
        "environmentName": "AzureCloud", "state": "Enabled",
    })
    with pytest.raises(ValueError, match="tenant"):
        account(cli)


def test_cloud_build_mode_is_explicit_and_never_an_arm_or_runtime_setting(config_case):
    config = load_deploy_config(config_case(ACA_BUILD_MODE="acr"), "aca")
    assert config.build_mode == "acr"
    assert "build_mode" not in config.parameters
    assert "ACA_BUILD_MODE" not in config.runtime_environment
    assert load_deploy_config(config_case(), "aca").build_mode == "local"


def test_cloud_build_mode_is_part_of_the_approved_configuration_digest(config_case):
    from pathlib import Path
    from advisor_deploy.render import PreparedPlan

    local = load_deploy_config(config_case(), "aca")
    remote = load_deploy_config(config_case(ACA_BUILD_MODE="acr"), "aca")
    args = (Path("."), "11111111-2222-4333-8444-555555555555", "aca",
            Path("fixture.env"), Path("context"), Path("manifest.json"))
    first = PreparedPlan(*args, local, None)
    second = PreparedPlan(*args, remote, None)
    assert first.configuration_digest != second.configuration_digest

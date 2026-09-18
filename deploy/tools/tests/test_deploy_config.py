import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def load(path, target="aca"):
    from advisor_deploy.config import load_deploy_config
    return load_deploy_config(path, target)


@pytest.mark.parametrize(("target", "directory"), [
    ("aca", "/app/runtime-config"),
    ("functions", "/home/site/wwwroot/runtime-config"),
])
def test_explicit_config_maps_public_bicep_parameters(config_case, target, directory):
    config = load(config_case(), target)
    assert config.deployment_scope == "subscription"
    assert config.parameters["location"] == "southeastasia"
    assert config.parameters["runtimeConfigPath"] == directory
    assert config.parameters["modelDeploymentName"] == "approved-chat"
    assert config.parameters["maxInstances"] == 40
    assert config.parameters["workerParallelism"] == 4
    assert config.parameters["benchmarkEnabled"] is False
    assert config.runtime_environment["ESCALATION_CONFIG"] == directory + "/escalation.yaml"
    assert config.runtime_environment["DIAGNOSTICS_CONFIG"] == directory + "/diagnostics.yaml"
    assert {path.name for path in config.runtime_files.values()} == {"escalation.yaml", "diagnostics.yaml"}
    assert not config.secret_settings


def test_secrets_are_hidden_and_not_copied_in_entra_mode(config_case, caplog, capsys):
    sentinel = "fixture-secret-do-not-log"
    config = load(config_case(AZURE_OPENAI_API_KEY=sentinel, AZURE_SEARCH_API_KEY=sentinel, GITHUB_TOKEN=sentinel))
    assert set(config.secret_settings) == {"GITHUB_TOKEN"}
    assert sentinel not in repr(config)
    assert sentinel not in repr(config.secret_settings)
    assert sentinel not in json.dumps(config.public_summary())
    assert sentinel not in json.dumps(config.parameters)
    assert sentinel not in caplog.text + capsys.readouterr().out
    assert config.secret_settings.reveal_for_deployment()["GITHUB_TOKEN"] == sentinel


def test_key_mode_is_explicit_and_requires_a_key(config_case):
    with pytest.raises(ValueError, match="configuration"):
        load(config_case(AZURE_OPENAI_AUTH_MODE="key"))
    config = load(config_case(AZURE_OPENAI_AUTH_MODE="key", AZURE_OPENAI_API_KEY="fixture-key"))
    assert config.parameters["openaiAuthMode"] == "key"
    assert config.secret_settings.reveal_for_deployment()["AZURE_OPENAI_API_KEY"] == "fixture-key"


@pytest.mark.parametrize("override", [
    {"UNKNOWN_SETTING": "private-value"},
    {"AZURE_OPENAI_AUTH_MODE": "automatic"},
    {"ADVISOR_STORAGE_AUTH_MODE": "key"},
    {"ADVISOR_ENTRA_CREDENTIAL": "automatic"},
    {"AZURE_LOCATION": "eastus"},
    {"AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000"},
    {"AZURE_OPENAI_ENDPOINT": "https://name:private-value@example.test/path?key=private-value"},
    {"AZURE_OPENAI_ENDPOINT": "http://example.test"},
    {"AZURE_OPENAI_RESOURCE_ID": "/subscriptions/bad/resourceGroups/x/providers/Microsoft.Web/sites/app"},
    {"ADVISOR_MAX_INSTANCES": "0"},
    {"ADVISOR_MAX_INSTANCES": "1001"},
    {"ADVISOR_WORKER_PARALLELISM": "false"},
    {"BENCHMARK_ENABLED": "yes"},
    {"BENCHMARK_ENABLED": "true"},
    {"ACA_RESOURCE_PROFILE": "automatic"},
    {"ACA_MIN_REPLICAS": "2"},
    {"ACA_DEPLOY_APP": "true"},
])
def test_invalid_configuration_fails_without_echoing_values(config_case, override):
    with pytest.raises(ValueError, match="configuration") as error:
        load(config_case(**override))
    assert "private-value" not in str(error.value)


def test_dotenv_is_data_without_shell_or_environment_interpolation(config_case, tmp_path, monkeypatch):
    monkeypatch.setenv("DO_NOT_EXPAND", "private-value")
    config = load(config_case(GITHUB_TOKEN="$(Write-Output escaped) ${DO_NOT_EXPAND}"))
    assert config.secret_settings.reveal_for_deployment()["GITHUB_TOKEN"] == "$(Write-Output escaped) ${DO_NOT_EXPAND}"
    assert not (tmp_path / "escaped").exists()
    with pytest.raises(ValueError, match="configuration"):
        load(config_case(UNUSED="$(Write-Output private-value)"))


@pytest.mark.parametrize("line", ["GITHUB_TOKEN", "GITHUB_TOKEN='unterminated", "export GITHUB_TOKEN=x\nGITHUB_TOKEN=y"])
def test_malformed_and_duplicate_assignments_are_rejected(config_case, line):
    path = config_case()
    path.write_text(path.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="configuration"):
        load(path)


def test_real_configs_are_explicit_and_missing_paths_do_not_fallback(config_case):
    with pytest.raises(ValueError, match="configuration"):
        load(config_case(ESCALATION_CONFIG=None))
    with pytest.raises(ValueError, match="configuration"):
        load(config_case(ESCALATION_CONFIG="missing.yaml"))


def test_functions_and_aca_options_cannot_be_mixed(config_case):
    with pytest.raises(ValueError, match="configuration"):
        load(config_case(ACA_MIN_REPLICAS="1"), "functions")
    with pytest.raises(ValueError, match="configuration"):
        load(config_case(FUNCTIONS_INSTANCE_MEMORY_MB="512"), "aca")
    config = load(config_case(ADVISOR_MAX_INSTANCES="1", FUNCTIONS_INSTANCE_MEMORY_MB="2048"), "functions")
    assert config.parameters["maxInstances"] == 1
    assert config.parameters["instanceMemoryMB"] == 2048


def test_unknown_target_is_not_a_silent_aca_fallback(config_case):
    with pytest.raises(ValueError, match="configuration"):
        load(config_case(), "bench")


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_config_parameters_match_real_compiled_subscription_template(config_case, target):
    compiler = shutil.which("az")
    if compiler is None:
        pytest.skip("Azure CLI/Bicep required for compiled parameter contract")
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [compiler, "bicep", "build", "--file", str(root / "deploy" / target / "infra" / "main.bicep"), "--stdout"],
        capture_output=True, encoding="utf-8", check=False, timeout=240,
    )
    assert result.returncode == 0, result.stderr
    compiled = json.loads(result.stdout)
    config = load(config_case(), target)
    assert "subscriptionDeploymentTemplate" in compiled["$schema"]
    assert set(config.parameters) <= set(compiled["parameters"])
    required = {name for name, spec in compiled["parameters"].items() if "defaultValue" not in spec}
    assert required <= config.parameters.keys()
    assert compiled["parameters"]["secretSettings"]["type"] == "secureObject"


@pytest.mark.parametrize("target", ["aca", "functions"])
def test_existing_example_shape_reuses_local_config_without_editing_it(tmp_path, target):
    from dotenv import dotenv_values
    from advisor_deploy.config import load_deploy_config
    root = Path(__file__).resolve().parents[3]
    # Only the checked-in, sanitized example is read, never the user's .env.
    values = dict(dotenv_values(root / ".env.example", interpolate=False))
    values.update({
        "GITHUB_TOKEN": "fixture-github", "AZURE_SEARCH_API_KEY": "fixture-search",
        "AZURE_OPENAI_API_KEY": "fixture-model", "TAVILY_API_KEY": "fixture-tavily",
        "AZURE_SEARCH_ENDPOINT": "https://search.search.windows.net",
        "AZURE_OPENAI_ENDPOINT": "https://model.openai.azure.com",
        "AZURE_OPENAI_CHAT_DEPLOYMENT": "approved-chat",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET": "fixture-local-only",
        "KB_SEARCH_BUDGET_SECONDS": "7.5", "GITHUB_SEARCH_BUDGET_SECONDS": "2.5",
        "WEB_SEARCH_BUDGET_SECONDS": "9", "WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS": "3",
        "ADVISOR_LOG_LEVEL": "WARNING", "ADVISOR_LOG_FORMAT": "text",
        "ESCALATION_CONFIG": "escalation.yaml",
    })
    (tmp_path / "escalation.yaml").write_text("contacts: []\n", encoding="utf-8")
    env = tmp_path / "fixture.env"
    env.write_text("\n".join(f"{key}='{value or ''}'" for key, value in values.items()), encoding="utf-8")
    original = env.read_bytes()
    scope = "/subscriptions/adca540e-984c-4cab-bc86-5d38bf979841/resourceGroups/rg-dependencies/providers/"
    config = load_deploy_config(env, target, overrides={
        "AZURE_OPENAI_RESOURCE_ID": scope + "Microsoft.CognitiveServices/accounts/model",
        "AZURE_SEARCH_RESOURCE_ID": scope + "Microsoft.Search/searchServices/search",
        "AZURE_OPENAI_AUTH_MODE": "entra", "AZURE_SEARCH_AUTH_MODE": "entra",
    })
    assert env.read_bytes() == original
    assert config.parameters["openaiAuthMode"] == "entra"
    assert set(config.secret_settings) == {"GITHUB_TOKEN", "TAVILY_API_KEY"}
    for key in ("KB_SEARCH_BUDGET_SECONDS", "GITHUB_SEARCH_BUDGET_SECONDS", "WEB_SEARCH_BUDGET_SECONDS",
                "WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS", "ADVISOR_LOG_LEVEL", "ADVISOR_LOG_FORMAT"):
        assert config.runtime_environment[key] == values[key]
        assert config.parameters["runtimeSettings"][key] == values[key]
    assert config.runtime_environment["ADVISOR_ENTRA_CREDENTIAL"] == "managed_identity"
    assert config.runtime_files["diagnostics.yaml"] == (root / "agent" / "diagnostics.yaml").resolve()
    assert "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET" in config.ignored_names
    assert "fixture-local-only" not in repr(config) + json.dumps(config.public_summary()) + json.dumps(config.parameters)


def test_original_embedding_and_tracked_diagnostics_have_explicit_defaults(config_case):
    config = load(config_case(AZURE_OPENAI_EMBEDDING_DEPLOYMENT=None, DIAGNOSTICS_CONFIG=None))
    root = Path(__file__).resolve().parents[3]
    assert config.parameters["embeddingDeploymentName"] == "text-embedding-3-large"
    assert config.runtime_files["diagnostics.yaml"] == (root / "agent" / "diagnostics.yaml").resolve()


@pytest.mark.parametrize("overrides", [
    {"UNKNOWN_FIELD": "fixture-private"}, {"ADVISOR_MAX_INSTANCES": 40},
    {"KB_SEARCH_BUDGET_SECONDS": "nan"}, {"WEB_SEARCH_BUDGET_SECONDS": "-1"},
    {"GITHUB_SEARCH_BUDGET_SECONDS": "0"}, {"WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS": "inf"},
    {"ADVISOR_LOG_LEVEL": "TRACE"}, {"ADVISOR_LOG_FORMAT": "yaml"},
])
def test_metadata_overlay_is_validated_and_never_echoed(config_case, overrides):
    from advisor_deploy.config import load_deploy_config
    with pytest.raises(ValueError, match="configuration") as error:
        load_deploy_config(config_case(), "aca", overrides=overrides)
    assert "fixture-private" not in str(error.value)


@pytest.mark.skipif(os.name != "nt", reason="real Windows junction regression")
@pytest.mark.parametrize("forbidden", [".state", ".venv"])
def test_runtime_source_rejects_real_junction_into_forbidden_directory(tmp_path, forbidden):
    from advisor_deploy.config import approved_file
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "PowerShell required for Windows junction regression"
    target = tmp_path / forbidden
    target.mkdir()
    source = target / "escalation.yaml"
    source.write_text("contacts: []\n", encoding="utf-8")
    junction = tmp_path / "approved"
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    result = subprocess.run(
        [shell, "-NoProfile", "-Command",
         f"$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path {quote(junction)} -Target {quote(target)} | Out-Null"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(ValueError, match="configuration"):
            approved_file(junction / "escalation.yaml")
        assert source.read_text(encoding="utf-8") == "contacts: []\n"
    finally:
        junction.rmdir()


def test_cli_metadata_overlay_supports_existing_file_without_resource_ids(config_case):
    path = config_case(AZURE_OPENAI_RESOURCE_ID=None, AZURE_SEARCH_RESOURCE_ID=None)
    original = path.read_bytes()
    scope = "/subscriptions/adca540e-984c-4cab-bc86-5d38bf979841/resourceGroups/rg-dependencies/providers/"
    result = subprocess.run(
        [sys.executable, "-m", "advisor_deploy", "config", "--target", "aca", "--env-file", str(path),
         "--set", "AZURE_OPENAI_RESOURCE_ID=" + scope + "Microsoft.CognitiveServices/accounts/model",
         "--set", "AZURE_SEARCH_RESOURCE_ID=" + scope + "Microsoft.Search/searchServices/search"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["target"] == "aca"
    assert path.read_bytes() == original


def test_cli_metadata_flags_do_not_accept_or_echo_secrets(config_case):
    result = subprocess.run(
        [sys.executable, "-m", "advisor_deploy", "config", "--target", "aca",
         "--env-file", str(config_case()), "--set", "GITHUB_TOKEN=fixture-cli-secret"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "fixture-cli-secret" not in result.stdout + result.stderr

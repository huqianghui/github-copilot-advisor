"""Explicit, non-executing configuration import; never load the process environment."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
import math
import re

from dotenv.parser import parse_stream

from advisor_deploy.paths import has_link


SUBSCRIPTION = "18827cdf-a4e8-4316-a7ba-a4499139a3ec"
TENANT = "4bc8a937-2b77-44c4-8790-dfc0068162f2"
APPROVED_KEY_DEPENDENCIES = {
    "Microsoft.CognitiveServices/accounts": "/subscriptions/adca540e-984c-4cab-bc86-5d38bf979841/resourceGroups/rg-ai-services-all/providers/Microsoft.CognitiveServices/accounts/cdc-ai-foundry-southeastasia-res",
    "Microsoft.Search/searchServices": "/subscriptions/adca540e-984c-4cab-bc86-5d38bf979841/resourceGroups/rg-playground-aiservices/providers/Microsoft.Search/searchServices/cdc-playground-ai-search",
}
SECRET_NAMES = frozenset({
    "AZURE_OPENAI_API_KEY", "AZURE_SEARCH_API_KEY",
    "GITHUB_TOKEN", "TAVILY_API_KEY", "BRAVE_API_KEY",
})
IGNORED_NAMES = frozenset({
    "AZURE_CLIENT_ID", "AZURE_OPENAI_AD_TOKEN", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY",
    "ADVISOR_BLOB_SERVICE_URL", "ADVISOR_QUEUE_SERVICE_URL",
    "ADVISOR_STORAGE_ACCOUNT_NAME", "ADVISOR_STORAGE_ACCOUNT_KEY",
})
RUNTIME_DEFAULTS = {
    "KB_SEARCH_BUDGET_SECONDS": "12", "GITHUB_SEARCH_BUDGET_SECONDS": "4",
    "WEB_SEARCH_BUDGET_SECONDS": "12", "WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS": "6",
    "ADVISOR_LOG_LEVEL": "INFO", "ADVISOR_LOG_FORMAT": "json",
    "AZURE_OPENAI_API_VERSION": "2024-10-21",
}
COMMON_NAMES = frozenset({
    "AZURE_SUBSCRIPTION_ID", "AZURE_LOCATION", "AZURE_ENV_NAME",
    "AZURE_OPENAI_RESOURCE_ID", "AZURE_SEARCH_RESOURCE_ID",
    "AZURE_OPENAI_ENDPOINT", "AZURE_SEARCH_ENDPOINT",
    "AZURE_OPENAI_CHAT_DEPLOYMENT", "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "AZURE_SEARCH_INDEX",
    "AZURE_OPENAI_AUTH_MODE", "AZURE_SEARCH_AUTH_MODE",
    "ADVISOR_STORAGE_AUTH_MODE", "ADVISOR_ENTRA_CREDENTIAL",
    "ESCALATION_CONFIG", "DIAGNOSTICS_CONFIG",
    "ADVISOR_MAX_INSTANCES", "ADVISOR_WORKER_PARALLELISM",
    "BENCHMARK_ENABLED", "BENCHMARK_AUDIENCE", "BENCHMARK_API_CLIENT_ID",
    "BENCHMARK_ALLOWED_PRINCIPAL_IDS", "BENCHMARK_ALLOWED_CLIENT_IDS",
    "BENCHMARK_RECEIVER_URL", "BENCHMARK_RECEIVER_AUDIENCE",
})
TARGET_NAMES = {
    "aca": frozenset({"ACA_MIN_REPLICAS", "ACA_RESOURCE_PROFILE", "ACA_IMAGE", "ACA_DEPLOY_APP", "ACA_BUILD_MODE"}),
    "functions": frozenset({"FUNCTIONS_INSTANCE_MEMORY_MB", "FUNCTIONS_PACKAGE_IDENTIFIER"}),
}


class SecretSettings(Mapping[str, str]):
    """An explicit reveal is required at the deployment boundary, not for summaries."""

    def __init__(self, values: Mapping[str, str]):
        self._values = dict(values)

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return "<SecretSettings redacted>"

    def reveal_for_deployment(self) -> dict[str, str]:
        return dict(self._values)


@dataclass(frozen=True)
class DeployConfig:
    target: str
    parameters: dict[str, object] = field(repr=False)
    runtime_environment: dict[str, str] = field(repr=False)
    runtime_files: dict[str, Path] = field(repr=False)
    secret_settings: SecretSettings = field(repr=False)
    ignored_names: tuple[str, ...] = ()
    subscription_id: str = SUBSCRIPTION
    deployment_scope: str = "subscription"
    build_mode: str = "local"

    def public_summary(self) -> dict[str, object]:
        return {
            "target": self.target,
            "subscription_id": self.subscription_id,
            "deployment_scope": self.deployment_scope,
            "build_mode": self.build_mode,
            "location": "southeastasia",
            "configured_parameters": sorted(self.parameters),
            "configured_secret_names": sorted(self.secret_settings),
            "runtime_files": sorted(self.runtime_files),
            "ignored_names": list(self.ignored_names),
        }


def _invalid(name: str = "") -> ValueError:
    return ValueError(f"invalid deployment configuration{': ' + name if name else ''}")


def approved_file(path: Path) -> Path:
    """Accept an explicit YAML file, never a fallback or a credential/config directory."""
    path = path.absolute()
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise _invalid("runtime YAML")
    try:
        if has_link(path):
            raise _invalid("runtime YAML link or reparse point")
        resolved = path.resolve(strict=True)
        forbidden = {".env", ".venv", ".state", "tests", "test"}
        if any(part.lower() in forbidden for candidate in (path, resolved) for part in candidate.parts):
            raise _invalid("runtime YAML location")
        if has_link(resolved) or not resolved.is_file() or not 0 < resolved.stat().st_size <= 1024 * 1024:
            raise _invalid("runtime YAML missing or too large")
        return resolved
    except OSError:
        raise _invalid("runtime YAML unavailable") from None


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "")
    if not value.strip():
        raise _invalid(name)
    return value


def _choice(values: Mapping[str, str], name: str, default: str, choices: set[str]) -> str:
    value = values.get(name, default)
    if value not in choices:
        raise _invalid(name)
    return value


def _integer(values: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    value = values.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", value) or not low <= int(value) <= high:
        raise _invalid(name)
    return int(value)


def _boolean(values: Mapping[str, str], name: str) -> bool:
    return _choice(values, name, "false", {"true", "false"}) == "true"


def _endpoint(value: str, name: str) -> str:
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https" and url.hostname and not url.username and not url.password
            and not url.query and not url.fragment and url.port != 0
            and not any(char.isspace() or ord(char) < 32 for char in value) and "\\" not in value
        )
    except ValueError:
        valid = False
    if not valid:
        raise _invalid(name)
    return value


def _resource_id(value: str, resource_type: str, name: str, *, auth_mode: str = "entra") -> str:
    if (auth_mode == "key" and resource_type in APPROVED_KEY_DEPENDENCIES
            and value.lower() == APPROVED_KEY_DEPENDENCIES[resource_type].lower()):
        return value
    pattern = rf"/subscriptions/{SUBSCRIPTION}/resourceGroups/[A-Za-z0-9_.()-]+/providers/{resource_type}/[A-Za-z0-9_.-]+"
    if not re.fullmatch(pattern, value, flags=re.IGNORECASE):
        raise _invalid(name)
    return value


def _identifier(value: str, name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise _invalid(name)
    return value


def _guid(value: str, name: str) -> str:
    try:
        parsed = str(UUID(value))
    except ValueError:
        raise _invalid(name) from None
    if value.lower() != parsed:
        raise _invalid(name)
    return parsed


def _local_only(name: str) -> bool:
    return name in IGNORED_NAMES or (
        name.startswith("CONNECTIONS__") and bool(re.fullmatch(r"[A-Z0-9_]+", name))
    )


def _runtime_settings(values: Mapping[str, str]) -> dict[str, str]:
    settings = {name: values.get(name, default) for name, default in RUNTIME_DEFAULTS.items()}
    for name in RUNTIME_DEFAULTS:
        if name.endswith("_SECONDS"):
            try:
                number = float(settings[name])
            except ValueError:
                raise _invalid(name) from None
            if not math.isfinite(number) or number <= 0:
                raise _invalid(name)
    settings["ADVISOR_LOG_LEVEL"] = settings["ADVISOR_LOG_LEVEL"].upper()
    settings["ADVISOR_LOG_FORMAT"] = settings["ADVISOR_LOG_FORMAT"].lower()
    _choice(settings, "ADVISOR_LOG_LEVEL", "INFO", {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
    _choice(settings, "ADVISOR_LOG_FORMAT", "json", {"json", "text"})
    _identifier(settings["AZURE_OPENAI_API_VERSION"], "model API version")
    return settings


def load_deploy_config(
    env_file: Path, target: str, *, overrides: Mapping[str, str] | None = None,
) -> DeployConfig:
    if target not in TARGET_NAMES:
        raise _invalid("target")
    allowed = COMMON_NAMES | SECRET_NAMES | TARGET_NAMES[target] | RUNTIME_DEFAULTS.keys()

    def validate_setting(name: str, value: str) -> None:
        if not isinstance(name, str) or not isinstance(value, str):
            raise _invalid("settings and overrides require string names and values")
        if name not in allowed and not _local_only(name):
            raise _invalid("unknown setting")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise _invalid("multiline setting")

    values: dict[str, str] = {}
    try:
        if env_file.stat().st_size > 1024 * 1024:
            raise _invalid("file too large")
        with env_file.open(encoding="utf-8-sig") as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise _invalid("dotenv syntax")
                if binding.key is None:
                    continue
                if binding.value is None or binding.key in values:
                    raise _invalid("duplicate or unassigned setting")
                validate_setting(binding.key, binding.value)
                values[binding.key] = binding.value
    except (OSError, UnicodeError):
        raise _invalid("file unreadable") from None
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise _invalid("overrides must be a mapping")
        for name, value in overrides.items():
            validate_setting(name, value)
            values[name] = value
    ignored = {name for name in values if _local_only(name)}
    if "ADVISOR_ENTRA_CREDENTIAL" in values:
        ignored.add("ADVISOR_ENTRA_CREDENTIAL")

    if values.get("AZURE_SUBSCRIPTION_ID", SUBSCRIPTION) != SUBSCRIPTION:
        raise _invalid("subscription")
    _choice(values, "AZURE_LOCATION", "southeastasia", {"southeastasia"})
    _choice(values, "ADVISOR_STORAGE_AUTH_MODE", "entra", {"entra"})
    _choice(values, "ADVISOR_ENTRA_CREDENTIAL", "managed_identity", {"managed_identity", "azure_cli"})
    runtime_settings = _runtime_settings(values)
    runtime_directory = "/app/runtime-config" if target == "aca" else "/home/site/wwwroot/runtime-config"
    defaults = {
        "DIAGNOSTICS_CONFIG": str(Path(__file__).resolve().parents[4] / "agent" / "diagnostics.yaml"),
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
    }
    values = defaults | values
    files = {
        filename: approved_file(env_file.parent / _required(values, name))
        for name, filename in (
            ("ESCALATION_CONFIG", "escalation.yaml"), ("DIAGNOSTICS_CONFIG", "diagnostics.yaml")
        )
    }
    parameters: dict[str, object] = {
        "location": "southeastasia",
        "environmentName": _identifier(values.get("AZURE_ENV_NAME", "copilot-advisor-exp"), "environment name"),
        "openaiResourceId": _resource_id(_required(values, "AZURE_OPENAI_RESOURCE_ID"), "Microsoft.CognitiveServices/accounts", "model resource",
                                       auth_mode=values.get("AZURE_OPENAI_AUTH_MODE", "entra")),
        "searchResourceId": _resource_id(_required(values, "AZURE_SEARCH_RESOURCE_ID"), "Microsoft.Search/searchServices", "search resource",
                                       auth_mode=values.get("AZURE_SEARCH_AUTH_MODE", "entra")),
        "openaiEndpoint": _endpoint(_required(values, "AZURE_OPENAI_ENDPOINT"), "model endpoint"),
        "searchEndpoint": _endpoint(_required(values, "AZURE_SEARCH_ENDPOINT"), "search endpoint"),
        "modelDeploymentName": _identifier(_required(values, "AZURE_OPENAI_CHAT_DEPLOYMENT"), "model deployment"),
        "embeddingDeploymentName": _identifier(_required(values, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"), "embedding deployment"),
        "searchIndexName": _identifier(_required(values, "AZURE_SEARCH_INDEX"), "search index"),
        "runtimeConfigPath": runtime_directory,
        "runtimeSettings": runtime_settings,
        "openaiAuthMode": _choice(values, "AZURE_OPENAI_AUTH_MODE", "entra", {"entra", "key"}),
        "searchAuthMode": _choice(values, "AZURE_SEARCH_AUTH_MODE", "entra", {"entra", "key"}),
        "maxInstances": _integer(values, "ADVISOR_MAX_INSTANCES", 40, 1, 1000),
        "workerParallelism": _integer(values, "ADVISOR_WORKER_PARALLELISM", 4, 1, 32),
        "benchmarkEnabled": _boolean(values, "BENCHMARK_ENABLED"),
        "benchmarkAudience": values.get("BENCHMARK_AUDIENCE", ""),
        "benchmarkApiClientId": values.get("BENCHMARK_API_CLIENT_ID", ""),
        "benchmarkReceiverUrl": values.get("BENCHMARK_RECEIVER_URL", ""),
        "benchmarkReceiverAudience": values.get("BENCHMARK_RECEIVER_AUDIENCE", ""),
    }
    for name, parameter in (
        ("BENCHMARK_ALLOWED_PRINCIPAL_IDS", "benchmarkAllowedPrincipalIds"),
        ("BENCHMARK_ALLOWED_CLIENT_IDS", "benchmarkAllowedClientIds"),
    ):
        parameters[parameter] = [_guid(item.strip(), name) for item in values.get(name, "").split(",") if item.strip()]
    if parameters["benchmarkEnabled"]:
        for name in ("BENCHMARK_AUDIENCE", "BENCHMARK_RECEIVER_AUDIENCE"):
            audience = _required(values, name)
            if not re.fullmatch(r"[A-Za-z0-9:/._-]+", audience):
                raise _invalid(name)
        _guid(_required(values, "BENCHMARK_API_CLIENT_ID"), "benchmark registration")
        _endpoint(_required(values, "BENCHMARK_RECEIVER_URL"), "benchmark receiver")
        if not parameters["benchmarkAllowedClientIds"] or not parameters["benchmarkAllowedPrincipalIds"]:
            raise _invalid("benchmark allowlists")
    elif any(values.get(name) for name in COMMON_NAMES if name.startswith("BENCHMARK_") and name != "BENCHMARK_ENABLED"):
        raise _invalid("benchmark settings require explicit enablement")

    secrets = {name: value for name, value in values.items() if name in SECRET_NAMES and value}
    for mode, key in (("openaiAuthMode", "AZURE_OPENAI_API_KEY"), ("searchAuthMode", "AZURE_SEARCH_API_KEY")):
        if parameters[mode] == "key":
            _required(secrets, key)
        else:
            secrets.pop(key, None)
    if target == "aca":
        parameters.update({
            "minReplicas": _integer(values, "ACA_MIN_REPLICAS", 0, 0, 1),
            "resourceProfile": _choice(values, "ACA_RESOURCE_PROFILE", "quarter", {"quarter", "half", "one", "two"}),
            "deployApp": _boolean(values, "ACA_DEPLOY_APP"),
            "image": values.get("ACA_IMAGE", ""),
        })
        image = values.get("ACA_IMAGE", "")
        if image and not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})", image):
            raise _invalid("image")
        if parameters["deployApp"] and not image:
            raise _invalid("image required")
    else:
        memory = _integer(values, "FUNCTIONS_INSTANCE_MEMORY_MB", 512, 512, 4096)
        if memory not in {512, 2048, 4096}:
            raise _invalid("function memory")
        identifier = values.get("FUNCTIONS_PACKAGE_IDENTIFIER", "")
        if identifier:
            _identifier(identifier, "package identifier")
        parameters.update({"runtimeVersion": "3.12", "instanceMemoryMB": memory, "packageIdentifier": identifier})
    return DeployConfig(
        target, parameters,
        runtime_settings | {"ADVISOR_ENTRA_CREDENTIAL": "managed_identity"} | {name: f"{runtime_directory}/{filename}" for name, filename in (
            ("ESCALATION_CONFIG", "escalation.yaml"), ("DIAGNOSTICS_CONFIG", "diagnostics.yaml")
        )},
        files, SecretSettings(secrets), ignored_names=tuple(sorted(ignored)),
        build_mode=_choice(values, "ACA_BUILD_MODE", "local", {"local", "acr"}) if target == "aca" else "local",
    )

"""Credential-free bounded smoke/stress plans; unauthenticated tests stay on loopback."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationInfo, model_validator

from advisor_shared.benchmark import (
    DEFAULT_DATASET_PATH, Digest, HistoryMode, Identifier, Scenario, WireModel, load_dataset,
)


Guid = Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{36}$")]
LEVELS = [1, 2, 4, 8, 16, 32, 64, 128]


class AuthPlan(WireModel):
    tenant_id: Guid
    audience: Guid
    scope: str
    driver_client_id: Guid
    operator_client_ids: list[Guid] = Field(min_length=1)
    operator_principal_ids: list[Guid] = Field(min_length=1)
    target_client_ids: list[Guid] = Field(min_length=1)
    target_principal_ids: list[Guid] = Field(min_length=1)

    @model_validator(mode="after")
    def separate_identities(self):
        from uuid import UUID
        identities = [self.tenant_id, self.driver_client_id, *self.operator_client_ids,
                      *self.operator_principal_ids, *self.target_client_ids,
                      *self.target_principal_ids, self.audience]
        for identity in identities:
            UUID(identity)
        if self.scope != "api://" + self.audience + "/.default":
            raise ValueError("scope must request the dedicated API resource URI")
        if (set(map(str.lower, self.operator_client_ids)) & set(map(str.lower, self.target_client_ids))
                or set(map(str.lower, self.operator_principal_ids))
                & set(map(str.lower, self.target_principal_ids))):
            raise ValueError("operator and target callback allowlists must be separate")
        return self


class DeploymentBounds(WireModel):
    target_id: str = Field(min_length=1, max_length=1024)
    endpoint: str
    callback_url: str
    region: Literal["southeastasia"]
    host: Literal["aca", "functions"]
    host_mode: Literal["aca-resident", "aca-zero", "flex-on-demand"]
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    artifact_sha256: Digest
    config_sha256: Digest
    dataset_sha256: Digest
    model_deployment: Identifier
    model_version: Identifier
    model_sku: Identifier
    retrieval_config_sha256: Digest
    python_version: str = Field(pattern=r"^3\.\d+\.\d+$")
    memory_mb: int = Field(ge=128)
    max_instances: int = Field(ge=1)
    worker_parallelism: int = Field(ge=1)
    worker_processes: Literal[1]
    queue_batch_size: int = Field(ge=1)
    queue_poll_seconds: float = Field(gt=0)
    min_replicas: int = Field(ge=0)
    always_ready: Literal[0]
    max_concurrency: int = Field(ge=1, le=128)
    max_requests_per_run: int = Field(ge=1, le=1_000_000)
    max_submit_seconds: float = Field(gt=0, le=1800)
    driver_replicas: Literal[1]
    driver_processes: Literal[1]
    driver_cpu_cores: float = Field(gt=0)
    driver_memory_mb: int = Field(ge=128)
    dedicated_benchmark_audience: str
    results_account_url: str
    config_verified: Literal[True]
    cloud_smoke_verified: bool = Field(strict=True)
    capacity_verified: Literal[True]
    entra_registration_verified: Literal[True]
    valid_until: str

    @model_validator(mode="after")
    def valid_manifest(self, info: ValidationInfo):
        expiry = datetime.fromisoformat(self.valid_until.replace("Z", "+00:00"))
        historical = bool(info.context and info.context.get("historical"))
        if expiry.utcoffset() is None or (not historical and expiry <= datetime.now(timezone.utc)):
            raise ValueError("deployment manifest is expired or has no timezone")
        if (self.host == "functions") != (self.host_mode == "flex-on-demand"):
            raise ValueError("host and host mode disagree")
        if self.min_replicas != (1 if self.host_mode == "aca-resident" else 0):
            raise ValueError("host mode and minimum replicas disagree")
        return self


class ResultsPlan(WireModel):
    account_url: str
    container: Literal["benchmark-results"] = "benchmark-results"
    operation_timeout_seconds: float = Field(default=10., gt=0, le=30)
    flush_timeout_seconds: float = Field(default=30., gt=0, le=60)
    max_pending_events: int = Field(default=256, ge=1, le=1024)
    checkpoint_every: int = Field(default=10, ge=1, le=100)
    max_record_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def private_account_endpoint(self):
        url = urlsplit(self.account_url)
        if (url.scheme != "https" or url.username or url.password or url.query or url.fragment
                or url.path not in {"", "/"} or url.port is not None
                or not url.hostname or not url.hostname.endswith(".blob.core.windows.net")):
            raise ValueError("results require an explicit Azure Blob service endpoint without credentials")
        return self


class RunPlan(WireModel):
    """Cloud smoke is an operator-reviewed functional bootstrap, never a stress run."""
    target_url: str
    callback_url: str
    expected_target_id: str = Field(min_length=1, max_length=1024)
    local_test: bool = False
    profile: Literal["warm", "concurrency", "cold", "local-smoke", "cloud-smoke"] = "concurrency"
    scenario: Scenario = "real_agent"
    history_mode: HistoryMode = "empty"
    case_ids: list[Identifier] = Field(min_length=1)
    seed: int = Field(ge=0, le=2**32 - 1)
    dataset_sha256: Digest
    cases_path: str = DEFAULT_DATASET_PATH
    concurrency_levels: list[int] = Field(default_factory=lambda: LEVELS.copy())
    target_completions: int = Field(default=50, ge=1)
    warm_target: int = Field(default=100, ge=100)
    warmup_requests: int = Field(default=3, ge=1, le=100)
    cold_target: int = Field(default=10, ge=10)
    cold_attempts: int = Field(default=15, ge=10, le=15)
    zero_wait_seconds: float = Field(default=3600, gt=0, le=3600)
    zero_evidence_path: str | None = None
    min_seconds: float = Field(default=180, ge=0)
    max_submit_seconds: float = Field(default=1800, gt=0, le=1800)
    request_timeout_seconds: float = Field(default=600, gt=0, le=600)
    drain_seconds: float = Field(default=600, gt=0, le=600)
    trend_seconds: float = Field(default=60, gt=0, le=60)
    http_timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_http_attempts: int = Field(default=3, ge=1, le=5)
    max_requests: int = Field(default=10000, ge=1, le=1_000_000)
    driver_max_loop_lag_seconds: float = Field(default=1, gt=0)
    auth: AuthPlan | None = None
    deployment: DeploymentBounds | None = None
    results: ResultsPlan | None = None

    @model_validator(mode="after")
    def enforce_bounds(self):
        if (not self.concurrency_levels or len(set(self.concurrency_levels)) != len(self.concurrency_levels)
                or self.concurrency_levels != sorted(self.concurrency_levels)
                or any(type(level) is not int or level not in LEVELS
                       for level in self.concurrency_levels)):
            raise ValueError("concurrency must be an ascending subset of the approved ladder")
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("case IDs must be unique")
        if self.min_seconds > self.max_submit_seconds:
            raise ValueError("minimum duration exceeds submission bound")
        if self.cold_target > self.cold_attempts:
            raise ValueError("cold target exceeds attempt bound")
        if self.profile == "cloud-smoke" and self.local_test:
            raise ValueError("cloud-smoke requires the authenticated cloud boundary")
        for value in (self.target_url, self.callback_url):
            parsed = urlsplit(value)
            if (parsed.username or parsed.password or parsed.query or parsed.fragment
                    or not parsed.hostname):
                raise ValueError("endpoints must not carry credentials, queries or fragments")
            if self.local_test:
                if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
                    raise ValueError("local test boundary requires literal loopback HTTP URLs")
            elif parsed.scheme != "https":
                raise ValueError("cloud benchmark endpoints require HTTPS")
        if urlsplit(self.target_url).path not in ("", "/"):
            raise ValueError("target URL must be an origin")
        if urlsplit(self.callback_url).path != "/bench/replies":
            raise ValueError("callback path must be /bench/replies")
        if self.local_test:
            if (self.scenario != "synthetic" or self.auth is not None or self.deployment is not None
                    or self.results is not None):
                raise ValueError("local boundary is synthetic only and cannot carry cloud auth")
            return self
        if (self.profile == "local-smoke" or self.auth is None or self.deployment is None
                or self.results is None):
            raise ValueError("cloud runs need verified deployment bounds and explicit Entra auth")
        deployment = self.deployment
        if (deployment.target_id != self.expected_target_id
                or deployment.endpoint.rstrip("/") != self.target_url.rstrip("/")
                or deployment.callback_url != self.callback_url
                or deployment.dataset_sha256 != self.dataset_sha256
                or deployment.dedicated_benchmark_audience != self.auth.audience
                or deployment.results_account_url.rstrip("/") != self.results.account_url.rstrip("/")):
            raise ValueError("plan does not match the deployed target, callback, dataset or audience")
        if (max(self.concurrency_levels) > deployment.max_concurrency
                or self.max_requests > deployment.max_requests_per_run
                or self.max_submit_seconds > deployment.max_submit_seconds):
            raise ValueError("plan exceeds deployment manifest bounds")
        if (self.request_timeout_seconds != 600 or self.drain_seconds != 600
                or self.trend_seconds != 60):
            raise ValueError("cloud sampling must use the approved observation limits")
        if self.profile == "cloud-smoke":
            if (self.concurrency_levels != [1] or self.min_seconds != 0
                    or not 1 <= self.target_completions <= 5
                    or self.max_requests != self.target_completions
                    or self.max_submit_seconds > 600):
                raise ValueError("cloud-smoke requires one slot and an explicit 1-5 request budget")
        else:
            if not deployment.cloud_smoke_verified:
                raise ValueError("cloud stress profiles require completed smoke verification")
            if self.min_seconds < 180 or self.target_completions < 50:
                raise ValueError("cloud sampling must use the approved observation limits")
        if self.profile == "cold" and (deployment.host_mode == "aca-resident"
                                       or self.zero_evidence_path is None):
            raise ValueError("cold runs need a zero-capable target and control-plane evidence file")
        return self


def load_plan(path: Path) -> RunPlan:
    return RunPlan.model_validate_json(path.read_text(encoding="utf-8"))


def load_cases(path: Path, plan: RunPlan) -> dict:
    data = load_dataset(path, expected_digest=plan.dataset_sha256)
    if not set(plan.case_ids) <= {case["case_id"] for case in data["cases"]}:
        raise ValueError("workload plan selects a missing case")
    return data

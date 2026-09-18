"""Experiment wire protocol; rendered replies are transient, never telemetry."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Scenario = Literal["synthetic", "real_agent"]
HistoryMode = Literal["empty", "seeded20", "same_user"]
ReplyOutcome = Literal["success", "degraded", "failed", "unknown", "expired", "ignored"]
ScaleGroup = Literal["app", "http", "worker"]
DEFAULT_DATASET_PATH = "runtime-config/benchmark-cases.json"
MAX_DATASET_BYTES = 4 * 1024 * 1024


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False,
                              hide_input_in_errors=True)


class BenchmarkRunRegistration(WireModel):
    run_id: Identifier
    profile: Identifier
    case_ids: list[Identifier] = Field(min_length=1, max_length=1000)
    seed: int = Field(ge=0, le=2**32 - 1)
    scenario: Scenario
    history_mode: HistoryMode
    max_requests: int = Field(ge=1, le=1_000_000)
    max_concurrency: int = Field(ge=1, le=128)
    expires_at: str
    dataset_sha256: Digest

    @field_validator("case_ids")
    @classmethod
    def unique_cases(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("case IDs must be unique")
        return value

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("expiry requires a timezone")
        return value


class BenchmarkRunAccepted(WireModel):
    run_id: Identifier
    target_id: str = Field(min_length=1, max_length=1024)
    dataset_sha256: Digest


class BenchmarkRequest(WireModel):
    run_id: Identifier
    request_id: Identifier
    case_id: Identifier
    virtual_user: int = Field(ge=0, le=127)
    scenario: Scenario


class BenchmarkAck(WireModel):
    run_id: Identifier
    request_id: Identifier
    job_id: Identifier


class StageObservation(WireModel):
    name: Literal[
        "ingress", "queue_wait", "worker_init", "auth", "connection", "history_setup",
        "history_read", "history_commit",
        "synthetic", "agent",
        "kb", "github", "web", "model", "render", "send",
    ]
    duration_ms: float = Field(ge=0)
    outcome: Literal["success", "degraded", "failed", "unknown", "empty"]


class DependencyObservation(WireModel):
    dependency: Literal["ingress", "model", "search", "github", "web", "storage"]
    calls: int = Field(ge=1)
    throttled: int = Field(ge=0)

    @model_validator(mode="after")
    def bounded_throttles(self):
        if self.throttled > self.calls:
            raise ValueError("throttled count exceeds observed calls")
        return self


class BootObservation(WireModel):
    group: ScaleGroup
    boot_id: Identifier
    instance_id: Identifier
    first_request: bool


class SafeTelemetry(WireModel):
    trace_id: Identifier | None = None
    stages: list[StageObservation] = Field(default_factory=list, max_length=100)
    dependencies: list[DependencyObservation] = Field(default_factory=list, max_length=100)
    boots: list[BootObservation] = Field(default_factory=list, max_length=3)
    pending_jobs: int | None = Field(default=None, ge=0)
    host_crashed: bool = False
    host_oom: bool = False
    instances: int | None = Field(default=None, ge=0)
    workers: int | None = Field(default=None, ge=0)
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_mb: float | None = Field(default=None, ge=0)
    model_rounds: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    business_attempts: int | None = Field(default=None, ge=1)
    delivery_attempts: int | None = Field(default=None, ge=1)
    cache_hit: bool | None = None
    paths: list[Literal["kb", "github", "web", "generic", "escalation"]] = Field(
        default_factory=list, max_length=5)


class RenderedReply(WireModel):
    type: Literal["message"]
    format: Literal["markdown"]
    text: str = Field(min_length=1, max_length=32768, repr=False)

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value):
        if not value.strip():
            raise ValueError("rendered final message must not be empty")
        return value


class BenchmarkReply(WireModel):
    run_id: Identifier
    request_id: Identifier
    job_id: Identifier
    event: Literal["typing", "final"]
    outcome: ReplyOutcome | None = None
    telemetry: SafeTelemetry = Field(default_factory=SafeTelemetry)
    reply: RenderedReply | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def final_requires_outcome(self):
        if (self.event == "final") != (self.outcome is not None):
            raise ValueError("only final replies must carry an outcome")
        if self.event == "typing" and self.reply is not None:
            raise ValueError("typing cannot carry a final message")
        if self.outcome in {"success", "degraded"} and self.reply is None:
            raise ValueError("answered outcomes require a complete rendered message")
        return self


def conversation_id(request: BenchmarkRequest, history_mode: HistoryMode) -> str:
    """Target namespaces this synthetic key, never a real conversation/user identity."""
    suffix = (f"user-{request.virtual_user}" if history_mode == "same_user"
              else request.request_id)
    return f"bench:{request.run_id}:{suffix}"


def dataset_digest(dataset: dict) -> str:
    """Canonical JSON digest survives Windows/Linux line-ending and key-order changes."""
    canonical = json.dumps(dataset, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_dataset(value: object) -> dict:
    """Validate the common v1 fixture without normalizing content or exposing it in errors."""
    try:
        if (not isinstance(value, dict) or set(value) != {"version", "cases", "seeded_history"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["cases"], list) or not 1 <= len(value["cases"]) <= 1000):
            raise ValueError()
        identities = set()
        for case in value["cases"]:
            if not isinstance(case, dict) or set(case) != {"case_id", "question"}:
                raise ValueError()
            identity = BenchmarkRequest(
                run_id="validation", request_id="validation", case_id=case["case_id"],
                virtual_user=0, scenario="synthetic").case_id
            if (identity in identities or not isinstance(case["question"], str)
                    or not case["question"].strip() or len(case["question"]) > 32000):
                raise ValueError()
            identities.add(identity)
        history = value["seeded_history"]
        if not isinstance(history, list) or len(history) != 20:
            raise ValueError()
        for index, item in enumerate(history):
            if (not isinstance(item, dict) or set(item) != {"role", "content"}
                    or item["role"] != ("user" if index % 2 == 0 else "assistant")
                    or not isinstance(item["content"], str) or not item["content"]
                    or len(item["content"]) > 32000):
                raise ValueError()
        return value
    except (ValueError, TypeError, KeyError):
        raise ValueError("approved benchmark dataset is invalid") from None


def parse_dataset(content: bytes) -> dict:
    """Parse the strict shared fixture contract; errors never expose dataset content."""
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate dataset field")
            value[key] = item
        return value

    try:
        if not isinstance(content, bytes) or len(content) > MAX_DATASET_BYTES:
            raise ValueError("invalid dataset bytes")
        data = json.loads(content.decode("utf-8"), object_pairs_hook=unique_object)
        return validate_dataset(data)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("approved benchmark dataset is invalid") from None


def load_dataset(path: str | Path, *, expected_digest: str | None = None) -> dict:
    """Read a bounded approved fixture through the common artifact/runtime parser."""
    try:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_DATASET_BYTES + 1)
    except (OSError, ValueError, TypeError):
        raise ValueError("approved benchmark dataset is missing or invalid") from None
    data = parse_dataset(raw)
    if expected_digest is not None and dataset_digest(data) != expected_digest:
        raise ValueError("workload dataset digest mismatch")
    return data

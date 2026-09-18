"""Validated, payload-private contracts shared by cloud hosts and workers."""
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator,
    model_validator,
)
from typing_extensions import Self, TypedDict


Identifier = Annotated[str, Field(min_length=1)]


class _CloudModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value


def _without_authorization(value: JsonValue) -> None:
    if isinstance(value, dict):
        if any(key.casefold() == "authorization" for key in value):
            raise ValueError("activity must not contain Authorization")
        for item in value.values():
            _without_authorization(item)
    elif isinstance(value, list):
        for item in value:
            _without_authorization(item)


class HistoryMessage(_CloudModel):
    role: Literal["user", "assistant"]
    content: str = Field(repr=False)
    written_at: AwareDatetime
    turn_id: Identifier
    sequence: int = Field(ge=0, strict=True)

    _validate_time = field_validator("written_at")(_utc)


class TaskPayload(_CloudModel):
    job_id: Identifier
    partition_key: Identifier
    activity: dict[str, JsonValue] = Field(repr=False)
    accepted_at: AwareDatetime
    trace_id: Identifier
    target: Literal["teams", "benchmark"]
    run_id: Identifier | None = None
    request_id: Identifier | None = None

    _validate_time = field_validator("accepted_at")(_utc)

    @field_validator("activity")
    @classmethod
    def validate_activity(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _without_authorization(value)
        return value

    @model_validator(mode="after")
    def validate_benchmark_ids(self) -> Self:
        if self.target == "benchmark" and (not self.run_id or not self.request_id):
            raise ValueError("benchmark tasks require run_id and request_id")
        return self


class QueueWake(_CloudModel):
    partition_key: Identifier
    trace_id: Identifier
    version: int = Field(default=1, strict=True, ge=1, le=1)


class TurnMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class PreparedTurn(_CloudModel):
    reply: dict[str, JsonValue] | None = Field(repr=False)
    history: list[TurnMessage] = Field(repr=False)
    outcome: Literal["success", "degraded", "ignored"]

    @field_validator("history")
    @classmethod
    def validate_history(cls, value: list[TurnMessage]) -> list[TurnMessage]:
        if value and [message["role"] for message in value] != ["user", "assistant"]:
            raise ValueError("history must be empty or a user/assistant pair")
        return value

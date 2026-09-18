from dataclasses import dataclass
import math
from typing import Literal


Outcome = Literal["success", "degraded", "failed", "unknown", "expired", "ignored", "censored"]
OUTCOMES: tuple[Outcome, ...] = (
    "success", "degraded", "failed", "unknown", "expired", "ignored", "censored")


def finite_nonnegative(value: float, name: str) -> None:
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or value < 0):
        raise ValueError(f"{name} must be finite and nonnegative")


@dataclass(frozen=True)
class Sample:
    request_id: str
    case_id: str
    status: Outcome
    started_s: float
    finished_s: float | None
    ack_ms: float | None = None

    def __post_init__(self) -> None:
        if not self.request_id or not self.case_id or self.status not in OUTCOMES:
            raise ValueError("invalid sample identity or status")
        finite_nonnegative(self.started_s, "started_s")
        if self.ack_ms is not None:
            finite_nonnegative(self.ack_ms, "ack_ms")
        if self.status == "censored":
            if self.finished_s is not None:
                raise ValueError("censored work has no observed completion")
        else:
            if self.finished_s is None:
                raise ValueError("terminal samples require a completion time")
            finite_nonnegative(self.finished_s, "finished_s")
            if self.finished_s < self.started_s:
                raise ValueError("completion precedes submission")

    @property
    def duration_ms(self) -> float | None:
        if self.finished_s is None:
            return None
        return (self.finished_s - self.started_s) * 1000

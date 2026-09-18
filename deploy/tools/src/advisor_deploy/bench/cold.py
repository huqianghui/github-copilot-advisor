from dataclasses import dataclass
from typing import Literal

from advisor_deploy.bench.models import finite_nonnegative


ColdReason = Literal["idle", "restart", "deployment", "scale_out", "warm"]
ColdCategory = Literal["natural_zero", "restart", "deployment", "scale_out", "warm", "unverified"]


@dataclass(frozen=True)
class ColdEvidence:
    idle_seconds: float
    zero_confirmed: bool
    new_boot_confirmed: bool
    reason: ColdReason

    def __post_init__(self) -> None:
        finite_nonnegative(self.idle_seconds, "idle_seconds")
        if (type(self.zero_confirmed) is not bool or type(self.new_boot_confirmed) is not bool
                or self.reason not in {"idle", "restart", "deployment", "scale_out", "warm"}):
            raise ValueError("invalid cold-start evidence")


def classify_cold(evidence: ColdEvidence) -> ColdCategory:
    if evidence.reason in {"restart", "deployment", "scale_out"}:
        return evidence.reason if evidence.new_boot_confirmed else "unverified"
    if evidence.reason == "idle":
        return ("natural_zero" if evidence.zero_confirmed and evidence.new_boot_confirmed
                else "unverified")
    if evidence.new_boot_confirmed or evidence.zero_confirmed:
        return "unverified"
    return "warm"

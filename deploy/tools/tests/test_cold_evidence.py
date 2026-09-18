import pytest

from advisor_deploy.bench.cold import ColdEvidence, classify_cold


@pytest.mark.parametrize("reason,zero,new_boot,want", [
    ("idle", False, True, "unverified"),
    ("idle", True, False, "unverified"),
    ("idle", True, True, "natural_zero"),
    ("restart", True, True, "restart"),
    ("deployment", True, True, "deployment"),
    ("scale_out", False, True, "scale_out"),
    ("warm", False, False, "warm"),
    ("warm", False, True, "unverified"),
])
def test_cold_categories_require_evidence_not_waiting(reason, zero, new_boot, want):
    assert classify_cold(ColdEvidence(
        idle_seconds=3600, zero_confirmed=zero,
        new_boot_confirmed=new_boot, reason=reason)) == want


@pytest.mark.parametrize("idle", [-1, float("inf"), float("nan")])
def test_invalid_idle_interval_is_rejected(idle):
    with pytest.raises(ValueError):
        ColdEvidence(idle_seconds=idle, zero_confirmed=False,
                     new_boot_confirmed=False, reason="idle")

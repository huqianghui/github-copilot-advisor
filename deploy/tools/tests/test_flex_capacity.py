from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from advisor_deploy.config import SUBSCRIPTION
from advisor_deploy.operations import require_capacity
from advisor_deploy.render import digest


def python_flex_record(version="3.12"):
    return {
        "name": "python", "version": version,
        "sku": {
            "skuCode": "FC1",
            "functionAppConfigProperties": {"runtime": {"name": "python", "version": version}},
            "instanceMemoryMB": [
                {"size": "512", "isDefault": False},
                {"size": "2048", "isDefault": True},
                {"size": "4096", "isDefault": False},
            ],
            "maximumInstanceCount": {
                "defaultValue": 100, "lowestMaximumInstanceCount": 1,
                "highestMaximumInstanceCount": 1000,
            },
        },
    }


class RuntimeMetadata:
    def __init__(self, records=None):
        self.records = [python_flex_record()] if records is None else records

    def run(self, arguments, phase):
        assert arguments[:2] == ["functionapp", "list-flexconsumption-runtimes"]
        assert phase == "flex_runtime"
        return deepcopy(self.records)


def capacity_plan(remaining_mb):
    evidence = {
        "subscription_id": SUBSCRIPTION, "location": "southeastasia",
        "kind": "flex-regional-memory", "limit_mb": remaining_mb + 8192, "used_mb": 8192,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "Flex Consumption Quota diagnostic",
    }
    plan = SimpleNamespace(target="functions", config=SimpleNamespace(parameters={
        "maxInstances": 40, "instanceMemoryMB": 512,
    }), capacity_evidence=evidence)
    return plan, digest(evidence)


@pytest.mark.parametrize("remaining_mb", [40 * 512, 2 * 40 * 512, 3 * 40 * 512 - 1])
def test_flex_capacity_requires_all_three_function_scale_groups(remaining_mb):
    plan, approval = capacity_plan(remaining_mb)
    with pytest.raises(ValueError, match="flex_capacity_insufficient_or_stale"):
        require_capacity(plan, RuntimeMetadata(), approval)


def test_flex_capacity_accepts_reviewed_headroom_for_three_groups():
    plan, approval = capacity_plan(3 * 40 * 512)
    require_capacity(plan, RuntimeMetadata(), approval)


def test_flex_capacity_never_substitutes_published_default_for_evidence():
    plan, _ = capacity_plan(0)
    plan.capacity_evidence = None
    with pytest.raises(ValueError, match="flex_capacity_evidence_unconfirmed"):
        require_capacity(plan, RuntimeMetadata(), None)


@pytest.mark.parametrize("maximum", [1, 40, 1000])
@pytest.mark.parametrize("memory", [512, 2048, 4096])
def test_live_cli_shape_admits_supported_memory_and_instance_boundaries(maximum, memory):
    plan, approval = capacity_plan(3 * maximum * memory)
    plan.config.parameters.update(maxInstances=maximum, instanceMemoryMB=memory)
    require_capacity(plan, RuntimeMetadata(), approval)


@pytest.mark.parametrize("maximum", [0, 1001])
def test_requested_instance_count_must_stay_inside_selected_record(maximum):
    plan, approval = capacity_plan(10_000_000)
    plan.config.parameters["maxInstances"] = maximum
    with pytest.raises(ValueError, match="flex_runtime_capacity_unverified"):
        require_capacity(plan, RuntimeMetadata(), approval)


def test_requested_memory_must_be_supported_by_same_fc1_runtime():
    plan, approval = capacity_plan(10_000_000)
    plan.config.parameters["instanceMemoryMB"] = 1024
    with pytest.raises(ValueError, match="flex_runtime_memory_unsupported"):
        require_capacity(plan, RuntimeMetadata(), approval)


def test_memory_cannot_be_borrowed_from_another_runtime_version():
    plan, approval = capacity_plan(10_000_000)
    current = python_flex_record()
    current["sku"]["instanceMemoryMB"] = [{"size": "2048", "isDefault": True}]
    with pytest.raises(ValueError, match="flex_runtime_memory_unsupported"):
        require_capacity(plan, RuntimeMetadata([python_flex_record("3.13"), current]), approval)


def test_unrelated_language_record_cannot_supply_python_capabilities():
    plan, approval = capacity_plan(10_000_000)
    other = python_flex_record()
    other["name"] = "node"
    other["sku"]["functionAppConfigProperties"]["runtime"]["name"] = "node"
    with pytest.raises(ValueError, match="flex_runtime_capacity_unverified"):
        require_capacity(plan, RuntimeMetadata([other]), approval)


def test_caps_cannot_come_from_another_version_or_nested_github_metadata():
    plan, approval = capacity_plan(10_000_000)
    current = python_flex_record()
    current["sku"]["maximumInstanceCount"].update(defaultValue=10, highestMaximumInstanceCount=20)
    current["githubActionMetadata"] = {
        "runtimeVersion": "3.12", "lowestMaximumInstanceCount": 1, "highestMaximumInstanceCount": 1000,
    }
    other = python_flex_record("3.13")
    with pytest.raises(ValueError, match="flex_runtime_capacity_unverified"):
        require_capacity(plan, RuntimeMetadata([other, current]), approval)


def test_nested_runtime_is_not_a_substitute_for_top_level_python312_record():
    plan, approval = capacity_plan(10_000_000)
    other = python_flex_record("3.13")
    other["metadata"] = {"version": "3.12", "lowestMaximumInstanceCount": 1, "highestMaximumInstanceCount": 1000}
    with pytest.raises(ValueError, match="flex_runtime_capacity_unverified"):
        require_capacity(plan, RuntimeMetadata([other]), approval)


@pytest.mark.parametrize("change", ["name", "version", "sku"])
def test_runtime_name_version_and_fc1_sku_must_agree(change):
    plan, approval = capacity_plan(10_000_000)
    record = python_flex_record()
    if change == "name":
        record["sku"]["functionAppConfigProperties"]["runtime"]["name"] = "node"
    elif change == "version":
        record["sku"]["functionAppConfigProperties"]["runtime"]["version"] = "3.13"
    else:
        record["sku"]["skuCode"] = "Y1"
    with pytest.raises(ValueError, match="flex_runtime_(metadata_inconsistent|capacity_unverified)"):
        require_capacity(plan, RuntimeMetadata([record]), approval)


@pytest.mark.parametrize(("path", "value"), [
    (("sku",), None),
    (("sku", "maximumInstanceCount"), []),
    (("sku", "maximumInstanceCount", "lowestMaximumInstanceCount"), True),
    (("sku", "maximumInstanceCount", "highestMaximumInstanceCount"), "1000"),
    (("sku", "maximumInstanceCount", "defaultValue"), 100.0),
    (("sku", "instanceMemoryMB"), {"size": "512"}),
    (("sku", "instanceMemoryMB", 0, "size"), 512),
    (("sku", "instanceMemoryMB", 0, "size"), "512.0"),
    (("sku", "instanceMemoryMB", 0, "isDefault"), "false"),
    (("sku", "functionAppConfigProperties", "runtime"), "python3.12"),
])
def test_invalid_runtime_metadata_field_types_fail_with_bounded_code(path, value):
    plan, approval = capacity_plan(10_000_000)
    record = python_flex_record()
    parent = record
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(ValueError, match="flex_runtime_metadata_invalid"):
        require_capacity(plan, RuntimeMetadata([record]), approval)


def test_malformed_or_ambiguous_runtime_response_is_not_admitted():
    plan, approval = capacity_plan(10_000_000)
    for records in ({"value": [python_flex_record()]}, [None], [python_flex_record(), python_flex_record()]):
        with pytest.raises(ValueError, match="flex_runtime_metadata_(invalid|ambiguous)"):
            require_capacity(plan, RuntimeMetadata(records), approval)

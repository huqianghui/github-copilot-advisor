"""Channel-independent retry information consumed by durable workers."""
import pytest


@pytest.mark.parametrize("delay", [None, 0, 0.25, 3600, 10**100])
def test_valid_retry_delay_is_preserved_without_a_cap(delay):
    from advisor_shared.delivery import DeliveryFailure

    error = DeliveryFailure(
        definitely_not_sent=True, retryable=True, safe_code="delivery_throttled",
        retry_after_seconds=delay)
    assert error.retry_after_seconds == delay
    assert error.definitely_not_sent and error.retryable
    assert str(error) == "delivery_throttled"


@pytest.mark.parametrize("delay", [
    -1, -0.25, float("nan"), float("inf"), float("-inf"),
    True, False, "private-invalid-delay", object(),
])
def test_invalid_retry_delay_is_rejected_without_echoing_its_value(delay):
    from advisor_shared.delivery import DeliveryFailure

    with pytest.raises(ValueError) as caught:
        DeliveryFailure(
            definitely_not_sent=True, retryable=True, safe_code="delivery_throttled",
            retry_after_seconds=delay)
    assert str(caught.value) == "retry_after_seconds must be a finite nonnegative number"


def test_ambiguous_delivery_remains_nonretryable_even_with_provider_delay():
    from advisor_shared.delivery import DeliveryFailure

    error = DeliveryFailure(
        definitely_not_sent=False, retryable=True, safe_code="delivery_unknown",
        retry_after_seconds=300)
    assert not error.definitely_not_sent
    assert not error.retryable
    assert error.retry_after_seconds == 300


def test_legacy_failure_constructor_does_not_require_retry_metadata():
    from advisor_shared.delivery import DeliveryFailure

    error = DeliveryFailure(
        definitely_not_sent=True, retryable=False, safe_code="invalid_payload")
    assert error.retry_after_seconds is None
    assert not error.retryable

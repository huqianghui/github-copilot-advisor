"""Channel-independent delivery failure contract for durable workers."""
import math


class DeliveryFailure(Exception):
    """Only definitely-unsent, retryable failures permit automatic send retry.

    ``safe_code`` is a fixed diagnostic code, never an upstream message or URL.
    Preparation failures also use this contract and are always definitely unsent.
    ``retry_after_seconds`` is an optional finite, nonnegative provider delay.
    It is not capped and never makes an uncertain delivery retryable.
    """

    def __init__(
        self, *, definitely_not_sent: bool, retryable: bool, safe_code: str,
        retry_after_seconds: float | None = None,
    ):
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or retry_after_seconds < 0
            or isinstance(retry_after_seconds, float)
            and not math.isfinite(retry_after_seconds)
        ):
            raise ValueError(
                "retry_after_seconds must be a finite nonnegative number")
        self.definitely_not_sent = definitely_not_sent
        self.retryable = retryable and definitely_not_sent
        self.safe_code = safe_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(safe_code)

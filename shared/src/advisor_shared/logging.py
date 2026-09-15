"""Console logging configuration shared by application entry points."""
import json
import logging
import os
import sys
from datetime import datetime, timezone

from advisor_shared.telemetry import current_span, current_trace


class TelemetryFormatter(logging.Formatter):
    def __init__(self, output_format: str = "json"):
        super().__init__()
        self.output_format = output_format

    def format(self, record: logging.LogRecord) -> str:
        trace, span = current_trace(), current_span()
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": trace.trace_id if trace else None,
            "span_id": span.span_id if span else None,
        }
        payload.update(getattr(record, "telemetry", {}))
        if record.exc_info and record.exc_info[0]:
            payload["error_type"] = record.exc_info[0].__name__
        if self.output_format == "json":
            return json.dumps(payload, ensure_ascii=False)
        details = " ".join(
            f"{key}={value}" for key, value in payload.items()
            if key not in {"timestamp", "level", "logger", "message"}
            and value is not None)
        return (f"{payload['timestamp']} {record.levelname} {record.name} "
                f"{record.getMessage()} {details}")


def configure_logging() -> None:
    level = os.environ.get("ADVISOR_LOG_LEVEL", "INFO").upper()
    output_format = os.environ.get("ADVISOR_LOG_FORMAT", "json").lower()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"invalid ADVISOR_LOG_LEVEL: {level}")
    if output_format not in {"json", "text"}:
        raise ValueError(f"invalid ADVISOR_LOG_FORMAT: {output_format}")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console_handlers = [
        h for h in root.handlers if type(h) is logging.StreamHandler]
    if not console_handlers:
        handler = logging.StreamHandler(sys.stdout)
        root.addHandler(handler)
        console_handlers.append(handler)
    for handler in console_handlers:
        handler.setFormatter(TelemetryFormatter(output_format))
    for name in ("advisor_shared", "advisor_agent", "teams_adapter"):
        logging.getLogger(name).setLevel(level)
    # Application DEBUG must not enable payloads or query-bearing HTTP URLs.
    logging.getLogger("openai").setLevel(logging.INFO)
    for name in ("httpx", "httpx2", "httpcore", "azure"):
        logging.getLogger(name).setLevel(logging.WARNING)

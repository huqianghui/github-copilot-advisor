"""Lightweight authenticated admission; no model, downloader or typing imports."""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import ActivityHandler, OutboundHostValidator

from advisor_agent.durable.jobs import JobConflict, JobExpired, make_job_id
from advisor_shared.cloud import TaskPayload
from advisor_shared.telemetry import trace_scope, step
from teams_adapter.cloud.auth import (
    AuthRejected, AuthUnavailable, _safe_sdk_logs, validate_bot_request,
)
from teams_adapter.extract import build_conversation_key, should_respond

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _rejected(status, reason, *, error=None, headers=None):
    logger.warning("teams_ingress_rejected", extra={"telemetry": {
        "http_status": status, "reason": reason,
        "error_type": type(error).__name__ if error is not None else None,
    }})
    return HttpResult(status, headers=headers or {})


class _ActivityRequest:
    method = "POST"
    headers = {}

    def __init__(self, activity, claims):
        self.activity, self.claims = activity, claims

    async def json(self):
        return self.activity

    def get_claims_identity(self):
        return self.claims

    def get_path_param(self, name):
        return ""


async def _protocol_error(context, error):
    logger.warning("teams_protocol_failed error_type=%s", type(error).__name__)
    raise RuntimeError("teams_protocol_failed") from None


class CloudIngress:
    def __init__(self, jobs, dispatcher, *, bot_app_id, auth_config,
                 protocol_adapter, protocol_agent=None):
        self.jobs, self.dispatcher = jobs, dispatcher
        self.bot_app_id, self.auth_config = bot_app_id, auth_config
        self.protocol_adapter = protocol_adapter
        self.protocol_adapter.on_turn_error = _protocol_error
        self.protocol_agent = protocol_agent or ActivityHandler()
        self._hosts = OutboundHostValidator(
            enabled=True, include_default_microsoft_hosts=False,
            hosts=["botframework.com", "smba.trafficmanager.net",
                   "teams.microsoft.com", "teams.microsoft.us"])

    async def handle_http(self, method, headers, body) -> HttpResult:
        if method != "POST":
            return _rejected(405, "method_not_allowed", headers={"Allow": "POST"})
        try:
            claims = await validate_bot_request(headers, self.auth_config)
        except AuthRejected as error:
            return _rejected(401, "unauthorized", error=error)
        except AuthUnavailable as error:
            return _rejected(503, "authentication_unavailable", error=error)
        if isinstance(body, (bytes, str)):
            try:
                body = json.loads(body)
            except (ValueError, UnicodeError) as error:
                return _rejected(400, "invalid_json", error=error)
        return await self.handle_activity(body, claims)

    async def handle_activity(self, activity, claims) -> HttpResult:
        if not claims or claims.get_app_id() != self.bot_app_id:
            return _rejected(401, "unauthorized")
        with _safe_sdk_logs("ingress"), trace_scope(new=True) as trace:
            try:
                parsed = Activity.model_validate(activity)
                normalized = parsed.model_dump(
                    mode="json", by_alias=True, exclude_none=True)
                if (not parsed.conversation or not parsed.conversation.id
                        or parsed.channel_id != "msteams"):
                    raise ValueError("invalid activity")
                if parsed.service_url:
                    url = urlsplit(parsed.service_url)
                    if (url.scheme != "https" or not url.hostname or url.username
                            or url.password or url.query or url.fragment
                            or any(c.isspace() for c in parsed.service_url)
                            or not self._hosts.is_allowed(parsed.service_url)):
                        raise ValueError("invalid service url")
                    claimed_url = claims.get_claim_value("serviceurl")
                    if claimed_url is not None:
                        if not isinstance(claimed_url, str):
                            return _rejected(401, "service_url_claim_mismatch")
                        claimed_host = urlsplit(claimed_url).hostname
                        if not claimed_host or claimed_host.casefold() != url.hostname.casefold():
                            return _rejected(401, "service_url_claim_mismatch")
                if parsed.type != "message" or parsed.delivery_mode == "expectReplies":
                    return await self._protocol(normalized, claims)
                if (not parsed.service_url or not parsed.recipient or not parsed.recipient.id
                        or parsed.delivery_mode not in (None, "normal")
                        or normalized.get("from", {}).get("role") not in (None, "user")):
                    raise ValueError("invalid message")
                if not should_respond(normalized, parsed.recipient.id):
                    logger.info("teams_message_ignored", extra={"telemetry": {
                        "http_status": 202, "reason": "unaddressed_message"}})
                    return HttpResult(202)
                partition = build_conversation_key(normalized)
                payload = TaskPayload(
                    job_id=make_job_id(self.bot_app_id, partition, parsed.id),
                    partition_key=partition, activity=normalized,
                    accepted_at=datetime.now(timezone.utc),
                    trace_id=trace.trace_id, target="teams")
            except (ValueError, TypeError, AttributeError) as error:
                return _rejected(400, "invalid_activity", error=error)
            try:
                with step("teams.admit"):
                    receipt = await self.jobs.submit(payload)
                    await self.dispatcher.notify(partition, trace.trace_id)
                return HttpResult(202, {
                    "job_id": receipt.job_id, "duplicate": receipt.duplicate})
            except JobConflict as error:
                return _rejected(400, "admission_conflict", error=error)
            except JobExpired as error:
                return _rejected(400, "admission_expired", error=error)
            except Exception as error:
                return _rejected(503, "admission_unavailable", error=error)

    async def _protocol(self, activity, claims) -> HttpResult:
        try:
            response = await self.protocol_adapter.process_request(
                _ActivityRequest(activity, claims), self.protocol_agent)
            return HttpResult(response.status_code, response.body, response.headers or {})
        except Exception as error:
            logger.warning("teams_protocol_unavailable error_type=%s", type(error).__name__)
            return HttpResult(503)

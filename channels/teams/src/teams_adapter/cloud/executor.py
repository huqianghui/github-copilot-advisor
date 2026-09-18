"""Two-phase SDK turns: prepare privately, then deliver a saved checkpoint."""
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Literal

from aiohttp import ClientConnectionError, ClientResponseError
from microsoft_agents.activity import Activity, ResourceResponse
from microsoft_agents.hosting.core import (
    AgentApplication, ChannelServiceClientFactoryBase, MemoryStorage,
    TurnContext, TurnState,
)
from microsoft_agents.hosting.core.authorization import Connections

from advisor_agent.sessions import BufferedSessionStore
from advisor_shared.cloud import PreparedTurn, TaskPayload
from advisor_shared.events import AdvisorEvent
from advisor_shared.telemetry import step, trace_scope
from teams_adapter.bot import _activity_to_dict, _to_image_inputs
from teams_adapter.cloud.errors import DeliveryFailure
from teams_adapter.cloud.auth import ManagedCloudAdapter, _safe_sdk_logs
from teams_adapter.cloud.observability import observe_job
from teams_adapter.downloader import TeamsImageDownloader
from teams_adapter.extract import build_conversation_key, should_respond
from teams_adapter.processing import ReplyPreparation, prepare_reply
from teams_adapter.typing import NonBlockingTypingMiddleware

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from advisor_agent.factory import AdvisorComponents


def _failure(error: Exception, *, sending: bool,
             phase: str, acknowledged: bool = False) -> DeliveryFailure:
    if isinstance(error, DeliveryFailure) and not acknowledged:
        return error
    status = error.status if isinstance(error, ClientResponseError) else None
    if sending:
        if acknowledged:
            code, definite, retryable = "delivery_unknown", False, False
        elif status == 429:
            code, definite, retryable = "delivery_throttled", True, True
        elif status in {400, 401, 403, 404, 405, 413, 415, 422}:
            code, definite, retryable = "delivery_rejected", True, False
        else:
            code, definite, retryable = "delivery_unknown", False, False
    else:
        code = "preparation_failed" if phase == "prepare" else "delivery_setup_failed"
        definite = True
        retryable = (
            isinstance(error, (TimeoutError, ClientConnectionError))
            or status == 429 or status is not None and status >= 500)
    logger.warning("teams_cloud_failure phase=%s code=%s error_type=%s",
                   phase, code, type(error).__name__)
    return DeliveryFailure(
        definitely_not_sent=definite, retryable=retryable, safe_code=code,
        retry_after_seconds=_retry_after_seconds(error))


def _retry_after_seconds(error: Exception) -> float | None:
    if isinstance(error, DeliveryFailure):
        return error.retry_after_seconds
    if not isinstance(error, ClientResponseError) or error.headers is None:
        return None
    value = error.headers.get("Retry-After")
    if value is None:
        return None
    try:
        value = value.strip()
        if value.isascii() and value.isdecimal():
            return int(value)
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            raise ValueError("retry date requires a timezone")
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError, AttributeError):
        logger.warning("teams_retry_after_invalid")
        return None


def _validated_activity(
    payload: TaskPayload, *, expected_target: Literal["teams", "benchmark"] = "teams",
) -> Activity:
    try:
        if (payload.target != expected_target
                or build_conversation_key(payload.activity) != payload.partition_key):
            raise ValueError("identity mismatch")
        activity = Activity.model_validate(payload.activity)
        # Attachments bypass the connector and use a separate Teams HTTP downloader.
        if expected_target == "benchmark" and activity.attachments:
            raise ValueError("benchmark attachments are not supported")
        if (not activity.service_url or activity.channel_id != "msteams"
                or not activity.recipient or not activity.recipient.id
                or activity.delivery_mode not in {None, "normal"}
                or build_conversation_key(_activity_to_dict(activity))
                != payload.partition_key):
            raise ValueError("invalid continuation")
        return activity
    except Exception:
        raise DeliveryFailure(
            definitely_not_sent=True, retryable=False,
            safe_code="invalid_payload") from None


async def _raise_prepare_error(context: TurnContext, error: Exception) -> None:
    raise _failure(error, sending=False, phase="prepare") from None


async def _raise_send_error(context: TurnContext, error: Exception) -> None:
    # Send callbacks classify at the actual attempt boundary. Unexpected
    # middleware failures are conservatively uncertain.
    raise _failure(error, sending=True, phase="send") from None


class _PreparationAdapter(ManagedCloudAdapter):
    async def send_activities(
        self, context: TurnContext, activities: list[Activity],
    ) -> list[ResourceResponse]:
        if any(activity.type != "typing" for activity in activities):
            raise DeliveryFailure(
                definitely_not_sent=True, retryable=False,
                safe_code="prepare_send_blocked")
        return await super().send_activities(context, activities)


def _validated_reply(reply: dict) -> Activity:
    try:
        if reply.get("type") != "message":
            raise ValueError("not a final reply")
        return Activity.model_validate(reply)
    except Exception:
        raise DeliveryFailure(
            definitely_not_sent=True, retryable=False,
            safe_code="invalid_reply") from None


class TeamsTurnExecutor:
    """Reuse components/connection manager; own only lightweight turn state.

    The caller owns and closes ``components`` and ``connection_manager``.
    The optional SDK connector factory replaces the outbound transport, not the
    SDK pipeline. Generation and delivery use distinct adapters so saved-reply
    delivery cannot restart typing, download attachments or call the model.
    Benchmark reuse requires an explicit isolated connector and rejects attachments;
    target markers and partition identity are never rewritten.
    """

    def __init__(
        self, components: "AdvisorComponents", connection_manager: Connections,
        bot_app_id: str, *,
        channel_service_client_factory: ChannelServiceClientFactoryBase | None = None,
        expected_target: Literal["teams", "benchmark"] = "teams",
    ):
        if expected_target not in ("teams", "benchmark"):
            raise ValueError("expected_target must be teams or benchmark")
        if expected_target == "benchmark" and channel_service_client_factory is None:
            raise ValueError("benchmark requires an explicit connector factory")
        self._expected_target = expected_target
        self._components = components
        self._connection_manager = connection_manager
        self._bot_app_id = bot_app_id
        self._generation = _PreparationAdapter(
            connection_manager=connection_manager,
            channel_service_client_factory=channel_service_client_factory)
        self._generation.on_turn_error = _raise_prepare_error
        self._generation.use(NonBlockingTypingMiddleware())
        self._delivery = ManagedCloudAdapter(
            connection_manager=connection_manager,
            channel_service_client_factory=channel_service_client_factory)
        self._delivery.on_turn_error = _raise_send_error

    def validate(self, payload: TaskPayload) -> None:
        """Reject invalid identity before the worker reads private history."""
        with _safe_sdk_logs("validate"):
            try:
                with trace_scope(new=True, trace_id=payload.trace_id):
                    _validated_activity(payload, expected_target=self._expected_target)
                observe_job(payload)
            except ValueError:
                raise DeliveryFailure(
                    definitely_not_sent=True, retryable=False,
                    safe_code="invalid_payload") from None

    async def prepare(self, payload: TaskPayload,
                      history: list[dict]) -> PreparedTurn:
        with _safe_sdk_logs("prepare"), trace_scope(new=True, trace_id=payload.trace_id) as trace:
            activity = _validated_activity(payload, expected_target=self._expected_target)
            bot_id = activity.recipient.id
            if not should_respond(_activity_to_dict(activity), bot_id):
                return PreparedTurn(reply=None, history=[], outcome="ignored")
            try:
                # Validate identity before constructing a buffer from private data.
                sessions = BufferedSessionStore(
                    history, partition_key=payload.partition_key)
                events: list[AdvisorEvent] = []
                core = self._components.new_core(sessions, event_sink=events.append)
                app = AgentApplication[TurnState](
                    storage=MemoryStorage(), adapter=self._generation,
                    connection_manager=self._connection_manager,
                    start_typing_timer=False, remove_recipient_mention=False,
                    file_downloaders=[TeamsImageDownloader(self._connection_manager)])
                preparation = ReplyPreparation(None, "ignored")

                @app.activity("message")
                async def on_message(context: TurnContext, state: TurnState):
                    nonlocal preparation
                    with step("teams.message"):
                        preparation = await prepare_reply(
                            _activity_to_dict(context.activity), _to_image_inputs(state),
                            core, bot_id=bot_id, turn_id=payload.job_id)

                await self._generation.continue_conversation(
                    self._bot_app_id, activity, app.on_turn)
                outcome = preparation.outcome
                if outcome == "success" and (
                    any(event.error is not None for event in events)
                    or any(timing.status in {
                        "error", "timeout", "degraded", "cancelled", "not_configured"}
                           for timing in trace.timings)
                ):
                    outcome = "degraded"
                return PreparedTurn(
                    reply=preparation.reply, history=sessions.delta, outcome=outcome)
            except Exception as error:
                raise _failure(error, sending=False, phase="prepare") from None

    async def send(self, payload: TaskPayload,
                   prepared: PreparedTurn) -> str | None:
        sending = False
        acknowledged = False
        receipt: str | None = None
        with _safe_sdk_logs("send"), trace_scope(new=True, trace_id=payload.trace_id):
            activity = _validated_activity(payload, expected_target=self._expected_target)
            if prepared.reply is None:
                return None
            try:
                reply = _validated_reply(prepared.reply)

                async def deliver(context: TurnContext):
                    nonlocal receipt, sending, acknowledged
                    with step("teams.send"):
                        sending = True
                        result = await context.send_activity(reply)
                        acknowledged = True
                        receipt = result.id or None

                await self._delivery.continue_conversation(
                    self._bot_app_id, activity, deliver)
                return receipt
            except Exception as error:
                raise _failure(error, sending=sending, phase="send",
                               acknowledged=acknowledged) from None

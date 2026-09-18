"""One-process observer: registration, HTTP callback and timing share one clock."""
import asyncio
from dataclasses import dataclass, field, replace
import time

from aiohttp import web
from advisor_deploy.bench.models import Outcome, Sample
from advisor_deploy.bench.sink import EvidenceEvent, EvidenceFailure, EvidenceSample
from advisor_shared.benchmark import BenchmarkReply, SafeTelemetry
from advisor_shared.entra_api import AuthRejected, AuthUnavailable, EntraApiValidator


@dataclass
class Pending:
    request_id: str
    case_id: str
    started_s: float
    future: asyncio.Future
    job_id: str | None = None
    ack_ms: float | None = None
    sample: Sample | None = None
    telemetry: SafeTelemetry | None = None
    duplicates: int = 0
    late: int = 0
    typing: int = 0
    deadline_seconds: float | None = None
    validation_deadlines_s: dict[object, float] = field(default_factory=dict, repr=False)
    observer_processing_ms: float | None = None
    evidence_future: asyncio.Future | None = None

    @property
    def validating_until_s(self) -> float:
        return max(self.validation_deadlines_s.values(), default=0.)


@dataclass(frozen=True)
class ObservedReply:
    run_id: str
    request_id: str
    job_id: str
    event: str
    outcome: Outcome | None
    telemetry: SafeTelemetry


class ReplyReceiver:
    def __init__(self, *, operator_validator: EntraApiValidator | None = None,
                 callback_validator: EntraApiValidator | None = None,
                 local_test: bool = False, auth_timeout_seconds: float = 10.):
        if not local_test and (operator_validator is None or callback_validator is None):
            raise ValueError("separate operator and callback validators are required")
        self.operator_validator = operator_validator
        self.callback_validator = callback_validator
        self.local_test = local_test
        if not 0 < auth_timeout_seconds <= 10:
            raise ValueError("observer authentication timeout must be bounded")
        self.auth_timeout_seconds = auth_timeout_seconds
        self.start_requested = asyncio.Event()
        self.origins: dict[str, float] = {}
        self.entries: dict[tuple[str, str], Pending] = {}
        self.control_status = "waiting"
        self.evidence_sink = None

    def begin_run(self, run_id: str) -> None:
        if run_id in self.origins:
            raise ValueError("run IDs cannot be reused")
        self.origins[run_id] = time.perf_counter()

    def elapsed(self, run_id: str) -> float:
        return time.perf_counter() - self.origins[run_id]

    def register(self, run_id: str, request_id: str, case_id: str, *,
                 deadline_seconds: float | None = None) -> Pending:
        key = run_id, request_id
        if key in self.entries:
            raise ValueError("request ID already registered")
        entry = Pending(request_id, case_id, self.elapsed(run_id),
                        asyncio.get_running_loop().create_future(),
                        deadline_seconds=deadline_seconds)
        self.entries[key] = entry
        if self.evidence_sink:
            self.evidence_sink.emit(EvidenceEvent(
                run_id=run_id, request_id=request_id, case_id=case_id,
                kind="request_started", elapsed_s=entry.started_s))
        return entry

    def acknowledge(self, run_id: str, request_id: str, job_id: str) -> None:
        entry = self.entries[run_id, request_id]
        if entry.job_id is not None and entry.job_id != job_id:
            raise ValueError("job correlation conflict")
        entry.job_id = job_id
        if entry.ack_ms is None:
            entry.ack_ms = (self.elapsed(run_id) - entry.started_s) * 1000
            if entry.sample is not None:
                entry.sample = replace(entry.sample, ack_ms=entry.ack_ms)
            if self.evidence_sink:
                self.evidence_sink.emit(EvidenceEvent(
                    run_id=run_id, request_id=request_id, job_id=job_id,
                    kind="ack", elapsed_s=self.elapsed(run_id)))

    def finish(self, run_id: str, request_id: str, status: Outcome, *,
               finished_s: float | None = None) -> None:
        entry = self.entries[run_id, request_id]
        if entry.sample is not None:
            return
        entry.sample = Sample(
            entry.request_id, entry.case_id, status, entry.started_s,
            None if status == "censored" else (
                self.elapsed(run_id) if finished_s is None else finished_s), entry.ack_ms)
        def complete(_=None):
            if not entry.future.done():
                entry.future.set_result(entry.sample)

        if self.evidence_sink:
            try:
                entry.evidence_future = self.evidence_sink.emit(EvidenceEvent(
                    run_id=run_id, request_id=request_id, job_id=entry.job_id,
                    kind="censored" if status == "censored" else "final",
                    sample=EvidenceSample(**{
                        "request_id": entry.sample.request_id, "case_id": entry.sample.case_id,
                        "status": entry.sample.status, "started_s": entry.sample.started_s,
                        "finished_s": entry.sample.finished_s, "ack_ms": entry.sample.ack_ms}),
                    telemetry=entry.telemetry, observer_processing_ms=entry.observer_processing_ms))
                entry.evidence_future.add_done_callback(complete)
                return
            except EvidenceFailure:
                pass
        complete()

    def censor(self, run_id: str, request_id: str) -> None:
        self.finish(run_id, request_id, "censored")

    def samples(self, run_id: str) -> list[Sample]:
        return [entry.sample for (run, _), entry in self.entries.items()
                if run == run_id and entry.sample is not None]

    def deadline_due(self, run_id: str, request_id: str) -> bool:
        entry = self.entries[run_id, request_id]
        return (entry.sample is None and entry.deadline_seconds is not None
                and self.elapsed(run_id) >= max(
                    entry.started_s + entry.deadline_seconds, entry.validating_until_s))

    async def wait_for_terminal(self, run_id: str, request_id: str) -> Sample:
        entry = self.entries[run_id, request_id]
        while not entry.future.done():
            if entry.deadline_seconds is None or entry.sample is not None:
                return await asyncio.shield(entry.future)
            remaining = max(entry.started_s + entry.deadline_seconds,
                            entry.validating_until_s) - self.elapsed(run_id)
            if remaining <= 0:
                self.censor(run_id, request_id)
                break
            try:
                return await asyncio.wait_for(asyncio.shield(entry.future), remaining)
            except TimeoutError:
                continue
        return await asyncio.shield(entry.future)

    def observe(self, reply: ObservedReply, *, received_at: float | None = None) -> str:
        entry = self.entries.get((reply.run_id, reply.request_id))
        if entry is None:
            raise KeyError("unregistered request")
        finished_s = (self.elapsed(reply.run_id) if received_at is None
                      else received_at - self.origins[reply.run_id])
        if finished_s < entry.started_s:
            raise ValueError("callback reception preceded request registration")
        if entry.job_id is not None and entry.job_id != reply.job_id:
            raise ValueError("job correlation conflict")
        entry.job_id = reply.job_id
        if (entry.sample is None and entry.deadline_seconds is not None
                and finished_s - entry.started_s >= entry.deadline_seconds):
            self.censor(reply.run_id, reply.request_id)
        if entry.sample is not None:
            if entry.sample.status == "censored":
                entry.late += 1
                return "late"
            if reply.event == "final" and reply.outcome != entry.sample.status:
                raise ValueError("conflicting duplicate outcome")
            entry.duplicates += 1
            return "duplicate"
        if reply.event == "typing":
            entry.typing += 1
            return "typing"
        entry.telemetry = reply.telemetry
        self.finish(reply.run_id, reply.request_id, reply.outcome, finished_s=finished_s)
        return "completed"

    async def _authorize(self, request: web.Request, *, operator: bool) -> None:
        if self.local_test:
            # This is an explicit loopback-only test boundary, never cloud authentication.
            if request.remote not in {"127.0.0.1", "::1"}:
                raise web.HTTPForbidden()
            return
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise web.HTTPUnauthorized()
        validator = self.operator_validator if operator else self.callback_validator
        try:
            await asyncio.wait_for(validator.verify(header[7:]), self.auth_timeout_seconds)
        except AuthRejected:
            raise web.HTTPUnauthorized() from None
        except (AuthUnavailable, TimeoutError):
            raise web.HTTPServiceUnavailable() from None

    async def _reply(self, request: web.Request) -> web.Response:
        limit = 256 * 1024
        chunks, size = [], 0
        try:
            async with asyncio.timeout(5):
                async for chunk in request.content.iter_chunked(16384):
                    size += len(chunk)
                    if size > limit:
                        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=size)
                    chunks.append(chunk)
        except TimeoutError:
            raise web.HTTPRequestTimeout() from None
        received_at = time.perf_counter()
        try:
            wire = BenchmarkReply.model_validate_json(b"".join(chunks))
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid_reply"}, status=400)
        reply = ObservedReply(wire.run_id, wire.request_id, wire.job_id,
                              wire.event, wire.outcome, wire.telemetry)
        del wire, chunks
        entry = self.entries.get((reply.run_id, reply.request_id))
        validation_id = object()
        if entry is not None and entry.deadline_seconds is not None and entry.sample is None:
            received_s = received_at - self.origins[reply.run_id]
            if entry.started_s <= received_s < entry.started_s + entry.deadline_seconds:
                entry.validation_deadlines_s[validation_id] = received_s + self.auth_timeout_seconds
        try:
            await self._authorize(request, operator=False)
        finally:
            if entry is not None:
                entry.validation_deadlines_s.pop(validation_id, None)
        if entry is not None and entry.sample is None and reply.event == "final":
            entry.observer_processing_ms = (time.perf_counter() - received_at) * 1000
        try:
            disposition = self.observe(reply, received_at=received_at)
            if entry is not None and entry.evidence_future is not None:
                await asyncio.shield(entry.evidence_future)
            if self.evidence_sink and self.evidence_sink.failed:
                raise EvidenceFailure()
        except EvidenceFailure:
            return web.json_response({"error": "observer_evidence_failure"}, status=503)
        except KeyError:
            return web.json_response({"error": "unknown_request"}, status=404)
        except ValueError:
            return web.json_response({"error": "invalid_or_conflicting_reply"}, status=409)
        return web.json_response({"disposition": disposition})

    async def _start(self, request: web.Request) -> web.Response:
        await self._authorize(request, operator=True)
        try:
            if await request.json() != {}:
                raise ValueError()
        except (ValueError, TypeError):
            return web.json_response({"error": "configured_plan_only"}, status=400)
        if self.start_requested.is_set():
            return web.json_response({"status": self.control_status}, status=409)
        self.start_requested.set()
        self.control_status = "starting"
        return web.json_response({"status": "starting"}, status=202)

    async def _status(self, request: web.Request) -> web.Response:
        await self._authorize(request, operator=True)
        return web.json_response({"status": self.control_status})

    def application(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_post("/bench/replies", self._reply)
        app.router.add_post("/bench/control/start", self._start)
        app.router.add_get("/bench/control/status", self._status)
        return app

"""Bounded, payload-free observer evidence. Storage failures are never target failures."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import re
import time
from typing import Literal

from azure.storage.blob.aio import ContainerClient
from pydantic import Field, model_validator

from advisor_deploy.bench.models import Outcome, Sample
from advisor_shared.benchmark import Identifier, SafeTelemetry, WireModel


class EvidenceFailure(RuntimeError):
    def __init__(self):
        super().__init__("observer evidence persistence failed")


class EvidenceSample(WireModel):
    request_id: Identifier
    case_id: Identifier
    status: Outcome
    started_s: float = Field(ge=0)
    finished_s: float | None = Field(default=None, ge=0)
    ack_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_sample(self):
        Sample(**self.model_dump())
        return self


class EvidenceEvent(WireModel):
    run_id: Identifier
    kind: Literal[
        "run_started", "request_intent", "request_started", "not_submitted", "ack", "final", "censored",
    ]
    request_id: Identifier | None = None
    case_id: Identifier | None = None
    job_id: Identifier | None = None
    elapsed_s: float | None = Field(default=None, ge=0)
    sample: EvidenceSample | None = None
    telemetry: SafeTelemetry | None = None
    observer_processing_ms: float | None = Field(default=None, ge=0)


class BlobEvidenceStore:
    """Uses an already-provisioned private container. Never creates Azure resources."""
    def __init__(self, container: ContainerClient):
        self.container = container

    async def validate(self):
        properties = await self.container.get_container_properties(logging_enable=False)
        if properties.get("public_access"):
            raise EvidenceFailure()

    async def write(self, name: str, data: bytes, *, overwrite: bool):
        await self.container.upload_blob(
            name, data, overwrite=overwrite, max_concurrency=1, logging_enable=False)


class FileEvidenceStore:
    def __init__(self, root: Path):
        self.root = root

    async def validate(self):
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)

    async def write(self, name: str, data: bytes, *, overwrite: bool):
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/(?:events/\d+|checkpoint|final)\.json", name
        ):
            raise EvidenceFailure()
        path = self.root.joinpath(*name.split("/"))

        def write():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not overwrite and path.exists():
                raise EvidenceFailure()
            partial = path.with_suffix(".partial")
            partial.write_bytes(data)
            partial.replace(path)

        await asyncio.to_thread(write)


class EvidenceSink:
    def __init__(self, store, *, max_pending: int = 256, operation_timeout_seconds: float = 10.,
                 flush_timeout_seconds: float = 30., checkpoint_every: int = 10,
                 max_record_bytes: int = 32 * 1024 * 1024):
        if (not 1 <= max_pending <= 1024 or not 0 < operation_timeout_seconds <= 30
                or not 0 < flush_timeout_seconds <= 60 or not 1 <= checkpoint_every <= 100
                or not 1024 <= max_record_bytes <= 64 * 1024 * 1024):
            raise ValueError("invalid observer evidence bounds")
        self.store = store
        self.queue = asyncio.Queue(maxsize=max_pending)
        self.operation_timeout_seconds = operation_timeout_seconds
        self.flush_timeout_seconds = flush_timeout_seconds
        self.checkpoint_every = checkpoint_every
        self.max_record_bytes = max_record_bytes
        self.failed = False
        self.states = {}
        self.worker = None
        self.operations = 0
        self.io_seconds = 0.
        self.max_io_seconds = 0.

    async def __aenter__(self):
        try:
            await asyncio.wait_for(self.store.validate(), self.operation_timeout_seconds)
        except Exception:
            self.failed = True
            raise EvidenceFailure() from None
        self.worker = asyncio.create_task(self._consume())
        return self

    async def __aexit__(self, *args):
        try:
            await self.flush()
        except EvidenceFailure:
            pass
        finally:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    async def _write(self, name: str, data: dict, *, overwrite=True):
        started = time.perf_counter()
        try:
            raw = json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(raw) > self.max_record_bytes:
                raise EvidenceFailure()
            await asyncio.wait_for(
                self.store.write(name, raw, overwrite=overwrite), self.operation_timeout_seconds)
        except Exception:
            self.failed = True
            raise EvidenceFailure() from None
        finally:
            duration = time.perf_counter() - started
            self.operations += 1
            self.io_seconds += duration
            self.max_io_seconds = max(self.max_io_seconds, duration)

    async def start_run(self, run_id, plan):
        if self.failed or run_id in self.states:
            raise EvidenceFailure()
        # The full validated plan contains only credentials-free configuration.
        self.states[run_id] = dict(
            run_id=run_id, state="running", sequence=0, pending={}, terminal_count=0,
            plan=plan.model_dump(), observer_clock_id=run_id,
            restart_policy="never_resume_monotonic_clock")
        await self.record(EvidenceEvent(run_id=run_id, kind="run_started"))

    def emit(self, event: EvidenceEvent) -> asyncio.Future:
        if self.failed or event.run_id not in self.states:
            raise EvidenceFailure()
        future = asyncio.get_running_loop().create_future()
        # Unawaited ACK/start records must not emit unhandled-exception warnings.
        future.add_done_callback(lambda item: None if item.cancelled() else item.exception())
        try:
            self.queue.put_nowait((event, future))
        except asyncio.QueueFull:
            self.failed = True
            raise EvidenceFailure() from None
        return future

    async def record(self, event: EvidenceEvent):
        await asyncio.shield(self.emit(event))

    async def _consume(self):
        while True:
            event, future = await self.queue.get()
            try:
                state = self.states[event.run_id]
                state["sequence"] += 1
                sequence = state["sequence"]
                await self._write(
                    f"{event.run_id}/events/{sequence:08d}.json",
                    {"sequence": sequence, **event.model_dump(exclude_none=True)}, overwrite=False)
                if event.kind == "request_intent":
                    state["pending"][event.request_id] = {
                        "request_id": event.request_id, "case_id": event.case_id, "started_s": None}
                elif event.kind == "request_started":
                    state["pending"][event.request_id]["started_s"] = event.elapsed_s
                elif event.kind == "ack":
                    if event.request_id in state["pending"]:
                        state["pending"][event.request_id]["job_id"] = event.job_id
                elif event.kind in {"final", "censored"}:
                    state["pending"].pop(event.request_id, None)
                    state["terminal_count"] += 1
                elif event.kind == "not_submitted":
                    state["pending"].pop(event.request_id, None)
                if event.kind in {"run_started", "request_intent", "final", "censored"} or (
                    sequence % self.checkpoint_every == 0
                ):
                    await self._write(f"{event.run_id}/checkpoint.json", state)
                if not future.done():
                    future.set_result(None)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_exception(EvidenceFailure())
                raise
            except Exception:
                self.failed = True
                if not future.done():
                    future.set_exception(EvidenceFailure())
                self._fail_pending()
            finally:
                self.queue.task_done()

    def _fail_pending(self):
        while not self.queue.empty():
            _, future = self.queue.get_nowait()
            if not future.done():
                future.set_exception(EvidenceFailure())
            self.queue.task_done()

    async def flush(self):
        try:
            await asyncio.wait_for(self.queue.join(), self.flush_timeout_seconds)
        except TimeoutError:
            self.failed = True
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self._fail_pending()
            raise EvidenceFailure() from None
        if self.failed:
            raise EvidenceFailure()

    async def finalize(self, result):
        from advisor_deploy.bench.report import read_result

        try:
            await self.flush()
        except EvidenceFailure:
            pass

        def failure():
            result.stop_reason = "observer_evidence_failure"
            reasons = result.observations.setdefault("stop_reasons", [])
            if result.stop_reason not in reasons:
                reasons.append(result.stop_reason)

        if self.failed:
            failure()
        result.driver.update(evidence_operations=self.operations, evidence_io_seconds=self.io_seconds,
                             max_evidence_io_seconds=self.max_io_seconds)
        state = self.states[result.run_id]
        state["state"] = ("evidence_failure" if self.failed else
                          "interrupted" if result.stop_reason == "driver_interrupted" else "finished")
        # A final file includes censored work even if a preceding event write was unavailable.
        for sample in result.samples:
            state["pending"].pop(sample.request_id, None)
        result.observations["unstarted_intents"] = sum(
            item["started_s"] is None for item in state["pending"].values())
        state["stop_reason"] = result.stop_reason
        try:
            await self._write(f"{result.run_id}/checkpoint.json", state)
        except EvidenceFailure:
            failure()
        # final.json is the authoritative last commit, including any checkpoint failure.
        data = asdict(result)
        read_result(data)
        try:
            await self._write(f"{result.run_id}/final.json", data)
        except EvidenceFailure:
            failure()

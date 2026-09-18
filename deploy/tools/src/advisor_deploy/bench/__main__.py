"""Standalone Task15 CLI; does not add target application routes."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, TCPConnector, web

from advisor_deploy.bench.plan import AuthPlan, RunPlan, load_cases, load_plan
from advisor_deploy.bench.receiver import ReplyReceiver
from advisor_deploy.bench.report import read_result, render_report, write_result
from advisor_deploy.bench.runner import HttpSubmitter, RunResult, run_plan
from advisor_deploy.bench.sink import (
    BlobEvidenceStore, EvidenceFailure, EvidenceSink, FileEvidenceStore,
)
from advisor_shared.benchmark import (
    BenchmarkAck, BenchmarkReply, BenchmarkRequest, BenchmarkRunAccepted,
    BenchmarkRunRegistration, RenderedReply, SafeTelemetry, StageObservation,
)
from advisor_shared.entra_api import EntraApiValidator


@asynccontextmanager
async def serve(app: web.Application, *, host: str, port: int):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
        yield runner.addresses[0][1]
    finally:
        await runner.cleanup()


def local_synthetic_target(plan: RunPlan, session: ClientSession) -> web.Application:
    """Explicit loopback HTTP fixture, not the production Task11 implementation."""
    if not plan.local_test:
        raise ValueError("synthetic local boundary must never host a cloud target")
    registrations = {}
    accepted = {}
    tasks = set()

    async def register(request):
        try:
            run = BenchmarkRunRegistration.model_validate(await request.json())
            if run.scenario != "synthetic" or run.dataset_sha256 != plan.dataset_sha256:
                raise ValueError()
            if run.run_id in registrations and registrations[run.run_id] != run:
                return web.json_response({"error": "run_conflict"}, status=409)
            registrations[run.run_id] = run
        except ValueError:
            return web.json_response({"error": "invalid_run"}, status=400)
        accepted_run = BenchmarkRunAccepted(
            run_id=run.run_id, target_id=plan.expected_target_id,
            dataset_sha256=plan.dataset_sha256)
        return web.json_response(accepted_run.model_dump(), status=201)

    async def deliver(message, ack):
        started = time.perf_counter()
        await asyncio.sleep(1)
        duration_ms = (time.perf_counter() - started) * 1000
        reply = BenchmarkReply(
            run_id=message.run_id, request_id=message.request_id,
            job_id=ack.job_id, event="final", outcome="success",
            reply=RenderedReply(type="message", format="markdown",
                                text="[synthetic benchmark: 1000 ms; no model invocation]"),
            telemetry=SafeTelemetry(stages=[
                StageObservation(name="synthetic", duration_ms=duration_ms, outcome="success")]))
        async with session.post(plan.callback_url, json=reply.model_dump(),
                                allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError("local callback was not accepted")

    async def message(request):
        try:
            message = BenchmarkRequest.model_validate(await request.json())
            run = registrations[message.run_id]
            if (message.scenario != "synthetic" or message.case_id not in run.case_ids
                    or message.virtual_user >= run.max_concurrency
                    or datetime.fromisoformat(run.expires_at) <= datetime.now(timezone.utc)):
                raise ValueError()
            key = message.run_id, message.request_id
            if key in accepted:
                previous, ack = accepted[key]
                if previous != message:
                    return web.json_response({"error": "request_conflict"}, status=409)
            else:
                if sum(run_id == message.run_id for run_id, _ in accepted) >= run.max_requests:
                    return web.json_response({"error": "budget_exhausted"}, status=429,
                                             headers={"Retry-After": "60"})
                ack = BenchmarkAck(run_id=message.run_id, request_id=message.request_id,
                                   job_id="local-" + message.request_id)
                accepted[key] = message, ack
                task = asyncio.create_task(deliver(message, ack))
                tasks.add(task)
        except (ValueError, KeyError):
            return web.json_response({"error": "invalid_request"}, status=400)
        return web.json_response(ack.model_dump(), status=202)

    async def cleanup(app):
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post("/bench/runs", register)
    app.router.add_post("/bench/messages", message)
    app.on_cleanup.append(cleanup)
    return app


def configured_receiver(auth: AuthPlan, *, key_provider=None) -> ReplyReceiver:
    operator = EntraApiValidator(
        tenant_id=auth.tenant_id, audience=auth.audience,
        allowed_client_ids=auth.operator_client_ids,
        allowed_principal_ids=auth.operator_principal_ids, key_provider=key_provider)
    callback = EntraApiValidator(
        tenant_id=auth.tenant_id, audience=auth.audience,
        allowed_client_ids=auth.target_client_ids,
        allowed_principal_ids=auth.target_principal_ids, key_provider=key_provider)
    return ReplyReceiver(operator_validator=operator, callback_validator=callback)


async def execute(plan: RunPlan, *, plan_directory: Path, output: Path,
                  listen_port: int = 8080) -> list[RunResult]:
    load_cases(plan_directory / plan.cases_path, plan)
    if plan.zero_evidence_path:
        plan = RunPlan.model_validate(plan.model_dump() | {
            "zero_evidence_path": str((plan_directory / plan.zero_evidence_path).resolve())})

    def persist(result):
        write_result(result, output / f"{result.run_id}.json")

    if plan.local_test:
        receiver = ReplyReceiver(local_test=True)
        callback = urlsplit(plan.callback_url)
        target = urlsplit(plan.target_url)
        async with ClientSession(connector=TCPConnector(limit=0)) as session:
            async with serve(receiver.application(), host="127.0.0.1", port=callback.port or 0) as port:
                actual = plan.model_dump() | {"callback_url": f"http://127.0.0.1:{port}/bench/replies"}
                callback_plan = RunPlan.model_validate(actual)
                app = local_synthetic_target(callback_plan, session)
                async with serve(app, host="127.0.0.1", port=target.port or 0) as port:
                    actual["target_url"] = f"http://127.0.0.1:{port}"
                    configured = RunPlan.model_validate(actual)
                    async with EvidenceSink(FileEvidenceStore(output)) as evidence:
                        return await run_plan(
                            configured, HttpSubmitter(session, configured), receiver,
                            result_sink=persist, evidence_sink=evidence)

    auth = plan.auth
    receiver = configured_receiver(auth)
    from azure.identity.aio import ManagedIdentityCredential
    from azure.storage.blob.aio import ContainerClient

    results = plan.results
    configured_url = os.environ.get("ADVISOR_BLOB_SERVICE_URL")
    if configured_url and configured_url.rstrip("/") != results.account_url.rstrip("/"):
        raise ValueError("results storage differs from the deployed observer configuration")
    configured_container = os.environ.get("BENCHMARK_RESULTS_CONTAINER", "benchmark-results")
    if configured_container != results.container:
        raise ValueError("results container differs from the deployed observer configuration")

    async with ManagedIdentityCredential(client_id=auth.driver_client_id) as credential:
        async def token():
            return (await credential.get_token(auth.scope)).token

        # Prepare only driver identity. Never health-check or poll the target application.
        await token()
        async with ContainerClient(
            results.account_url, results.container, credential=credential, retry_total=0,
            connection_timeout=results.operation_timeout_seconds,
            read_timeout=results.operation_timeout_seconds,
        ) as container:
            async with EvidenceSink(
                BlobEvidenceStore(container), max_pending=results.max_pending_events,
                operation_timeout_seconds=results.operation_timeout_seconds,
                flush_timeout_seconds=results.flush_timeout_seconds,
                checkpoint_every=results.checkpoint_every,
                max_record_bytes=results.max_record_bytes,
            ) as evidence:
                async with ClientSession(connector=TCPConnector(limit=0)) as session:
                    async with serve(receiver.application(), host="0.0.0.0", port=listen_port):
                        await receiver.start_requested.wait()
                        receiver.control_status = "running"
                        try:
                            observed = await run_plan(
                                plan, HttpSubmitter(session, plan, token), receiver,
                                result_sink=persist, evidence_sink=evidence)
                            receiver.control_status = "finished"
                            return observed
                        except BaseException:
                            receiver.control_status = "stopped"
                            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bounded application-link benchmark observer")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--output", type=Path, default=Path("deploy") / "out" / "bench")
    run.add_argument("--allow-cloud", action="store_true")
    run.add_argument("--listen-port", type=int, default=int(os.environ.get("PORT", "8080")))
    report = commands.add_parser("report")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--stage-logs", type=Path,
                        help="Separate JSON list of strict benchmark_stage_completed DTOs")
    report.add_argument("--stage-log-window-start")
    report.add_argument("--stage-log-window-end")
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            from hashlib import sha256
            from advisor_deploy.bench.report import load_stage_logs

            protected = [args.input] + ([args.stage_logs] if args.stage_logs else [])
            if any(args.output.resolve() == path.resolve()
                   or args.output.exists() and args.output.samefile(path) for path in protected):
                raise ValueError("report output must not overwrite evidence")
            if not args.stage_logs and (
                args.stage_log_window_start is not None or args.stage_log_window_end is not None
            ):
                raise ValueError("stage-log window requires a stage-log input")
            source_bytes = args.input.read_bytes()
            result = read_result(json.loads(source_bytes.decode("utf-8")))
            imported = (load_stage_logs(
                args.stage_logs, window_start=args.stage_log_window_start,
                window_end=args.stage_log_window_end) if args.stage_logs else None)
            rendered = render_report(result, stage_logs=imported)
            rendered += (
                "\n## Raw result provenance\n\n"
                f"Source: [original raw result]({args.input.resolve().as_uri()}).\n"
                f"Raw-result SHA-256 (original bytes): `{sha256(source_bytes).hexdigest()}`.\n")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
            return 0
        plan = load_plan(args.plan)
        if not plan.local_test and not args.allow_cloud:
            raise ValueError("cloud execution requires --allow-cloud")
        results = asyncio.run(execute(
            plan, plan_directory=args.plan.parent, output=args.output,
            listen_port=args.listen_port))
        print(json.dumps({"runs": len(results),
                          "attempted": sum(len(result.samples) for result in results),
                          "stop_reasons": [result.stop_reason for result in results],
                          "local_test": plan.local_test}))
        return 0 if all(result.stop_reason == "target_reached" for result in results) else 2
    except KeyboardInterrupt:
        print("Benchmark interrupted; registered unfinished requests are censored.")
        return 130
    except EvidenceFailure:
        print("Observer evidence failure; stopped without a target-capacity conclusion.")
        return 2
    except Exception as error:
        # SDK/HTTP/config exceptions may contain credentials or payload fragments.
        print(f"Benchmark stopped ({type(error).__name__}); check configured gates and safe results.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

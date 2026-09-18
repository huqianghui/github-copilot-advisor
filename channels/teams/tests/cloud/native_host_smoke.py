"""Run real Core Tools plus isolated Azurite using synthetic local-only settings.

No .env loading, global installs or cloud resource calls. Both child process
trees are stopped by explicit PIDs. Evidence is written beneath deploy/out.
"""
import argparse
import asyncio
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import sysconfig
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from azure.core.credentials import AzureNamedKeyCredential
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.queue.aio import QueueServiceClient
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from aiohttp import web

ROOT = Path(__file__).parents[4]
APP_ID = "00000000-0000-0000-0000-000000000002"
TENANT = "00000000-0000-0000-0000-000000000001"
ACCOUNT = "nativesmoke"
KEY = base64.b64encode(b"offline-functions-host-smoke-only").decode()
BENCHMARK_KEY = "native-only-benchmark-key-32-characters"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(port, path, *, body=None, token=None, benchmark_key=None):
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if benchmark_key:
        headers["X-Benchmark-Key"] = benchmark_key
    req = Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers)
    try:
        with urlopen(req, timeout=120) as response:
            data = response.read()
            return response.status, json.loads(data) if data.startswith((b"{", b"[")) else data.decode()
    except HTTPError as error:
        return error.code, error.read().decode()


async def wait_until(predicate, *, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.5)
    raise TimeoutError("native smoke readiness/checkpoint deadline exceeded")


def stop_tree(process):
    if process.poll() is not None:
        return
    shell = shutil.which("pwsh") or shutil.which("powershell")
    code = """
    function Stop-OwnedTree([int]$ProcessId) {
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" |
          ForEach-Object { Stop-OwnedTree ([int]$_.ProcessId) }
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    Stop-OwnedTree %d
    """ % process.pid
    subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", code],
                   check=True, timeout=45, capture_output=True)
    process.wait(timeout=15)


async def smoke(args):
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("smoke output must remain in the project")
    app, bootstrap = (output / name for name in ("app", "bootstrap"))
    profile = ROOT / "deploy" / "out" / "task9-native-tools" / "host-profile"
    for folder in (app, bootstrap, profile, output / "scratch", output / "storage"):
        folder.mkdir(parents=True, exist_ok=True)
    for name in ("function_app.py", "host.json"):
        shutil.copy2(ROOT / "deploy" / "functions" / name, app / name)
    shutil.copy2(Path(__file__).with_name("native_host_bootstrap.py"), bootstrap / "sitecustomize.py")
    if sysconfig.get_platform() != "win-amd64":
        raise ValueError("this native harness requires the selected Windows x64 Python")
    worker_directory = bootstrap / "python-worker"
    worker_directory.mkdir(exist_ok=True)
    descriptor = json.loads(
        (args.func.resolve().parent / "workers" / "python" / "worker.config.json").read_text())
    descriptor["description"]["defaultExecutablePath"] = sys.executable
    descriptor["description"]["defaultWorkerPath"] = str(
        args.func.resolve().parent / "workers" / "python"
        / f"{sys.version_info.major}.{sys.version_info.minor}" / "WINDOWS" / "X64" / "worker.py")
    (worker_directory / "worker.config.json").write_text(json.dumps(descriptor))
    events = output / "events.jsonl"
    events.write_text("")
    ready_marker = output / "bootstrap-ready"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="native-offline-key", alg="RS256")
    public_file = output / "public-jwk.json"
    public_file.write_text(json.dumps(public))
    blob_port, queue_port, table_port, http_port = [free_port() for _ in range(4)]
    blob_url = f"http://127.0.0.1:{blob_port}/{ACCOUNT}"
    queue_url = f"http://127.0.0.1:{queue_port}/{ACCOUNT}"
    connection = (
        f"DefaultEndpointsProtocol=http;AccountName={ACCOUNT};AccountKey={KEY};"
        f"BlobEndpoint={blob_url};QueueEndpoint={queue_url};"
        f"TableEndpoint=http://127.0.0.1:{table_port}/{ACCOUNT}")
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT")
           if name in os.environ}
    env.update({
        "PATH": os.pathsep.join([
            str(Path(sys.executable).parent),
            str(Path(os.environ["SystemRoot"]) / "System32"),
            os.environ["SystemRoot"],
        ]),
        "USERPROFILE": str(profile), "HOME": str(profile),
        "APPDATA": str(profile / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
        "TEMP": str(output / "scratch"), "TMP": str(output / "scratch"),
        "FUNCTIONS_CORE_TOOLS_TELEMETRY_OPTOUT": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "AZURITE_ACCOUNTS": f"{ACCOUNT}:{KEY}",
        "HTTP_PROXY": "http://127.0.0.1:0", "HTTPS_PROXY": "http://127.0.0.1:0",
        "NO_PROXY": "127.0.0.1,localhost,::1",
    })
    if args.node_debug:
        env["NODE_DEBUG"] = "module"
    settings = {
        "FUNCTIONS_WORKER_RUNTIME": "python", "FUNCTIONS_WORKER_PROCESS_COUNT": "1",
        "AzureWebJobsStorage": connection, "AdvisorQueue": connection,
        "AzureWebJobsSecretStorageType": "files",
        "AzureFunctionsJobHost__extensionBundle__downloadPath": str(
            profile / ".azure-functions-core-tools" / "Functions" / "ExtensionBundles"
            / "Microsoft.Azure.Functions.ExtensionBundle"),
        "ADVISOR_HOST_SMOKE": "1", "SMOKE_EVENTS": str(events),
        "SMOKE_BOOTSTRAP_READY": str(ready_marker),
        "SMOKE_PUBLIC_JWK": str(public_file), "PYTHONPATH": str(bootstrap),
        "languageWorkers__python__defaultExecutablePath": sys.executable,
        "languageWorkers__python__workerDirectory": str(worker_directory),
        "VIRTUAL_ENV": str(Path(sys.executable).parents[1]),
        "ADVISOR_STORAGE_AUTH_MODE": "key", "ADVISOR_STORAGE_ACCOUNT_NAME": ACCOUNT,
        "ADVISOR_STORAGE_ACCOUNT_KEY": KEY,
        "ADVISOR_BLOB_SERVICE_URL": blob_url, "ADVISOR_QUEUE_SERVICE_URL": queue_url,
        "ADVISOR_QUEUE_NAME": "advisor-incoming", "ADVISOR_PROCESSING_MODE": "queued",
        "ADVISOR_MAINTENANCE_SCHEDULE": "0 */5 * * * *",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": APP_ID,
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": TENANT,
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET": "offline-only",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED": "False",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__VALIDATE_ISSUER": "True",
    }
    callback_runner = None
    benchmark_final = asyncio.Event()
    benchmark_replies = []
    if args.benchmark_cases is not None:
        from advisor_shared.benchmark import BenchmarkReply, load_dataset
        approved = load_dataset(args.benchmark_cases.resolve())
        installed_cases = app / "runtime-config" / "benchmark-cases.json"
        installed_cases.parent.mkdir(exist_ok=True)
        shutil.copy2(args.benchmark_cases.resolve(), installed_cases)

        async def receive_benchmark(req):
            if req.headers.get("X-Benchmark-Key") != BENCHMARK_KEY:
                return web.Response(status=401)
            reply = BenchmarkReply.model_validate_json(await req.read())
            benchmark_replies.append(reply)
            if reply.event == "final":
                benchmark_final.set()
            return web.json_response({
                "disposition": "typing" if reply.event == "typing" else "completed"})

        callback_app = web.Application()
        callback_app.router.add_post("/bench/replies", receive_benchmark)
        callback_runner = web.AppRunner(callback_app)
        await callback_runner.setup()
        callback_port = free_port()
        await web.TCPSite(callback_runner, "127.0.0.1", callback_port).start()
        settings.update({
            "BENCHMARK_ENABLED": "true", "BENCHMARK_TARGET_ID": "native-smoke-target",
            "BENCHMARK_CASES_PATH": str(installed_cases), "BENCHMARK_AUTH_MODE": "key",
            "BENCHMARK_API_KEY": BENCHMARK_KEY,
            "BENCHMARK_RECEIVER_URL": f"http://127.0.0.1:{callback_port}/bench/replies",
        })
    (app / "local.settings.json").write_text(json.dumps({"IsEncrypted": False, "Values": settings}))
    report = {"host_port": http_port, "offline_boundaries": ["JWKS", "model", "Teams connector"]}
    processes = []
    credential = AzureNamedKeyCredential(ACCOUNT, KEY)

    def checkpoint(phase):
        report["phase"] = phase
        report["child_pids"] = [process.pid for process in processes]
        (output / "report.json").write_text(json.dumps(report, indent=2))

    try:
        with ExitStack() as logs:
            probe = subprocess.run(
                [sys.executable, "-c",
                 "import asyncio; from teams_adapter.cloud.runtime import get_runtime; "
                 "asyncio.run(get_runtime().aclose())"],
                cwd=app, env={**env, **settings}, capture_output=True, text=True, timeout=180)
            if probe.returncode:
                (output / "configuration-probe.log").write_text(probe.stderr)
                raise RuntimeError("synthetic configuration probe failed")
            ready_marker.unlink(missing_ok=True)
            emulator_log = logs.enter_context((output / "azurite.log").open("w"))
            emulator = subprocess.Popen([
                shutil.which("node"), str(args.azurite.resolve()),
                "--location", str(output / "storage"), "--silent", "--disableTelemetry",
                "--skipApiVersionCheck", "--blobHost", "127.0.0.1", "--blobPort", str(blob_port),
                "--queueHost", "127.0.0.1", "--queuePort", str(queue_port),
                "--tableHost", "127.0.0.1", "--tablePort", str(table_port),
            ], cwd=output, env=env, stdout=emulator_log, stderr=subprocess.STDOUT)
            processes.append(emulator)
            checkpoint("azurite_start")

            async def emulator_ready():
                if emulator.poll() is not None:
                    raise RuntimeError("Azurite exited; inspect azurite.log")
                try:
                    for port in (blob_port, queue_port):
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            pass
                    return True
                except OSError:
                    return False

            await wait_until(emulator_ready, timeout=300)
            checkpoint("azurite_ready")
            async with BlobServiceClient(blob_url, credential=credential) as blobs, \
                    QueueServiceClient(queue_url, credential=credential) as queues:
                for name in ("advisor-jobs", "advisor-history"):
                    await blobs.create_container(name)
                for name in ("advisor-incoming", "advisor-incoming-poison"):
                    await queues.create_queue(name)
                host_log = logs.enter_context((output / "host.log").open("w"))
                host = subprocess.Popen([
                    str(args.func.resolve()), "start", "--port", str(http_port),
                    "--timeout", "300",
                ], cwd=app, env={**env, **settings}, stdout=host_log, stderr=subprocess.STDOUT)
                processes.append(host)
                checkpoint("functions_start")

                async def host_ready():
                    if host.poll() is not None:
                        raise RuntimeError("Functions host exited; inspect host.log")
                    try:
                        return request(http_port, "/healthz")[0] == 200
                    except (URLError, TimeoutError, OSError):
                        return False

                await wait_until(host_ready, timeout=300)
                checkpoint("functions_ready")
                assert ready_marker.is_file(), "offline I/O boundaries were not installed"
                status, discovered = request(http_port, "/admin/functions")
                assert status == 200, f"native discovery status {status}"
                names = sorted(function["name"] for function in discovered)
                assert {"messages", "healthz", "process_queue", "maintenance"} <= set(names)
                report["discovered_functions"] = names
                report["health_status"] = request(http_port, "/healthz")[0]
                report["unauthenticated_message_status"] = request(http_port, "/api/messages", body={})[0]
                assert report["unauthenticated_message_status"] == 401
                now = int(time.time())
                token = jwt.encode({
                    "iss": "https://api.botframework.com", "aud": APP_ID,
                    "iat": now - 10, "nbf": now - 10, "exp": now + 600,
                }, key, algorithm="RS256", headers={"kid": public["kid"]})
                activity = {
                    "type": "message", "id": "native-smoke-message", "text": "offline question",
                    "channelId": "msteams", "serviceUrl": "https://api.botframework.com",
                    "from": {"id": "29:native-smoke", "role": "user"},
                    "recipient": {"id": "28:native-smoke"},
                    "conversation": {"id": "native-chat", "conversationType": "groupChat"},
                    "channelData": {"tenant": {"id": TENANT}},
                }
                assert request(http_port, "/api/messages", body=activity, token=token)[0] == 202
                await asyncio.sleep(3)
                container = blobs.get_container_client("advisor-jobs")
                assert not [item async for item in container.list_blobs(name_starts_with="jobs/")]
                assert events.read_text() == ""
                report["unaddressed_message_persisted"] = False
                if args.benchmark_cases is not None:
                    from datetime import datetime, timedelta, timezone
                    from advisor_shared.benchmark import (
                        BenchmarkRequest, BenchmarkRunRegistration, dataset_digest)
                    case_id = approved["cases"][0]["case_id"]
                    registration = BenchmarkRunRegistration(
                        run_id="native-run", profile="warm", case_ids=[case_id], seed=11,
                        scenario="synthetic", history_mode="empty",
                        max_requests=1, max_concurrency=1,
                        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                        dataset_sha256=dataset_digest(approved)).model_dump(mode="json")
                    assert (await asyncio.to_thread(
                        request, http_port, "/bench/runs", body=registration, token=token))[0] == 401
                    assert (await asyncio.to_thread(
                        request, http_port, "/bench/runs", body=registration,
                        benchmark_key=BENCHMARK_KEY))[0] == 201
                    benchmark_message = BenchmarkRequest(
                        run_id="native-run", request_id="native-request", case_id=case_id,
                        virtual_user=0, scenario="synthetic").model_dump(mode="json")
                    status, benchmark_ack = await asyncio.to_thread(
                        request, http_port, "/bench/messages", body=benchmark_message,
                        benchmark_key=BENCHMARK_KEY)
                    assert status == 202
                    await asyncio.wait_for(benchmark_final.wait(), 90)
                    assert events.read_text() == "", "synthetic scenario initialized a model"
                    final = next(item for item in benchmark_replies if item.event == "final")
                    assert final.job_id == benchmark_ack["job_id"]
                    assert final.outcome == "success" and "synthetic" in final.reply.text
                    assert final.telemetry.model_rounds is None
                    assert all(not item.first_request for item in final.telemetry.boots)
                    report["benchmark"] = {
                        "registration_status": 201, "message_status": 202,
                        "bot_jwt_rejected": True, "final_outcome": final.outcome,
                        "model_calls": 0,
                        "host_boots": [item.model_dump() for item in final.telemetry.boots],
                    }
                else:
                    for route in ("runs", "messages"):
                        assert request(http_port, f"/bench/{route}", body={})[0] == 404
                    report["benchmark_disabled"] = True
                activity["conversation"]["conversationType"] = "personal"
                status, accepted = request(http_port, "/api/messages", body=activity, token=token)
                assert status == 202
                job_id = accepted["job_id"]

                async def completed():
                    from azure.core.exceptions import ResourceNotFoundError
                    try:
                        downloaded = await container.get_blob_client(
                            f"terminal-metadata/{job_id}.json").download_blob()
                        document = json.loads(await downloaded.readall())
                        return document["state"] == "completed"
                    except ResourceNotFoundError:
                        return False

                await wait_until(completed, timeout=90)
                records = [json.loads(line) for line in events.read_text().splitlines()]
                assert len([item for item in records if item["kind"] == "model_built"]) == 1
                assert len([item for item in records if item["kind"] == "model_call"]) == 1
                worker_observation = next(
                    item["host_telemetry"] for item in records if item["kind"] == "model_call")
                identity = next(
                    item["host_identity"] for item in records if item["kind"] == "model_call")
                if identity["instance_id"] is None:
                    assert worker_observation["boots"] == []
                else:
                    assert worker_observation["boots"][0]["group"] == "worker"
                    assert worker_observation["boots"][0]["first_request"] is False
                setup = "connection" if args.benchmark_cases is not None else "worker_init"
                assert len([stage for stage in worker_observation["stages"]
                            if stage["name"] == setup]) == 1
                assert worker_observation["input_tokens"] is None
                report["worker_host_evidence"] = worker_observation
                report["process_identity"] = identity
                replies = [item for item in records if item["kind"] == "reply"]
                assert len(replies) == 1 and replies[0]["text"] == "native smoke answer"
                status, duplicate = request(http_port, "/api/messages", body=activity, token=token)
                assert status == 202 and duplicate["duplicate"]
                await asyncio.sleep(3)
                assert events.read_text().splitlines() == [json.dumps(record) for record in records]
                report.update(queue_job_state="completed", model_calls=1, final_replies=1,
                              duplicate_suppressed=True, success=True)
    except Exception as error:
        report.update(success=False, error_type=type(error).__name__, error=str(error))
        raise
    finally:
        for process in reversed(processes):
            stop_tree(process)
        if callback_runner is not None:
            await callback_runner.cleanup()
        report["children_stopped"] = all(process.poll() is not None for process in processes)
        (output / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--func", type=Path, required=True)
    parser.add_argument("--azurite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node-debug", action="store_true")
    parser.add_argument("--benchmark-cases", type=Path)
    asyncio.run(smoke(parser.parse_args()))

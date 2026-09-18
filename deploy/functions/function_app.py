"""Native v2 bindings based on the official HTTP, Queue and Timer templates."""
import os

import azure.functions as func

from advisor_shared.logging import configure_logging
from teams_adapter.cloud.functions import (
    handle_benchmark_request, handle_function_health, handle_function_request,
    handle_maintenance, handle_queue_message,
)

configure_logging()
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="api/messages", methods=["POST"])
async def messages(req: func.HttpRequest) -> func.HttpResponse:
    return await handle_function_request(req)


@app.route(route="healthz", methods=["GET"])
async def healthz(req: func.HttpRequest) -> func.HttpResponse:
    return await handle_function_health(req)


@app.route(route="bench/runs", methods=["POST"])
async def benchmark_runs(req: func.HttpRequest) -> func.HttpResponse:
    return await handle_benchmark_request(req, operation="runs")


@app.route(route="bench/messages", methods=["POST"])
async def benchmark_messages(req: func.HttpRequest) -> func.HttpResponse:
    return await handle_benchmark_request(req, operation="messages")


@app.queue_trigger(arg_name="msg", queue_name="%ADVISOR_QUEUE_NAME%",
                   connection="AdvisorQueue")
async def process_queue(msg: func.QueueMessage) -> None:
    await handle_queue_message(msg.get_body())


@app.timer_trigger(
    schedule=("%ADVISOR_MAINTENANCE_SCHEDULE%"
              if os.environ.get("ADVISOR_MAINTENANCE_SCHEDULE") else "0 */5 * * * *"),
    arg_name="timer", run_on_startup=False, use_monitor=True)
async def maintenance(timer: func.TimerRequest) -> None:
    await handle_maintenance()

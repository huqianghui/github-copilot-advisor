import asyncio
import json
import logging

import pytest
from microsoft_agents.activity import Activity, ResourceResponse
from microsoft_agents.hosting.core import TurnContext

from test_bot import group_activity as group_stub, personal_activity as personal_stub


def sdk_activity(activity):
    return Activity.model_validate({
        **activity.model_dump(by_alias=True, exclude_none=True),
        "serviceUrl": "https://api.botframework.com",
        "channelId": "msteams",
    })


def group_activity(mentions_bot=True):
    return sdk_activity(group_stub(mentions_bot))


def personal_activity():
    return sdk_activity(personal_stub())


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.typing_started = asyncio.Event()

    async def send_activities(self, context, activities):
        for activity in activities:
            self.sent.append(activity)
            if activity.type == "typing":
                self.typing_started.set()
        return [ResourceResponse(id="sent") for _ in activities]


async def test_slow_typing_never_blocks_processing_or_final_reply():
    from teams_adapter.typing import NonBlockingTypingMiddleware

    core_started = asyncio.Event()
    reply_sent = asyncio.Event()
    release_cleanup = asyncio.Event()
    typing_finished = asyncio.Event()

    class SlowAdapter(RecordingAdapter):
        async def send_activities(self, context, activities):
            if activities[0].type == "typing":
                self.typing_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await release_cleanup.wait()
                    typing_finished.set()
            return await super().send_activities(context, activities)

    adapter = SlowAdapter()
    context = TurnContext(adapter, group_activity())

    async def logic(ctx):
        core_started.set()
        await adapter.typing_started.wait()
        await ctx.send_activity("answer")
        reply_sent.set()

    async with asyncio.timeout(2):
        task = asyncio.create_task(
            NonBlockingTypingMiddleware().on_turn(context, logic))
        try:
            await core_started.wait()
            await reply_sent.wait()
            assert not typing_finished.is_set()
            assert adapter.sent[-1].text == "answer"
        finally:
            release_cleanup.set()
            if not reply_sent.is_set():
                task.cancel()
            await task
    assert typing_finished.is_set()


async def test_typing_renews_without_overlapping_or_continuing_after_response():
    from teams_adapter.typing import NonBlockingTypingMiddleware

    third_send = asyncio.Event()
    times = []

    class RepeatingAdapter(RecordingAdapter):
        async def send_activities(self, context, activities):
            if activities[0].type == "typing":
                times.append(asyncio.get_running_loop().time())
                if len(times) == 3:
                    third_send.set()
            return await super().send_activities(context, activities)

    adapter = RepeatingAdapter()
    context = TurnContext(adapter, personal_activity())

    async def logic(ctx):
        await third_send.wait()
        await ctx.send_activity("answer")
        await asyncio.sleep(0.04)

    async with asyncio.timeout(2):
        await NonBlockingTypingMiddleware(
            interval_seconds=0.01).on_turn(context, logic)
    assert len(times) == 3
    assert all(b > a for a, b in zip(times, times[1:]))
    assert [a.type for a in adapter.sent] == ["typing"] * 3 + ["message"]


async def test_typing_timeout_does_not_fail_turn_or_stack_sends(caplog):
    from teams_adapter.typing import NonBlockingTypingMiddleware

    second_send = asyncio.Event()
    active, maximum, started = 0, 0, 0

    class HangingAdapter(RecordingAdapter):
        async def send_activities(self, context, activities):
            nonlocal active, maximum, started
            if activities[0].type == "typing":
                active += 1
                maximum = max(maximum, active)
                started += 1
                if started == 2:
                    second_send.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    active -= 1
            return await super().send_activities(context, activities)

    adapter = HangingAdapter()
    context = TurnContext(adapter, personal_activity())

    async def logic(ctx):
        await second_send.wait()
        await ctx.send_activity("answer")

    async with asyncio.timeout(2):
        await NonBlockingTypingMiddleware(
            interval_seconds=0.02, send_timeout_seconds=0.01).on_turn(context, logic)
    assert adapter.sent[-1].text == "answer"
    assert maximum == 1 and active == 0
    assert any(r.msg == "typing_send_timeout" for r in caplog.records)


async def test_typing_error_is_logged_safely_and_never_fails_answer(caplog):
    from teams_adapter.typing import NonBlockingTypingMiddleware

    class FailedAdapter(RecordingAdapter):
        async def send_activities(self, context, activities):
            if activities[0].type == "typing":
                self.typing_started.set()
                raise RuntimeError("private-signed-url-and-token")
            return await super().send_activities(context, activities)

    adapter = FailedAdapter()

    async def logic(ctx):
        await adapter.typing_started.wait()
        await ctx.send_activity("answer")

    with caplog.at_level(logging.DEBUG):
        await NonBlockingTypingMiddleware().on_turn(
            TurnContext(adapter, personal_activity()), logic)
    failure = next(r for r in caplog.records if r.msg == "typing_send_failed")
    assert failure.levelno == logging.WARNING
    assert failure.telemetry["error_type"] == "RuntimeError"
    assert "private-signed" not in caplog.text
    assert "private-signed" not in json.dumps(failure.telemetry)
    assert adapter.sent[-1].text == "answer"


@pytest.mark.parametrize("kind", ["unmentioned", "invalid_identity", "empty", "non_message"])
async def test_ineligible_messages_do_not_start_typing(kind):
    from teams_adapter.typing import NonBlockingTypingMiddleware

    activity = group_activity(mentions_bot=kind != "unmentioned")
    if kind == "invalid_identity":
        activity.conversation = None
    elif kind == "empty":
        activity.text = "<at>A</at>"
    elif kind == "non_message":
        activity.type = "typing"
    adapter = RecordingAdapter()
    handled = []

    async def logic(ctx):
        handled.append(True)
        await asyncio.sleep(0.02)

    await NonBlockingTypingMiddleware(interval_seconds=0.005).on_turn(
        TurnContext(adapter, activity), logic)
    assert handled == [True]
    assert adapter.sent == []


async def test_image_only_message_types_while_waiting_for_download():
    from teams_adapter.typing import NonBlockingTypingMiddleware
    from microsoft_agents.activity import Attachment

    adapter = RecordingAdapter()
    activity = group_activity()
    activity.text = "<at>A</at>"
    activity.attachments = [Attachment(content_type="image/png", content_url="https://x")]

    async def downloading_then_handling(ctx):
        await adapter.typing_started.wait()
        assert not ctx.responded
        await ctx.send_activity("answer after download")

    async with asyncio.timeout(2):
        await NonBlockingTypingMiddleware().on_turn(
            TurnContext(adapter, activity), downloading_then_handling)
    assert adapter.sent[0].type == "typing"
    assert adapter.sent[-1].text == "answer after download"


@pytest.mark.parametrize("cancel", [False, True])
async def test_processing_error_or_cancellation_stops_typing(cancel):
    from teams_adapter.typing import NonBlockingTypingMiddleware

    adapter = RecordingAdapter()

    async def logic(ctx):
        await adapter.typing_started.wait()
        if cancel:
            await asyncio.Event().wait()
        raise ValueError("main processing error")

    async with asyncio.timeout(2):
        task = asyncio.create_task(NonBlockingTypingMiddleware(
            interval_seconds=0.01).on_turn(TurnContext(adapter, personal_activity()), logic))
        await adapter.typing_started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await task
    sent = len(adapter.sent)
    await asyncio.sleep(0.03)
    assert len(adapter.sent) == sent
    assert not any(t.get_name().startswith("advisor-typing") for t in asyncio.all_tasks())


@pytest.mark.parametrize("field", ["interval_seconds", "send_timeout_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_typing_timing_rejected(field, value):
    from teams_adapter.typing import NonBlockingTypingMiddleware

    with pytest.raises(ValueError, match=field):
        NonBlockingTypingMiddleware(**{field: value})


async def test_production_middleware_starts_before_sdk_downloads(monkeypatch):
    import teams_adapter.__main__ as entry
    from microsoft_agents.activity import load_configuration_from_env
    from test_bot import StubCore

    config = load_configuration_from_env({
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "test",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED": "True",
    })
    core = StubCore()
    monkeypatch.setattr(entry, "load_configuration_from_env", lambda env: config)
    monkeypatch.setattr(entry, "build_advisor", lambda channel_name: core)
    app, adapter, _ = entry.build_agent_app()
    recording = RecordingAdapter()
    monkeypatch.setattr(adapter, "send_activities", recording.send_activities)

    class DownloadBarrier:
        async def download_files(self, context):
            await recording.typing_started.wait()
            assert core.requests == []
            return []

    app.options.file_downloaders = [DownloadBarrier()]
    context = TurnContext(adapter, group_activity())
    async with asyncio.timeout(2):
        await adapter.middleware_set.receive_activity_with_status(context, app.on_turn)
    assert len(core.requests) == 1
    assert [a.type for a in recording.sent] == ["typing", "message"]
    assert app.options.start_typing_timer is False


async def test_finishing_one_turn_does_not_stop_another_turn():
    from teams_adapter.typing import NonBlockingTypingMiddleware

    first_done, second_renewed = asyncio.Event(), asyncio.Event()
    middleware = NonBlockingTypingMiddleware(interval_seconds=0.005)
    first = RecordingAdapter()

    class SecondAdapter(RecordingAdapter):
        async def send_activities(self, context, activities):
            if activities[0].type == "typing" and first_done.is_set():
                second_renewed.set()
            return await super().send_activities(context, activities)

    second = SecondAdapter()

    async def first_logic(ctx):
        await second.typing_started.wait()
        await ctx.send_activity("first")
        first_done.set()

    async def second_logic(ctx):
        await first_done.wait()
        await second_renewed.wait()
        await ctx.send_activity("second")

    async with asyncio.timeout(2):
        await asyncio.gather(
            middleware.on_turn(TurnContext(first, personal_activity()), first_logic),
            middleware.on_turn(TurnContext(second, personal_activity()), second_logic))
    assert first.sent[-1].text == "first"
    assert second.sent[-1].text == "second"
    assert second_renewed.is_set()

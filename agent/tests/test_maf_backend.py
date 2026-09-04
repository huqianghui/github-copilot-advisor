"""MAFBackend 的消息构造与 vision 降级(图片输入 spec §6)。"""
import base64
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

from advisor_agent.maf_backend import (IMAGE_NOT_PROCESSED_NOTE, MAFBackend,
                                       build_user_message)
from advisor_agent.prompts import SYSTEM_PROMPT
from advisor_shared.messages import ImageInput

PNG = b"\x89PNG\r\n\x1a\n"


def test_text_only_stays_plain_string():
    """回归防线:无图时请求体必须与改动前逐字节一致。"""
    assert build_user_message("登录失败", None) == {
        "role": "user", "content": "登录失败"}
    assert build_user_message("登录失败", []) == {
        "role": "user", "content": "登录失败"}


def test_image_builds_content_parts_with_text_first():
    msg = build_user_message("这是什么错", [ImageInput(data=PNG, mime_type="image/png")])
    parts = msg["content"]
    assert msg["role"] == "user"
    assert parts[0] == {"type": "text", "text": "这是什么错"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["detail"] == "auto"
    expected = base64.b64encode(PNG).decode()
    assert parts[1]["image_url"]["url"] == f"data:image/png;base64,{expected}"


def test_empty_text_gets_placeholder():
    """纯图片场景:text part 不能为空,否则模型缺少任务指令。"""
    msg = build_user_message("", [ImageInput(data=PNG, mime_type="image/png")])
    assert msg["content"][0]["text"] == "(用户只发了图片,无文字说明)"


def test_multiple_images_all_appended():
    msg = build_user_message("看图", [
        ImageInput(data=PNG, mime_type="image/png"),
        ImageInput(data=PNG, mime_type="image/jpeg"),
    ])
    assert len(msg["content"]) == 3
    assert msg["content"][2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_run_assembles_system_history_then_multimodal_user(monkeypatch):
    """接线防线:run 必须把 images 真正交给 build_user_message。

    只测纯函数杀不掉「run 里漏传 images」这个变异 —— 那是静默失败:不报错、
    不降级,用户发了图却得到没看图的回答。顺带钉住 messages 组装顺序,以及
    「历史永远是纯字符串」这条设计约束(图片不随多轮追问重传)。
    """
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5-mini")
    backend = MAFBackend(tools=None, channel_id_provider=lambda: "19:c",
                         is_group_provider=lambda: False)
    sent = {}

    async def fake_create(*, model, messages, tools):
        sent["messages"] = messages
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=None))])

    monkeypatch.setattr(backend._client.chat.completions, "create", fake_create)
    history = [{"role": "user", "content": "[图片×1] 之前那个"},
               {"role": "assistant", "content": "之前的回答"}]
    await backend.run("这是什么错", history,
                      [ImageInput(data=PNG, mime_type="image/png")])

    messages = sent["messages"]
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1:3] == history          # 历史永远是纯字符串
    assert isinstance(messages[3]["content"], list)
    assert messages[3]["content"][1]["type"] == "image_url"


def _bad_request() -> BadRequestError:
    request = httpx.Request("POST", "https://x.openai.azure.com/chat")
    return BadRequestError("image input not supported",
                           response=httpx.Response(400, request=request),
                           body=None)


def _tool_call() -> SimpleNamespace:
    return SimpleNamespace(id="c1", function=SimpleNamespace(
        name="web_search", arguments='{"query":"x"}'))


async def _ok() -> str:
    return "{}"


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5-mini")
    return MAFBackend(tools=None, channel_id_provider=lambda: "19:c",
                      is_group_provider=lambda: False)


async def test_vision_400_retries_without_images(backend, monkeypatch):
    seen = []

    async def fake_loop(messages):
        seen.append(messages[-1]["content"])
        if isinstance(messages[-1]["content"], list):
            raise _bad_request()
        return "先检查网络代理"

    monkeypatch.setattr(backend, "_run_tool_loop", fake_loop)
    out = await backend.run("看图", [], [ImageInput(data=PNG, mime_type="image/png")])
    assert "先检查网络代理" in out
    assert IMAGE_NOT_PROCESSED_NOTE in out
    assert len(seen) == 2                      # 第一次带图,第二次纯文本
    assert isinstance(seen[1], str)


async def test_retry_preserves_system_and_history_order(backend, monkeypatch):
    """剥图重试只该换掉最后一条 user message,不能动 system 或历史。
    没有这条断言,把 messages[-1] 误写成 messages[0] 无人发现。"""
    seen = []

    async def fake_loop(messages):
        seen.append(list(messages))
        if isinstance(messages[-1]["content"], list):
            raise _bad_request()
        return "纯文本答案"

    monkeypatch.setattr(backend, "_run_tool_loop", fake_loop)
    history = [{"role": "user", "content": "上一轮"},
               {"role": "assistant", "content": "上一轮回答"}]
    await backend.run("看图", history,
                      [ImageInput(data=PNG, mime_type="image/png")])

    for messages in seen:
        assert messages[0]["role"] == "system"
        assert messages[1:3] == history        # 历史永远是纯字符串
    assert len(seen[0]) == len(seen[1])        # 剥图是替换,不是删除


async def test_retry_targets_user_message_even_if_loop_appended(backend,
                                                                monkeypatch):
    """_run_tool_loop 会往 messages 里 append 助手消息与工具结果。若这些 append
    落在 run 自己的列表上,messages[-1] 就不再是那条 user message —— 剥图改错
    位置,既没剥掉图,又切断了 tool_calls 与 tool 结果的配对,重试必然再 400。
    """
    seen = []

    async def fake_loop(messages):
        seen.append(list(messages))
        if isinstance(messages[-1]["content"], list):
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "web_search",
                                             "arguments": "{}"}}]})
            messages.append({"role": "tool", "tool_call_id": "c1",
                             "content": "{}"})
            raise _bad_request()
        return "纯文本答案"

    monkeypatch.setattr(backend, "_run_tool_loop", fake_loop)
    await backend.run("看图", [], [ImageInput(data=PNG, mime_type="image/png")])

    assert seen[1][-1] == {"role": "user", "content": "看图"}
    assert len(seen[1]) == len(seen[0])


async def test_400_after_a_successful_tool_round(backend, monkeypatch):
    """不打桩 _run_tool_loop:真实 loop 第 1 轮工具调用 append 之后,第 2 轮才 400。

    上面四个测试都在 _run_tool_loop 这个 seam 上打桩,于是「真实 loop 会变异
    传入的列表」这一前提由假 loop 自己扮演,永远自洽。这条测试驱动真实 loop
    (只打桩 create),把那个前提本身验证一遍,顺带覆盖 loop 的工具调用分支。
    """
    sent = []

    async def fake_create(*, model, messages, tools):
        sent.append(list(messages))
        has_image = any(isinstance(m.get("content"), list) for m in messages)
        if not has_image:
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="纯文本答案", tool_calls=None))])
        if len(messages) == 2:            # 第 1 轮:先要一次工具调用
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[_tool_call()]))])
        raise _bad_request()              # 第 2 轮(已 append 完)才拒

    monkeypatch.setattr(backend._client.chat.completions, "create", fake_create)
    monkeypatch.setattr(backend, "_dispatch", lambda name, args: _ok())
    out = await backend.run("看图", [], [ImageInput(data=PNG, mime_type="image/png")])
    assert out == f"纯文本答案\n\n{IMAGE_NOT_PROCESSED_NOTE}"
    assert [m["role"] for m in sent[-1]] == ["system", "user"]   # 副本被丢弃,不含 tool 残留
    assert sent[-1][-1]["content"] == "看图"


async def test_text_only_400_is_not_swallowed(backend, monkeypatch):
    """无图时的 400 是真错误,必须立刻抛出,交给 core 的重试与兜底。
    断言调用次数,否则「立刻抛」与「重试后再抛」无法区分 —— 后者会给
    从未发图的用户贴上图片提示。"""
    calls = []

    async def fake_loop(messages):
        calls.append(messages)
        raise _bad_request()

    monkeypatch.setattr(backend, "_run_tool_loop", fake_loop)
    with pytest.raises(BadRequestError):
        await backend.run("登录失败", [], None)
    assert len(calls) == 1        # 无图时不该重试


def test_note_does_not_attribute_a_cause():
    """关键词绊线,不是语义守卫:只挡计划里点名的几种归因写法
    (「部署未启用」「模型不支持」)。真正的约束和理由写在
    IMAGE_NOT_PROCESSED_NOTE 旁边的注释里。"""
    for forbidden in ("部署", "未启用", "不支持", "模型"):
        assert forbidden not in IMAGE_NOT_PROCESSED_NOTE

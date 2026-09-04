"""MAFBackend 的消息构造与 vision 降级(图片输入 spec §6)。"""
import base64
from types import SimpleNamespace

from advisor_agent.maf_backend import MAFBackend, build_user_message
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

"""视觉链路的端到端回归:真实图片穿过 AdvisorCore 抵达 Azure OpenAI。

与 test_eval_behavior.py 的图片用例分工不同:
- 本文件验证**链路通不通** —— 图片字节是否真的抵达模型。用程序生成的纯色图,
  不依赖任何 fixture,所以今天就能跑。
- eval 用例验证**模型读不读得懂真实截图**(OCR 质量),需要人放置 fixture。

守的失败模式:图片被静默丢弃。本计划中多个变异体都指向它 ——
`build_user_message(user_text, None)`、`TeamsImageDownloader(None)`、
`images=[]` —— 共同特征是不报错、不降级,用户拿到一个没看图的回答。
模型答不出颜色,就说明像素没到。

运行:uv run --env-file .env pytest -m integration agent/tests/test_vision_pipeline.py
"""
import os
import struct
import zlib

import pytest

from advisor_shared.messages import AdvisorRequest, ImageInput

pytestmark = pytest.mark.integration

REQUIRED_ENV = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
                "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY"]

# #0080FF —— 无歧义的蓝,不会被说成青或紫
BLUE = (0, 128, 255)


@pytest.fixture(autouse=True)
def require_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """标准库手搓 PNG。刻意不引入 Pillow —— 为一张纯色图加一个依赖不划算。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body)))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def test_solid_png_is_a_valid_png():
    """先确认夹具本身是合法 PNG —— 否则下面的失败会指向错误的方向。"""
    png = solid_png(64, 64, BLUE)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png[12:16] == b"IHDR" and png.endswith(b"IEND\xae\x42\x60\x82")


async def test_image_bytes_actually_reach_the_model():
    """模型能说出颜色 ⇒ 像素真的到了。这是整条链路唯一的端到端证据。"""
    from advisor_agent.factory import build_advisor

    events = []
    core = build_advisor(channel_name="vision-pipeline")
    core.event_sink = events.append

    request = AdvisorRequest(
        text="这张图是什么颜色?", conversation_key="vision-pipeline",
        channel_id="19:eval", user_id="u", user_name="pipeline",
        is_group=True,
        images=[ImageInput(data=solid_png(64, 64, BLUE),
                           mime_type="image/png")])

    response = await core.handle(request)
    answer = response.markdown.lower()

    # 承重断言:没收到像素就答不出颜色
    assert "蓝" in response.markdown or "blue" in answer, response.markdown[:300]

    event = events[-1]
    assert event.image_count == 1
    assert event.error is None
    # 纯图片场景下 question_summary 靠这个标记才不为空(spec §8)
    assert event.question_summary.startswith("[图片×1]")

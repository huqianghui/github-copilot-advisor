"""行为回归评估:需真实 Azure OpenAI + AI Search(灌过数据)。
prompt/工具描述每次改动必跑:uv run pytest -m integration agent/tests/test_eval_behavior.py"""
import os
import re
import time
from itertools import groupby
from pathlib import Path

import pytest
import yaml

from advisor_shared.events import AdvisorEvent
from advisor_shared.messages import AdvisorRequest, ImageInput

pytestmark = pytest.mark.integration

REQUIRED_ENV = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
                "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY"]

CASES = yaml.safe_load(
    (Path(__file__).parent / "eval_cases.yaml").read_text(encoding="utf-8")
)["cases"]


@pytest.fixture(autouse=True)
def require_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")


# 语言中立的片段:它们的字符构成取决于"在讨论什么技术",而不是"作者用哪种语言写作"。
# 把它们计进去,衡量的是回答有多技术,不是回答用了什么语言。
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`]*`")
_MD_LINK_TARGET = re.compile(r"\]\([^)]*\)")  # 只剥目标,保留作者写的链接文字
# URL 若用 \S+ 收尾,会把紧跟其后的中文一起吃掉 —— 中文句子在 URL 后不加空格,
# ".../mcp。配置完成后重启编辑器即可生效。" 实测被吃掉 16 个汉字里的 14 个。
_BARE_URL = re.compile(r"https?://[^\s一-鿿，。、；:：！？（）《》「」“”]+")


def _prose_only(text: str) -> str:
    """剥掉语言中立的内容:代码块、行内代码、markdown 链接目标、裸 URL。"""
    for pattern in (_CODE_BLOCK, _INLINE_CODE, _MD_LINK_TARGET, _BARE_URL):
        text = pattern.sub(" ", text)
    return text


def is_mostly_chinese(text: str) -> bool:
    """比较**语素**而非字符:汉字一字一语素,拉丁一词一语素。

    旧实现 `han > latin * 0.5` 是字符对字符,`GitHub Copilot`(2 语素但 14 字符)
    足以压掉 7 个汉字,于是无可争议的中文技术回答被判成英文 —— mcp-config
    用例就是这样三跑二败的(实测 han=699、latin=1472,阈值 736,差 37 个字符)。

    已知边界:汉字数超过英文词数的英文回答仍会被判成中文。实测真实英文回答
    han=0、latin_words≈410,离该边界很远;test_language_heuristic.py 用一条
    引用了 4 段中文报错的英文回答(han=100、latin_words=172)钉住这个方向。
    """
    prose = _prose_only(text)
    han = len(re.findall(r"[一-鿿]", prose))
    latin_words = len(re.findall(r"[a-zA-Z]+", prose))
    return han > latin_words


FIXTURES_ROOT = Path(__file__).parent


def load_images(case) -> list[ImageInput]:
    """加载用例声明的截图夹具;缺图时 skip 而非 fail(截图须人工提供,见 fixtures/README.md)。"""
    images = []
    for rel_path in case.get("images") or []:
        path = FIXTURES_ROOT / rel_path
        if not path.exists():
            pytest.skip(f"missing image fixture: {path}")
        images.append(ImageInput(data=path.read_bytes(),
                                 mime_type="image/png",
                                 name=path.name))
    return images


def make_request(text: str, images: list[ImageInput] | None = None) -> AdvisorRequest:
    return AdvisorRequest(text=text, conversation_key=f"eval-{hash(text)}",
                          channel_id="19:eval", user_id="u",
                          user_name="eval", is_group=True,
                          images=images or [])


def assert_concise_answer(markdown: str) -> None:
    summary, body = re.split(r"(?:\*\*)?建议尝试[：:](?:\*\*)?", markdown,
                             maxsplit=1)
    steps, sources = re.split(r"(?:\*\*)?来源[：:](?:\*\*)?", body, maxsplit=1)
    assert 2 <= len(re.findall(r"[。！？]+", summary)) <= 3, summary
    assert 3 <= len(re.findall(r"(?m)^\s*\d+[.)、]\s*", steps)) <= 5, steps
    assert len(summary.strip()) + len(steps.strip()) <= 600
    urls = [url.rstrip(").,;") for url in _BARE_URL.findall(sources)]
    assert 1 <= len(urls) <= 3, sources
    assert len(urls) == len(set(urls)), sources


def configure_search_fixture(core, fixture: dict) -> list[str]:
    """Use synthetic search evidence to isolate real model routing and style."""
    from advisor_agent.search.models import SearchResult
    from advisor_agent.search.web import WebSearchChain

    queries: list[str] = []

    class EmptyCombined:
        async def search_solutions(self, query, product_area=None):
            return {"no_results": True, "results": []}

    class FixtureProvider:
        name = "eval-fixture"

        async def search(self, query, top):
            scope = "trusted" if "site:" in query else "general"
            queries.append(scope)
            return [SearchResult(**item, origin="web", score=0)
                    for item in fixture.get(scope, [])][:top]

    core.backend._tools._combined = EmptyCombined()
    core.backend._tools._web = WebSearchChain([FixtureProvider()])
    return queries


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_eval_case(case, eval_turns: list[dict]):
    from advisor_agent.factory import build_advisor
    events: list[AdvisorEvent] = []
    core = build_advisor(channel_name="eval")
    core.event_sink = events.append
    queries = (configure_search_fixture(core, case["search_fixture"])
               if "search_fixture" in case else None)

    turns = case.get("multi_turn") or [case["text"]]
    key = f"eval-{case['id']}"
    images = load_images(case)
    for index, text in enumerate(turns):
        # 图片只附在第一轮,与真实用户行为一致(后续轮靠会话历史里的文字复述)
        req = make_request(text, images if index == 0 else None)
        req = req.model_copy(update={"conversation_key": key})
        turn = {
            "question": req.text,
            "images": [image.name for image in req.images],
            "response": None,
            "events": [],
        }
        eval_turns.append(turn)
        event_start = len(events)
        started_at = time.monotonic()
        try:
            resp = await core.handle(req)
            turn["response"] = resp.model_dump(mode="json")
        finally:
            turn["duration_seconds"] = time.monotonic() - started_at
            turn["events"] = [
                event.model_dump(mode="json") for event in events[event_start:]]

    assert events[-1].stage in case["expected_stage_in"], \
        f"stage={events[-1].stage}, want {case['expected_stage_in']}"
    if case.get("expect_tool_called"):
        assert case["expect_tool_called"] in events[-1].tool_latencies_ms
    if case.get("expect_mention"):
        assert resp.mentions or "@" in resp.markdown or events[-1].mentioned_human
    keywords = case.get("expect_answer_contains_any") or []
    if keywords:
        lowered = resp.markdown.lower()
        assert any(k.lower() in lowered for k in keywords), \
            f"none of {keywords} in reply: {resp.markdown[:300]}"
    if case.get("reply_language") == "zh":
        assert is_mostly_chinese(resp.markdown), resp.markdown[:200]
    elif case.get("reply_language") == "en":
        assert not is_mostly_chinese(resp.markdown), resp.markdown[:200]
    if case.get("expect_concise_answer"):
        assert_concise_answer(resp.markdown)
    if queries is not None:
        phases = [scope for scope, _ in groupby(queries)]
        assert phases == case["expected_web_scopes"], queries
        if "max_web_calls" in case:
            assert len(queries) <= case["max_web_calls"], queries
    for url in case.get("expect_source_urls", []):
        assert url in resp.markdown

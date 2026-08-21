from datetime import datetime, timezone
from types import SimpleNamespace

from advisor_shared.documents import RAW_CONTENT_MAX_CHARS
from ingestion.config import SourceConfig
from ingestion.connectors.base import RawQA
from ingestion.refine import Refiner, build_refine_prompt


class FakeChat:
    """最小 openai AsyncClient 形状:chat.completions.create(...)"""
    def __init__(self, reply: str | Exception):
        self._reply = reply
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        msg = SimpleNamespace(content=self._reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def make_raw(question="Q " * 100, answer="A " * 100) -> RawQA:
    return RawQA(
        native_id="42", title="Copilot proxy issue",
        question=question, answer=answer,
        url="https://github.com/x/y/issues/42",
        labels=["network"], doc_type="issue",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def make_config() -> SourceConfig:
    return SourceConfig(name="src-a", type="github_issues",
                        repo="x/y", product_area="vscode")


async def test_refine_produces_qadocument():
    chat = FakeChat("要点:代理配置错误。解决:设置 http.proxy 后重启。")
    doc = await Refiner(chat).refine(make_raw(), make_config())
    assert doc.content.startswith("要点")
    assert doc.source == "src-a"
    assert doc.product_area == "vscode"
    assert set(doc.keywords) == {"network", "vscode"}
    assert doc.id == doc.make_id("src-a", "42")
    assert doc.content_vector is None  # embedding 在 Task 9 才加


async def test_refine_truncates_raw_content():
    raw = make_raw(question="x" * 40_000)
    doc = await Refiner(FakeChat("ok")).refine(raw, make_config())
    assert len(doc.raw_content) == RAW_CONTENT_MAX_CHARS


async def test_refine_returns_none_on_llm_failure():
    chat = FakeChat(RuntimeError("rate limited"))
    assert await Refiner(chat).refine(make_raw(), make_config()) is None


def test_prompt_contains_question_answer_and_length_rule():
    p = build_refine_prompt(make_raw(question="THE_Q", answer="THE_A"))
    assert "THE_Q" in p and "THE_A" in p and "500" in p


async def test_refine_parses_theme_tag_into_keywords():
    chat = FakeChat(
        "要点:代理配置错误。解决:设置 http.proxy 后重启。\n"
        "主题标签: stability-network"
    )
    doc = await Refiner(chat).refine(make_raw(), make_config())
    assert "stability-network" in doc.keywords
    assert "主题标签" not in doc.content

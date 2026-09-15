from types import SimpleNamespace

from advisor_agent.search.knowledge import KnowledgeSearchClient
from advisor_agent.search.models import MIN_RERANKER_SCORE, SearchResult


class FakeEmbeddings:
    def __init__(self):
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, model, input):
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1] * 4)])


class FakeSearchClient:
    def __init__(self, docs):
        self._docs = docs
        self.kwargs = None

    async def search(self, search_text, **kwargs):
        self.kwargs = {"search_text": search_text, **kwargs}

        async def gen():
            for d in self._docs:
                yield d
        return gen()


def doc(title, score):
    return {"title": title, "content": f"c-{title}",
            "url": f"https://x/{title}", "@search.reranker_score": score}


async def test_search_returns_kb_results_above_threshold():
    fake = FakeSearchClient([doc("a", 2.5), doc("b", 1.2)])
    client = KnowledgeSearchClient(fake, FakeEmbeddings())
    results = await client.search("copilot login fails")
    assert [r.title for r in results] == ["a"]
    assert results[0].origin == "kb"
    assert results[0].score == 2.5
    assert fake.kwargs["query_type"] == "semantic"
    assert fake.kwargs["semantic_configuration_name"] == "default"


async def test_product_area_becomes_filter():
    fake = FakeSearchClient([])
    await KnowledgeSearchClient(fake, FakeEmbeddings()).search(
        "q", product_area="vscode")
    assert fake.kwargs["filter"] == "product_area eq 'vscode'"


async def test_no_filter_when_product_area_none():
    fake = FakeSearchClient([])
    await KnowledgeSearchClient(fake, FakeEmbeddings()).search("q")
    assert fake.kwargs["filter"] is None


def test_threshold_constant():
    assert MIN_RERANKER_SCORE == 1.5


async def test_kb_timing_includes_lazy_fetch_and_separates_embedding(monkeypatch):
    from advisor_shared import telemetry

    now = [0.0]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: now[0])

    class Embeddings(FakeEmbeddings):
        async def _create(self, model, input):
            now[0] += 0.5
            return await super()._create(model, input)

    class LazySearch(FakeSearchClient):
        async def search(self, search_text, **kwargs):
            async def pages():
                now[0] += 2.0
                for item in self._docs:
                    yield item
            return pages()

    client = KnowledgeSearchClient(
        LazySearch([doc("keep", 2.5), doc("drop", 1.0)]), Embeddings())
    with telemetry.trace_scope() as trace:
        results = await client.search("private-question")
    assert [r.title for r in results] == ["keep"]
    stages = {s.name: s for s in trace.timings}
    assert stages["search.kb.embedding"].duration_ms == 500
    assert stages["search.kb.azure_search"].duration_ms == 2000
    assert stages["search.kb.filter"].attributes == {
        "raw_count": 2, "result_count": 1, "filtered_count": 1}
    assert "private-question" not in str(trace.timings)


async def test_kb_fetch_failure_is_timed_not_mistaken_for_empty():
    import pytest
    from advisor_shared.telemetry import trace_scope

    class FailingSearch:
        async def search(self, **kwargs):
            async def pages():
                raise RuntimeError("private-response")
                yield
            return pages()

    with trace_scope() as trace:
        with pytest.raises(RuntimeError):
            await KnowledgeSearchClient(FailingSearch(), FakeEmbeddings()).search("q")
    fetch = next(s for s in trace.timings if s.name == "search.kb.azure_search")
    assert fetch.status == "error"
    assert fetch.error_type == "RuntimeError"
    assert not any(s.name == "search.kb.filter" for s in trace.timings)


async def test_product_area_quote_escaped():
    fake = FakeSearchClient([])
    await KnowledgeSearchClient(fake, FakeEmbeddings()).search(
        "q", product_area="vs'code")
    assert fake.kwargs["filter"] == "product_area eq 'vs''code'"

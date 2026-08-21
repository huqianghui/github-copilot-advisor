# ingestion/tests/test_search_writer.py
from datetime import datetime, timezone
from types import SimpleNamespace

from advisor_shared.documents import QADocument
from ingestion.search_writer import SearchWriter


def make_doc(n: int) -> QADocument:
    return QADocument(
        id=QADocument.make_id("s", str(n)), title=f"t{n}", content=f"c{n}",
        keywords=[], raw_content="r", url="https://x", source="s",
        doc_type="issue", product_area="vscode",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), resolved_at=None,
    )


class FakeEmbeddings:
    def __init__(self):
        self.batches: list[list[str]] = []
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, model: str, input: list[str]):
        self.batches.append(input)
        data = [SimpleNamespace(embedding=[float(i)] * 4)
                for i in range(len(input))]
        return SimpleNamespace(data=data)


class FakeSearchClient:
    def __init__(self):
        self.uploaded: list[dict] = []

    async def merge_or_upload_documents(self, documents: list[dict]):
        self.uploaded.extend(documents)
        return [SimpleNamespace(succeeded=True) for _ in documents]


async def test_upsert_embeds_and_uploads():
    embed, search = FakeEmbeddings(), FakeSearchClient()
    writer = SearchWriter(search, embed)
    count = await writer.upsert([make_doc(1), make_doc(2)])
    assert count == 2
    assert len(search.uploaded) == 2
    assert all("content_vector" in d for d in search.uploaded)
    assert embed.batches[0] == ["t1\nc1", "t2\nc2"]


async def test_upsert_batches_embeddings_by_16():
    embed, search = FakeEmbeddings(), FakeSearchClient()
    await SearchWriter(search, embed).upsert([make_doc(i) for i in range(40)])
    assert [len(b) for b in embed.batches] == [16, 16, 8]


async def test_upsert_empty_list_is_noop():
    embed, search = FakeEmbeddings(), FakeSearchClient()
    assert await SearchWriter(search, embed).upsert([]) == 0
    assert search.uploaded == []
